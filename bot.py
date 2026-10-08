"""Finance Buddy: a Telegram bot for tracking personal expenses, with Gemini
for natural language and receipt photos, budgets with alerts, and scheduled
weekly/monthly reports."""
import asyncio
import logging
import os
import sys
from datetime import time
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
# Load .env before our own modules are imported, so they see its settings.
# Explicit path, so it's found however the bot is started.
load_dotenv(BASE_DIR / ".env")

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import db
import report

TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

# Logs always go to bot.log (up to 3 files x 1MB, old ones rotate out), and
# also to the console when there is one. Under pythonw.exe (no window) there
# is no console, so bot.log is the only place to see what happened.
_handlers = [RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=1_000_000,
                                 backupCount=2, encoding="utf-8")]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, handlers=_handlers,
                    format="%(asctime)s %(levelname)s %(message)s")
# httpx logs every request URL, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

agent = None  # created in main() if GEMINI_API_KEY is set


def sent_at(update: Update):
    """When the message was SENT, in local time. If the bot was offline and
    reads the message later, the expense is still saved on the right day."""
    return update.message.date.astimezone(db.TZ).replace(tzinfo=None)


def restricted(func):
    """Only the owner (ALLOWED_USER_ID) can use the bot."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if ALLOWED_USER_ID == 0:
            await update.message.reply_text(
                f"Το user ID σου είναι {uid}.\n"
                "Βάλ' το στο .env ως ALLOWED_USER_ID και κάνε restart το bot."
            )
            return
        if uid != ALLOWED_USER_ID:
            return  # silently ignore strangers
        return await func(update, context)
    return wrapper


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Γεια! Γράψε μου ελεύθερα, π.χ. «12€ σουβλάκια», «χθες 3,5 καφές» ή "
        "«πόσα ξόδεψα σε φαγητό αυτόν τον μήνα;».\n"
        "Μπορείς και να μου στείλεις φωτό απόδειξης 📸 ή να ορίσεις budget "
        "(«βάλε budget 150€ για φαγητό»).\n\n"
        "Ή χρησιμοποίησε εντολές:\n"
        "/add <ποσό> <κατηγορία> [περιγραφή]  π.χ. /add 12,50 φαγητό σουβλάκια\n"
        "/last  – τελευταία 10 έξοδα\n"
        "/summary  – σύνοψη τρέχοντος μήνα\n"
        "/budget  – πώς πας με τα budget σου\n"
        "/report  – αναφορά εβδομάδας · /report μήνας – αναφορά μήνα\n"
        "/undo  – σβήνει την τελευταία καταχώρηση"
    )


@restricted
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Χρήση: /add <ποσό> <κατηγορία> [περιγραφή]")
        return
    try:
        amount = float(args[0].replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"Το «{args[0]}» δεν είναι έγκυρο ποσό.")
        return
    category = args[1]
    description = " ".join(args[2:])
    when = sent_at(update)
    expense_id = db.add_expense(amount, category, description, when)
    alerts = db.budget_alerts(category, amount, when)
    await update.message.reply_text(
        "\n\n".join([
            f"✅ #{expense_id}: {amount:.2f}€ · {category.lower()}"
            + (f" · {description}" if description else ""),
            *alerts,
        ])
    )


@restricted
async def budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = db.now()
    rows = db.budget_status(now.year, now.month)
    if not rows:
        await update.message.reply_text(
            "Δεν έχεις ορίσει budget. Γράψε π.χ. «βάλε budget 150€ για φαγητό»."
        )
        return
    lines = [f"🎯 Budget {now:%m/%Y}", ""]
    for b in rows:
        icon = "🚨" if b["percent"] > 100 else "⚠️" if b["percent"] >= 80 else "✅"
        lines.append(f"{icon} {b['category']}: {b['spent']:.2f}€ / "
                     f"{b['limit']:.2f}€ ({b['percent']}%)")
    await update.message.reply_text("\n".join(lines))


@restricted
async def last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.last_expenses()
    if not rows:
        await update.message.reply_text("Δεν υπάρχουν καταχωρήσεις ακόμα.")
        return
    lines = [
        f"#{r['id']} {r['created_at'][:10]}  {r['amount']:.2f}€  {r['category']}"
        + (f" – {r['description']}" if r["description"] else "")
        for r in rows
    ]
    await update.message.reply_text("\n".join(lines))


@restricted
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = db.now()
    rows = db.month_summary(now.year, now.month)
    if not rows:
        await update.message.reply_text("Κανένα έξοδο αυτόν τον μήνα.")
        return
    total = sum(r["total"] for r in rows)
    lines = [f"📊 {now:%m/%Y} – σύνολο {total:.2f}€", ""]
    lines += [
        f"{r['category']}: {r['total']:.2f}€ ({r['n']}x, {r['total'] / total:.0%})"
        for r in rows
    ]
    await update.message.reply_text("\n".join(lines))


@restricted
async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = db.delete_last()
    if row:
        await update.message.reply_text(
            f"🗑 Σβήστηκε #{row['id']}: {row['amount']:.2f}€ {row['category']}"
        )
    else:
        await update.message.reply_text("Δεν υπάρχει κάτι να σβήσω.")


async def ask_agent(update: Update, func, *args):
    """Shared by text and photo handlers: run the agent and send its reply."""
    if agent is None:
        await update.message.reply_text(
            "Δεν έχει ρυθμιστεί GEMINI_API_KEY – προς το παρόν μόνο εντολές (/start)."
        )
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        # The Gemini SDK call is blocking, so run it in a thread to keep
        # the bot responsive while waiting.
        reply = await asyncio.to_thread(func, *args)
    except Exception as e:
        logging.exception("Gemini error")
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            reply = "Έφτασα το όριο του Gemini για λίγο – δοκίμασε σε ένα λεπτό ή χρησιμοποίησε /add."
        else:
            reply = "Κάτι πήγε στραβά με το Gemini – δες το bot.log. Μπορείς να χρησιμοποιήσεις /add."
    await update.message.reply_text(reply)


@restricted
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_agent(update, agent.handle if agent else None,
                    update.message.text, sent_at(update))


@restricted
async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        # Telegram sends each photo in several sizes; take the largest.
        tg_file = await msg.photo[-1].get_file()
        mime = "image/jpeg"
    else:
        # Image sent "as a file" (full quality, no compression).
        if msg.document.file_size and msg.document.file_size > 10 * 1024 * 1024:
            await msg.reply_text("Η εικόνα είναι πολύ μεγάλη (όριο 10MB).")
            return
        tg_file = await msg.document.get_file()
        mime = msg.document.mime_type or "image/jpeg"
    image = bytes(await tg_file.download_as_bytearray())
    await ask_agent(update, agent.handle_receipt if agent else None,
                    image, mime, msg.caption or "", sent_at(update))


@restricted
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = db.now().date()
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("μήνας", "μηνας", "month", "μ"):
        text = report.month_report(today.year, today.month, upto_day=today.day)
    elif arg in ("προηγούμενος", "προηγουμενος", "last", "π"):
        text = report.previous_month_report(today)
    else:
        text = report.week_report(today)
    await update.message.reply_text(text)


# ---------- Scheduled reports ----------

async def weekly_job(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(ALLOWED_USER_ID,
                                   report.week_report(db.now().date()))


async def monthly_job(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(ALLOWED_USER_ID,
                                   report.previous_month_report(db.now().date()))


def schedule_reports(app: Application):
    if not ALLOWED_USER_ID:
        logging.warning("Χωρίς ALLOWED_USER_ID δεν ξέρω πού να στέλνω αναφορές.")
        return
    if app.job_queue is None:
        logging.warning('Λείπει το job queue: τρέξε pip install -r requirements.txt')
        return
    # In python-telegram-bot, days are 0 = Sunday ... 6 = Saturday.
    app.job_queue.run_daily(weekly_job, time(21, 0, tzinfo=db.TZ), days=(0,),
                            name="weekly_report")
    app.job_queue.run_monthly(monthly_job, time(10, 0, tzinfo=db.TZ), day=1,
                              name="monthly_report")
    logging.info("Αναφορές: κάθε Κυριακή 21:00 και την 1η του μήνα 10:00")


def main():
    global agent
    db.init_db()
    if os.environ.get("GEMINI_API_KEY"):
        from agent import FinanceAgent
        agent = FinanceAgent()
        logging.info("Gemini agent ενεργός (μοντέλο: %s)", agent.model)
    else:
        logging.warning("Δεν βρέθηκε GEMINI_API_KEY – τρέχω χωρίς LLM.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("budget", budget))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, photo))
    app.add_handler(CommandHandler("report", report_cmd))
    schedule_reports(app)
    app.run_polling()


if __name__ == "__main__":
    main()
