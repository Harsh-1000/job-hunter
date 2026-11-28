"""Server-wide settings from .env (see .env.example). Per-user preferences live in the database."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name, "").strip()
    return float(v) if v else default


def _ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    # Telegram user IDs that are admins. Empty: the first person to /start becomes admin.
    admin_ids: set[int] = field(default_factory=lambda: _ids("ADMIN_IDS"))
    # approval: new users wait for an admin to approve them. open: anyone can use the bot.
    access_mode: str = os.getenv("ACCESS_MODE", "approval").strip().lower()
    max_users: int = _int("MAX_USERS", 50)

    # How often the scanner runs. LinkedIn rate-limits fast clients, so there is a hard floor.
    scan_every_minutes: float = max(2.0, _float("SCAN_EVERY_MINUTES", 3))
    # "⚡ Just posted" (the default date filter): only jobs at most this old are sent.
    live_max_age_minutes: int = max(5, _int("LIVE_MAX_AGE_MINUTES", 30))
    pages_first_scan: int = _int("PAGES_FIRST_SCAN", 3)   # backlog pages for a brand-new search
    max_per_cycle: int = _int("MAX_PER_CYCLE", 10)         # job messages per user per cycle
    manual_scan_cooldown_s: int = _int("MANUAL_SCAN_COOLDOWN_SECONDS", 120)
    delay_min: float = _float("REQUEST_DELAY_MIN", 2)
    delay_max: float = _float("REQUEST_DELAY_MAX", 5)
    max_cooldown_minutes: int = _int("MAX_COOLDOWN_MINUTES", 60)

    # Logs (data/logs) are rotated hourly and deleted after this many hours.
    log_retention_hours: int = max(1, _int("LOG_RETENTION_HOURS", 12))
    max_resume_mb: float = _float("MAX_RESUME_MB", 5)
    timezone: str = os.getenv("TIMEZONE", "Asia/Kolkata")
    data_dir: Path = ROOT / os.getenv("DATA_DIR", "data")

    # Claude worker (the `ai` container). Empty AI_URL = AI off, keyword match only.
    ai_url: str = os.getenv("AI_URL", "").strip().rstrip("/")
    ai_max_per_cycle: int = max(0, _int("AI_MAX_PER_CYCLE", 20))
    ai_timeout_s: float = _float("AI_TIMEOUT_SECONDS", 100)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobhunter.db"

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"


settings = Settings()
