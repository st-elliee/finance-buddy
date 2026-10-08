"""Weekly and monthly spending reports.

The numbers are computed by plain code from the database, with no LLM:
free, fast and always exact.
"""
import calendar
from datetime import date, timedelta

import db

MONTHS = ["Ιανουάριος", "Φεβρουάριος", "Μάρτιος", "Απρίλιος", "Μάιος",
          "Ιούνιος", "Ιούλιος", "Αύγουστος", "Σεπτέμβριος", "Οκτώβριος",
          "Νοέμβριος", "Δεκέμβριος"]
# Greek month names in the accusative and genitive case, for correct grammar.
MONTHS_ACC = ["Ιανουάριο", "Φεβρουάριο", "Μάρτιο", "Απρίλιο", "Μάιο", "Ιούνιο",
              "Ιούλιο", "Αύγουστο", "Σεπτέμβριο", "Οκτώβριο", "Νοέμβριο", "Δεκέμβριο"]
MONTHS_GEN = ["Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου",
              "Ιουνίου", "Ιουλίου", "Αυγούστου", "Σεπτεμβρίου", "Οκτωβρίου",
              "Νοεμβρίου", "Δεκεμβρίου"]


def eur(x: float) -> str:
    return f"{x:.2f}".replace(".", ",") + "€"


def _totals(start: date, end: date) -> tuple[float, dict[str, float]]:
    rows = db.spending_between(start.isoformat(), end.isoformat())
    by_cat = {r["category"]: r["total"] for r in rows}
    return sum(by_cat.values()), by_cat


def _change(current: float, previous: float, label: str) -> str:
    if previous == 0:
        return ""
    pct = round(100 * (current - previous) / previous)
    arrow = "📈" if pct > 0 else "📉" if pct < 0 else "➖"
    return f"\n{arrow} {pct:+d}% σε σχέση με {label} ({eur(previous)})"


def _body(title: str, start: date, end: date, prev_start: date,
          prev_end: date, prev_label: str) -> list[str]:
    total, by_cat = _totals(start, end)
    prev_total, _ = _totals(prev_start, prev_end)
    period = f"{start:%d/%m} – {end:%d/%m}"
    if total == 0:
        return [title, period, "", "Δεν κατέγραψες κανένα έξοδο σε αυτό το διάστημα."]

    lines = [title, period, "", f"💶 Σύνολο: {eur(total)}"
             + _change(total, prev_total, prev_label), ""]
    for cat, amount in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {cat}: {eur(amount)} ({round(100 * amount / total)}%)")

    big = db.biggest_between(start.isoformat(), end.isoformat())
    if big:
        what = big["description"] or big["category"]
        lines += ["", f"🔝 Μεγαλύτερο έξοδο: {eur(big['amount'])} – {what} "
                      f"({big['created_at'][8:10]}/{big['created_at'][5:7]})"]
    return lines


def _budget_lines(year: int, month: int) -> list[str]:
    status = db.budget_status(year, month)
    if not status:
        return []
    lines = ["", "🎯 Budget μήνα:"]
    for b in status:
        icon = "🚨" if b["percent"] > 100 else "⚠️" if b["percent"] >= 80 else "✅"
        lines.append(f"{icon} {b['category']}: {eur(b['spent'])} / "
                     f"{eur(b['limit'])} ({b['percent']}%)")
    return lines


def week_report(end: date) -> str:
    """The 7 days ending on `end`, compared with the 7 days before them."""
    start = end - timedelta(days=6)
    lines = _body("📊 Εβδομαδιαία αναφορά", start, end,
                  start - timedelta(days=7), start - timedelta(days=1),
                  "την προηγούμενη εβδομάδα")
    return "\n".join(lines + _budget_lines(end.year, end.month))


def month_report(year: int, month: int, upto_day: int | None = None) -> str:
    """A whole month, or the 1st up to `upto_day` ("month to date"),
    compared with the same span of the previous month."""
    last_day = calendar.monthrange(year, month)[1]
    start, end = date(year, month, 1), date(year, month, upto_day or last_day)

    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    prev_last = calendar.monthrange(prev_year, prev_month)[1]
    prev_end_day = min(upto_day, prev_last) if upto_day else prev_last
    prev_start = date(prev_year, prev_month, 1)
    prev_end = date(prev_year, prev_month, prev_end_day)

    title = f"📅 {MONTHS[month - 1]} {year}" + (" (μέχρι σήμερα)" if upto_day else "")
    if upto_day:
        label = f"το ίδιο διάστημα του {MONTHS_GEN[prev_month - 1]}"
    else:
        label = f"τον {MONTHS_ACC[prev_month - 1]}"

    lines = _body(title, start, end, prev_start, prev_end, label)
    return "\n".join(lines + _budget_lines(year, month))


def previous_month_report(today: date) -> str:
    """Sent on the 1st: report for the month that just ended."""
    last_month_day = today.replace(day=1) - timedelta(days=1)
    return month_report(last_month_day.year, last_month_day.month)
