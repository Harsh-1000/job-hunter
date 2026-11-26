"""Job Hunter: LinkedIn -> Telegram job alerts matched to each user's resume.

    python main.py              run the bot
    python -m app.admin ...     admin commands (users, approve, stats) — see `jobhunter help`
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from app import db
from app.config import settings


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    root.addHandler(out)
    logs = settings.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    # one file per hour, keep LOG_RETENTION_HOURS of them (default 12) — older ones are deleted
    fh = TimedRotatingFileHandler(logs / "bot.log", when="h", interval=1,
                                  backupCount=settings.log_retention_hours, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "apscheduler", "pypdf", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    from app.bot import clean_old_logs
    clean_old_logs()
    db.init()
    from app.scanner import migrate_easy_apply_default, migrate_live_default
    if n := migrate_live_default():
        logging.getLogger(__name__).info("switched %d user(s) to the live (just posted) default", n)
    if n := migrate_easy_apply_default():
        logging.getLogger(__name__).info("switched %d user(s) to the Easy Apply-only default", n)
    if not settings.bot_token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env (create the bot with @BotFather first).")
    from app.bot import build
    build().run_polling(drop_pending_updates=False, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
