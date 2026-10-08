"""SQLite storage for expenses and monthly budgets."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "finance.db"

# All dates are handled in local (Greek) time, even when the bot runs on a
# server whose clock is set to UTC.
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Athens"))

# Special budget "category" meaning "all expenses combined".
TOTAL = "σύνολο"


def now() -> datetime:
    """Current local time, as a naive datetime (that's how it's stored)."""
    return datetime.now(TZ).replace(tzinfo=None)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                amount      REAL    NOT NULL,
                category    TEXT    NOT NULL,
                description TEXT,
                created_at  TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                category      TEXT PRIMARY KEY,
                monthly_limit REAL NOT NULL
            )
            """
        )


# ---------- Expenses ----------

def add_expense(amount: float, category: str, description: str = "",
                when: datetime | None = None) -> int:
    when = when or now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO expenses (amount, category, description, created_at) "
            "VALUES (?, ?, ?, ?)",
            (amount, category.lower(), description,
             when.isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def last_expenses(limit: int = 10):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM expenses ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def delete_last():
    """Delete the most recent expense and return it (or None if empty)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM expenses ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            conn.execute("DELETE FROM expenses WHERE id = ?", (row["id"],))
        return row


def spending_between(start: str, end: str, category: str = ""):
    """Totals per category for dates start..end (YYYY-MM-DD, inclusive)."""
    sql = ("SELECT category, SUM(amount) AS total, COUNT(*) AS n "
           "FROM expenses WHERE date(created_at) BETWEEN ? AND ?")
    params = [start, end]
    if category:
        sql += " AND category = ?"
        params.append(category.lower())
    sql += " GROUP BY category ORDER BY total DESC"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def month_summary(year: int, month: int):
    prefix = f"{year:04d}-{month:02d}"
    with get_conn() as conn:
        return conn.execute(
            "SELECT category, SUM(amount) AS total, COUNT(*) AS n "
            "FROM expenses WHERE created_at LIKE ? "
            "GROUP BY category ORDER BY total DESC",
            (prefix + "%",),
        ).fetchall()


def biggest_between(start: str, end: str):
    """The single largest expense in start..end (YYYY-MM-DD, inclusive)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM expenses WHERE date(created_at) BETWEEN ? AND ? "
            "ORDER BY amount DESC LIMIT 1", (start, end)
        ).fetchone()


# ---------- Budgets ----------

def set_budget(category: str, monthly_limit: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO budgets (category, monthly_limit) VALUES (?, ?) "
            "ON CONFLICT(category) DO UPDATE SET monthly_limit = excluded.monthly_limit",
            (category.lower(), monthly_limit),
        )


def delete_budget(category: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM budgets WHERE category = ?",
                           (category.lower(),))
        return cur.rowcount > 0


def get_budgets() -> dict[str, float]:
    with get_conn() as conn:
        rows = conn.execute("SELECT category, monthly_limit FROM budgets").fetchall()
    return {r["category"]: r["monthly_limit"] for r in rows}


def month_spent(year: int, month: int, category: str = TOTAL) -> float:
    """Amount spent in a month, for one category or for everything (TOTAL)."""
    sql = "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE created_at LIKE ?"
    params = [f"{year:04d}-{month:02d}%"]
    if category != TOTAL:
        sql += " AND category = ?"
        params.append(category.lower())
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()[0]


def budget_status(year: int, month: int) -> list[dict]:
    status = []
    for cat, limit in sorted(get_budgets().items()):
        spent = round(month_spent(year, month, cat), 2)
        status.append({"category": cat, "limit": limit, "spent": spent,
                       "remaining": round(limit - spent, 2),
                       "percent": round(100 * spent / limit) if limit else 0})
    return status


def budget_alerts(category: str, amount: float, when: datetime) -> list[str]:
    """Called right after an expense is saved. Returns alerts only when THIS
    expense crossed the 80% or the 100% threshold of a budget, so the user
    isn't told the same thing again on every following expense."""
    budgets = get_budgets()
    alerts = []
    for cat in (category.lower(), TOTAL):
        if cat not in budgets or budgets[cat] <= 0:
            continue
        limit = budgets[cat]
        after = month_spent(when.year, when.month, cat)
        before = after - amount
        if cat == TOTAL:
            to_label, of_label = "το συνολικό budget", "του συνολικού budget"
        else:
            to_label, of_label = f"το budget για {cat}", f"του budget για {cat}"
        if before <= limit < after:
            alerts.append(f"🚨 Ξεπέρασες {to_label}: {after:.2f}€ από "
                          f"{limit:.2f}€ ({when:%m/%Y})".replace(".", ","))
        elif before < 0.8 * limit <= after:
            alerts.append(f"⚠️ Έφτασες στο {round(100 * after / limit)}% {of_label}: "
                          f"{after:.2f}€ από {limit:.2f}€ ({when:%m/%Y})".replace(".", ","))
    return alerts
