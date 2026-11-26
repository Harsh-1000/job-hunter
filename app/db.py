"""SQLite storage.

users       one row per Telegram user: status, filters (JSON), parsed resume profile (JSON)
jobs        every LinkedIn job seen, shared by all users; full description fetched once
query_seen  which job IDs each LinkedIn search has already returned (stops paging early)
query_state when each search last ran
user_queries which searches each user has been attached to (to backfill a search others already ran)
deliveries  one row per (user, job): waiting -> pending -> sent -> applied/saved/hidden, or rejected
messages    message IDs in each chat, so "Clear chat" can delete them
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    first_name TEXT, username TEXT,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | active | blocked
    is_admin INTEGER NOT NULL DEFAULT 0,
    filters TEXT, resume TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, last_seen TEXT, last_manual_scan TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT, company TEXT, location TEXT, url TEXT, posted TEXT, posted_date TEXT,
    description TEXT, criteria TEXT, applicants TEXT, seniority TEXT, employment_type TEXT,
    detail_status TEXT NOT NULL DEFAULT 'none',  -- none | ok | failed
    detail_attempts INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS query_seen (
    qkey TEXT NOT NULL, job_id TEXT NOT NULL, seen_at TEXT NOT NULL,
    PRIMARY KEY (qkey, job_id)
);
CREATE TABLE IF NOT EXISTS query_state (qkey TEXT PRIMARY KEY, last_run TEXT, runs INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS deliveries (
    user_id INTEGER NOT NULL, job_id TEXT NOT NULL,
    keyword TEXT,
    status TEXT NOT NULL,          -- waiting | pending | sent | applied | saved | hidden | rejected
    score INTEGER, reason TEXT, reason_code TEXT, details TEXT,
    created_at TEXT NOT NULL, sent_at TEXT, message_id INTEGER,
    PRIMARY KEY (user_id, job_id)
);
CREATE INDEX IF NOT EXISTS deliveries_status ON deliveries(user_id, status);
CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS user_queries (user_id INTEGER NOT NULL, qkey TEXT NOT NULL, PRIMARY KEY (user_id, qkey));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

SHOWN = ("sent", "applied", "saved", "hidden")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")   # same format as SQLite datetime()


@contextmanager
def conn():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.db_path, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


# ---------------- meta ----------------

def get_meta(key: str, default: str = "") -> str:
    with conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


# ---------------- users ----------------

def get_user(user_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def upsert_user(user_id: int, chat_id: int, first_name: str = "", username: str = "") -> sqlite3.Row:
    with conn() as c:
        c.execute(
            """INSERT INTO users (id, chat_id, first_name, username, created_at, last_seen)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET chat_id=excluded.chat_id, first_name=excluded.first_name,
                    username=excluded.username, last_seen=excluded.last_seen""",
            (user_id, chat_id, first_name, username, iso(), iso()))
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def update_user(user_id: int, **fields) -> None:
    if not fields:
        return
    allowed = {"status", "is_admin", "filters", "resume", "paused", "last_manual_scan", "chat_id"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown user fields {bad}")
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), user_id))


def users(status: str | None = None) -> list[sqlite3.Row]:
    with conn() as c:
        if status:
            return c.execute("SELECT * FROM users WHERE status=? ORDER BY created_at", (status,)).fetchall()
        return c.execute("SELECT * FROM users ORDER BY created_at").fetchall()


def admins() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE is_admin=1").fetchall()


def count_users(status: str) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE status=?", (status,)).fetchone()[0]


def delete_user(user_id: int) -> None:
    with conn() as c:
        row = c.execute("SELECT chat_id FROM users WHERE id=?", (user_id,)).fetchone()
        c.execute("DELETE FROM deliveries WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM user_queries WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
        if row:
            c.execute("DELETE FROM messages WHERE chat_id=?", (row["chat_id"],))


# ---------------- jobs ----------------

def upsert_cards(cards: Iterable) -> None:
    with conn() as c:
        c.executemany(
            """INSERT INTO jobs (id, title, company, location, url, posted, posted_date, first_seen)
               VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
            [(j.id, j.title, j.company, j.location, j.url, j.posted, j.posted_date, iso()) for j in cards])


def save_details(job) -> None:
    with conn() as c:
        c.execute(
            """UPDATE jobs SET description=?, criteria=?, applicants=?, seniority=?, employment_type=?,
                   posted=COALESCE(NULLIF(?, ''), posted), detail_status='ok' WHERE id=?""",
            (job.description, json.dumps(job.criteria), job.applicants, job.seniority, job.employment_type,
             job.posted, job.id))


def detail_failed(job_id: str) -> int:
    with conn() as c:
        c.execute("UPDATE jobs SET detail_attempts=detail_attempts+1 WHERE id=?", (job_id,))
        n = c.execute("SELECT detail_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        if n >= 3:
            c.execute("UPDATE jobs SET detail_status='failed' WHERE id=?", (job_id,))
        return n


def get_job(job_id: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


# ---------------- searches ----------------

def query_seen_ids(qkey: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    with conn() as c:
        q = f"SELECT job_id FROM query_seen WHERE qkey=? AND job_id IN ({','.join('?' * len(ids))})"
        return {r[0] for r in c.execute(q, (qkey, *ids))}


def mark_query_seen(qkey: str, ids: list[str]) -> None:
    with conn() as c:
        c.executemany("INSERT OR IGNORE INTO query_seen (qkey, job_id, seen_at) VALUES (?,?,?)",
                      [(qkey, i, iso()) for i in ids])


def query_runs(qkey: str) -> int:
    with conn() as c:
        r = c.execute("SELECT runs FROM query_state WHERE qkey=?", (qkey,)).fetchone()
    return r[0] if r else 0


def query_ran(qkey: str) -> None:
    with conn() as c:
        c.execute("""INSERT INTO query_state (qkey, last_run, runs) VALUES (?,?,1)
                     ON CONFLICT(qkey) DO UPDATE SET last_run=excluded.last_run, runs=runs+1""", (qkey, iso()))


def attach_query(user_id: int, qkey: str) -> bool:
    """True the first time this user is attached to this search."""
    with conn() as c:
        return c.execute("INSERT OR IGNORE INTO user_queries (user_id, qkey) VALUES (?,?)",
                         (user_id, qkey)).rowcount > 0


def query_seen_since(qkey: str, hours: float) -> list[str]:
    with conn() as c:
        return [r[0] for r in c.execute("SELECT job_id FROM query_seen WHERE qkey=? AND seen_at >= ?",
                                        (qkey, iso(now() - timedelta(hours=hours))))]


# ---------------- deliveries ----------------

def add_waiting(user_id: int, job_ids: Iterable[str], keyword: str) -> int:
    with conn() as c:
        cur = c.executemany(
            """INSERT OR IGNORE INTO deliveries (user_id, job_id, keyword, status, created_at)
               VALUES (?,?,?, 'waiting', ?)""", [(user_id, j, keyword, iso()) for j in job_ids])
        return cur.rowcount


def deliveries(user_id: int, statuses: tuple[str, ...], limit: int = 500,
               since_hours: float | None = None) -> list[sqlite3.Row]:
    q = (f"""SELECT d.*, j.title, j.company, j.location, j.url, j.posted, j.applicants, j.seniority,
                    j.employment_type, j.description, j.detail_status
             FROM deliveries d JOIN jobs j ON j.id=d.job_id
             WHERE d.user_id=? AND d.status IN ({','.join('?' * len(statuses))})""")
    args: list = [user_id, *statuses]
    if since_hours is not None:
        q += " AND d.created_at >= ?"
        args.append(iso(now() - timedelta(hours=since_hours)))
    q += " ORDER BY COALESCE(d.score, -1) DESC, d.created_at LIMIT ?"
    args.append(limit)
    with conn() as c:
        return c.execute(q, args).fetchall()


def get_delivery(user_id: int, job_id: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(
            """SELECT d.*, j.title, j.company, j.location, j.url, j.posted, j.applicants, j.seniority,
                      j.employment_type, j.description, j.detail_status
               FROM deliveries d JOIN jobs j ON j.id=d.job_id WHERE d.user_id=? AND d.job_id=?""",
            (user_id, job_id)).fetchone()


def set_result(user_id: int, job_id: str, status: str, score: int | None, reason: str = "",
               reason_code: str = "", details: dict | None = None) -> None:
    with conn() as c:
        c.execute("""UPDATE deliveries SET status=?, score=?, reason=?, reason_code=?, details=?
                     WHERE user_id=? AND job_id=?""",
                  (status, score, reason, reason_code, json.dumps(details or {}), user_id, job_id))


def mark_sent(user_id: int, job_id: str, message_id: int | None) -> None:
    with conn() as c:
        c.execute("UPDATE deliveries SET status='sent', sent_at=?, message_id=? WHERE user_id=? AND job_id=?",
                  (iso(), message_id, user_id, job_id))


def set_status(user_id: int, job_id: str, status: str) -> bool:
    with conn() as c:
        return c.execute("UPDATE deliveries SET status=? WHERE user_id=? AND job_id=?",
                         (status, user_id, job_id)).rowcount > 0


def is_duplicate(user_id: int, job_id: str, title: str, company: str) -> bool:
    """Same title at the same company already queued/shown to this user in the last 7 days."""
    with conn() as c:
        return c.execute(
            f"""SELECT 1 FROM deliveries d JOIN jobs j ON j.id=d.job_id
                WHERE d.user_id=? AND d.job_id<>? AND lower(j.title)=lower(?) AND lower(j.company)=lower(?)
                  AND d.status IN ('pending',{','.join(repr(s) for s in SHOWN)})
                  AND d.created_at >= ?""",
            (user_id, job_id, title, company, iso(now() - timedelta(days=7)))).fetchone() is not None


def user_stats(user_id: int) -> dict[str, int]:
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM deliveries WHERE user_id=? GROUP BY status",
                         (user_id,)).fetchall()
        today = c.execute("SELECT COUNT(*) FROM deliveries WHERE user_id=? AND sent_at >= date('now')",
                          (user_id,)).fetchone()[0]
    out = {r["status"]: r["n"] for r in rows}
    out["sent_today"] = today
    return out


def global_stats() -> dict[str, int]:
    with conn() as c:
        return {
            "users": c.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0],
            "pending_users": c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0],
            "jobs": c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "sent": c.execute("SELECT COUNT(*) FROM deliveries WHERE sent_at IS NOT NULL").fetchone()[0],
            "sent_today": c.execute("SELECT COUNT(*) FROM deliveries WHERE sent_at >= date('now')").fetchone()[0],
        }


def prune(job_days: int = 30, seen_days: int = 35) -> None:
    """Drop old jobs (and their non-kept deliveries) and old search history."""
    with conn() as c:
        cut = iso(now() - timedelta(days=job_days))
        c.execute("DELETE FROM deliveries WHERE created_at < ? AND status NOT IN ('applied','saved')", (cut,))
        c.execute("DELETE FROM jobs WHERE first_seen < ? AND id NOT IN (SELECT job_id FROM deliveries)", (cut,))
        c.execute("DELETE FROM query_seen WHERE seen_at < ?", (iso(now() - timedelta(days=seen_days)),))
        c.execute("DELETE FROM messages WHERE ts < ?", (iso(now() - timedelta(days=3)),))


# ---------------- chat messages (for Clear chat) ----------------

def track_message(chat_id: int, message_id: int, kind: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO messages (chat_id, message_id, kind, ts) VALUES (?,?,?,?)",
                  (chat_id, message_id, kind, iso()))


def tracked_messages(chat_id: int, keep_kinds: tuple[str, ...] = ()) -> list[int]:
    """Message IDs Telegram still lets a bot delete (younger than 48 h), minus kept kinds."""
    with conn() as c:
        rows = c.execute("SELECT message_id, kind FROM messages WHERE chat_id=? AND ts >= ? ORDER BY message_id",
                         (chat_id, iso(now() - timedelta(hours=47, minutes=50)))).fetchall()
    return [r["message_id"] for r in rows if r["kind"] not in keep_kinds]


def forget_messages(chat_id: int, ids: list[int]) -> None:
    with conn() as c:
        c.executemany("DELETE FROM messages WHERE chat_id=? AND message_id=?", [(chat_id, i) for i in ids])
