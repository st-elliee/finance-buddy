"""The bot's "brain": Gemini with function calling.

The LLM never sees the whole database. It only sees the user's message and
whatever the tools below return (e.g. a total per category).

Budget alerts do NOT go through the LLM: the code collects them when an
expense is saved and appends them to the reply, so they can't be dropped
or altered by the model.
"""
import os
import threading
from datetime import datetime, timedelta

from google import genai
from google.genai import types

import db

CATEGORIES = [
    "φαγητό", "καφές", "σούπερ μάρκετ", "μεταφορές", "διασκέδαση",
    "ψώνια", "λογαριασμοί", "συνδρομές", "υγεία", "σπουδές", "άλλο",
]
WEEKDAYS = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη",
            "Παρασκευή", "Σάββατο", "Κυριακή"]

# Per-message context while Gemini calls tools: the budget alerts collected
# so far and the time the message was sent. threading.local keeps two
# messages processed in parallel from mixing them up.
_ctx = threading.local()


def _sent_at() -> datetime:
    """When the message was sent (local time). If the bot was offline and
    reads the message later, the expense is still saved on the right day."""
    return getattr(_ctx, "sent_at", None) or db.now()


# ---------- Tools the LLM can call ----------
# The docstrings below are sent to Gemini as the tool descriptions.

def add_expense(amount: float, category: str, description: str = "",
                day: str = "") -> dict:
    """Saves one expense.

    Args:
        amount: Amount in euros, a positive number.
        category: One of the allowed categories.
        description: Short description, e.g. "σουβλάκια". Must not repeat the category.
        day: Date as YYYY-MM-DD if the expense was NOT today, otherwise empty.
    """
    if amount <= 0:
        return {"error": "Το ποσό πρέπει να είναι θετικό."}
    category = category.lower().strip()
    if category not in CATEGORIES:
        category = "άλλο"
    when = None
    if day:
        try:
            d = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return {"error": f"Μη έγκυρη ημερομηνία: {day}"}
        if d.date() > db.now().date():
            return {"error": "Η ημερομηνία είναι στο μέλλον."}
        when = d.replace(hour=12)
    else:
        when = _sent_at()  # when the message was SENT, not when we read it
    expense_id = db.add_expense(round(amount, 2), category, description, when)
    alerts = db.budget_alerts(category, round(amount, 2), when)
    if hasattr(_ctx, "alerts"):
        _ctx.alerts.extend(alerts)
    return {"ok": True, "id": expense_id, "amount": round(amount, 2),
            "category": category, "description": description,
            "date": when.date().isoformat()}


def spending_summary(start_date: str, end_date: str,
                     category: str = "") -> dict:
    """Returns how much was spent in a date range, in total and per category.

    Args:
        start_date: Start of the range, YYYY-MM-DD (inclusive).
        end_date: End of the range, YYYY-MM-DD (inclusive).
        category: Optionally a single category; empty for all categories.
    """
    rows = db.spending_between(start_date, end_date, category)
    by_cat = [{"category": r["category"], "total": round(r["total"], 2),
               "count": r["n"]} for r in rows]
    return {"start": start_date, "end": end_date,
            "total": round(sum(c["total"] for c in by_cat), 2),
            "by_category": by_cat}


def recent_expenses(limit: int = 10) -> dict:
    """Returns the most recent expenses (up to 20).

    Args:
        limit: How many expenses to return.
    """
    rows = db.last_expenses(max(1, min(int(limit), 20)))
    return {"expenses": [
        {"id": r["id"], "date": r["created_at"][:10], "amount": r["amount"],
         "category": r["category"], "description": r["description"]}
        for r in rows
    ]}


def delete_last_expense() -> dict:
    """Deletes the most recent expense. Only when the user explicitly asks."""
    row = db.delete_last()
    if not row:
        return {"ok": False, "error": "Δεν υπάρχουν καταχωρήσεις."}
    return {"ok": True, "deleted": {"id": row["id"], "amount": row["amount"],
                                    "category": row["category"]}}


def set_budget(category: str, monthly_limit: float) -> dict:
    """Sets or changes a monthly budget. monthly_limit = 0 removes it.

    Args:
        category: One of the allowed categories, or "σύνολο" for a limit on all expenses combined.
        monthly_limit: Monthly limit in euros. 0 removes the budget.
    """
    category = category.lower().strip()
    if category not in CATEGORIES and category != db.TOTAL:
        return {"error": f"Άγνωστη κατηγορία: {category}"}
    if monthly_limit < 0:
        return {"error": "Το όριο δεν μπορεί να είναι αρνητικό."}
    if monthly_limit == 0:
        return {"ok": db.delete_budget(category), "removed": category}
    db.set_budget(category, round(monthly_limit, 2))
    now = db.now()
    spent = round(db.month_spent(now.year, now.month, category), 2)
    return {"ok": True, "category": category, "monthly_limit": round(monthly_limit, 2),
            "spent_this_month": spent}


def budget_status() -> dict:
    """Returns every budget and how much has been spent on it this month."""
    now = db.now()
    return {"month": f"{now:%m/%Y}", "budgets": db.budget_status(now.year, now.month)}


TOOLS = [add_expense, spending_summary, recent_expenses, delete_last_expense,
         set_budget, budget_status]

# ---------- Prompts (in Greek, because the bot talks to the user in Greek) ----------

SYSTEM_PROMPT = """Είσαι ο Finance Buddy, βοηθός καταγραφής προσωπικών εξόδων.
Σήμερα είναι {weekday} {today}. Χθες ήταν {yesterday}.

Επιτρεπτές κατηγορίες: {categories}.

Κανόνες:
- Όταν ο χρήστης αναφέρει έξοδο, κάλεσε add_expense (μία φορά για κάθε έξοδο).
  Διάλεξε την πιο ταιριαστή κατηγορία. Το "καφές" είναι ξεχωριστό από το "φαγητό".
- Η περιγραφή (description) είναι σύντομη και ΔΕΝ επαναλαμβάνει την κατηγορία.
  Αν δεν υπάρχει κάτι παραπάνω από την κατηγορία, άφησέ την κενή.
- Αν λείπει το ποσό ή είναι ασαφές, ρώτα αντί να μαντέψεις.
- Για ερωτήσεις για το πόσα ξόδεψε, κάλεσε spending_summary με τις σωστές
  ημερομηνίες (π.χ. "αυτόν τον μήνα" = από την 1η του μήνα έως σήμερα).
- ΠΟΤΕ μην επινοείς ποσά. Κάθε νούμερο πρέπει να έρχεται από εργαλείο ή από
  αυτό που έγραψε ο χρήστης / φαίνεται καθαρά στην απόδειξη.
- Σβήσε καταχώρηση μόνο αν ο χρήστης το ζητήσει ρητά.
- Για budget: set_budget για ορισμό/αλλαγή/αφαίρεση (όριο 0 = αφαίρεση,
  κατηγορία "σύνολο" = όριο σε όλα μαζί). Για «πώς πάω με τα budget» κάλεσε
  budget_status. Μην αναφέρεις εσύ ειδοποιήσεις υπέρβασης μετά από καταχώρηση,
  αυτές προστίθενται αυτόματα από το σύστημα.
- Μετά από καταχώρηση, επιβεβαίωσε τι γράφτηκε (ποσό, κατηγορία,
  ημερομηνία αν δεν είναι σήμερα).
- Απάντα σύντομα, στα ελληνικά, στον ενικό (μίλα στον χρήστη με «εσύ»),
  με ποσά σε μορφή 12,50€.
- Γράφεις σε Telegram ως απλό κείμενο: ΜΗΝ χρησιμοποιείς markdown (**, __, #).
  Για λίστες χρησιμοποίησε απλές παύλες.
- Αν το μήνυμα δεν αφορά οικονομικά, πες ευγενικά ότι βοηθάς μόνο με τα έξοδα.
"""

RECEIPT_PROMPT = """Ο χρήστης σού έστειλε φωτογραφία απόδειξης.{caption}

1. Βρες το ΤΕΛΙΚΟ ΣΥΝΟΛΟ πληρωμής (ΣΥΝΟΛΟ / ΠΛΗΡΩΤΕΟ / TOTAL), όχι επιμέρους
   γραμμές, όχι ΦΠΑ, όχι ρέστα.
2. Βρες το κατάστημα και την ημερομηνία της απόδειξης.
3. Κάλεσε add_expense ΜΙΑ φορά με το σύνολο, την πιο ταιριαστή κατηγορία,
   περιγραφή = όνομα καταστήματος, και day = ημερομηνία απόδειξης
   (κενό αν είναι σήμερα ή δεν διαβάζεται).
4. Αν η φωτό δεν είναι απόδειξη ή το σύνολο δεν διαβάζεται καθαρά, ΜΗΝ
   καταχωρήσεις τίποτα. Πες τι δεν διαβάζεται και ζήτα καλύτερη φωτό ή να
   γράψει το ποσό.
5. Μην αναφέρεις ΑΦΜ, αριθμούς κάρτας ή άλλα στοιχεία της απόδειξης στην απάντηση.
"""


class FinanceAgent:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    def _system_prompt(self) -> str:
        now = _sent_at()  # "today"/"yesterday" relative to when the user wrote
        return SYSTEM_PROMPT.format(
            weekday=WEEKDAYS[now.weekday()],
            today=now.date().isoformat(),
            yesterday=(now - timedelta(days=1)).date().isoformat(),
            categories=", ".join(CATEGORIES),
        )

    def handle(self, text: str, sent_at: datetime | None = None) -> str:
        """Handles a text message and returns the reply."""
        return self._run(text, sent_at)

    def handle_receipt(self, image: bytes, mime_type: str = "image/jpeg",
                       caption: str = "", sent_at: datetime | None = None) -> str:
        """Handles a receipt photo (plus optional caption) and saves it."""
        note = f'\nΗ λεζάντα του χρήστη: "{caption}"' if caption else ""
        contents = [
            types.Part.from_bytes(data=image, mime_type=mime_type),
            RECEIPT_PROMPT.format(caption=note),
        ]
        return self._run(contents, sent_at)

    def _run(self, contents, sent_at: datetime | None = None) -> str:
        """Sends the request to Gemini, lets it call whatever tools it needs
        (automatic function calling in the SDK) and returns the final reply,
        followed by any budget alerts raised along the way."""
        _ctx.alerts = []
        _ctx.sent_at = sent_at
        try:
            text = self._generate(contents)
            return "\n\n".join([text, *_ctx.alerts])
        finally:
            del _ctx.alerts
            del _ctx.sent_at

    def _generate(self, contents) -> str:
        response = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=self._system_prompt(),
                tools=TOOLS,
                temperature=0,
            ),
        )
        text = response.text or "Δεν κατάλαβα, μπορείς να το ξαναπείς;"
        # Safety net: Telegram shows raw text, so strip any markdown the model adds.
        return text.replace("**", "").replace("__", "")
