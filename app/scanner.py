"""The scan cycle: LinkedIn searches -> new job IDs -> per-user 'waiting' rows -> details -> match -> 'pending'.

Searches are de-duplicated across users: if two users both search "AI Engineer" in "India" with the
same LinkedIn filters, that search runs once and both get the results. Each job's full description
is fetched once and cached in the jobs table, then scored separately for every user.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from . import db
from .config import settings
from .filters import LIVE, Filters, Query, queries_for
from .linkedin import Job, LinkedInClient, RateLimited
from .matcher import evaluate, excluded_word
from .resume import ResumeProfile

log = logging.getLogger(__name__)

POSTED_HOURS = {"live": 1, "r3600": 1, "r21600": 6, "r86400": 24, "r604800": 168, "r2592000": 720}
MAX_DETAILS_PER_CYCLE = 60


@dataclass
class CycleResult:
    searches: int = 0
    listings: int = 0
    new_jobs: int = 0
    details_fetched: int = 0
    matched: int = 0
    rejected: int = 0
    skipped: str = ""
    error: str = ""
    users: list[int] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped:
            return self.skipped
        s = (f"{self.searches} searches, {self.listings} listings, {self.new_jobs} new, "
             f"{self.matched} matched, {self.rejected} filtered out")
        return s + (f" — ⚠️ {self.error}" if self.error else "")


def user_context(row) -> tuple[Filters, ResumeProfile | None]:
    return Filters.from_json(row["filters"]), ResumeProfile.from_dict(json.loads(row["resume"] or "null"))


# ---------------- cooldown after LinkedIn rate limits ----------------

def in_cooldown() -> str:
    until = db.get_meta("cooldown_until")
    if until and until > db.iso():
        return until
    return ""


def start_cooldown() -> int:
    prev = int(db.get_meta("cooldown_minutes", "0") or 0)
    minutes = min(settings.max_cooldown_minutes, max(10, prev * 2))
    db.set_meta("cooldown_minutes", str(minutes))
    db.set_meta("cooldown_until", db.iso(db.now() + timedelta(minutes=minutes)))
    return minutes


def clear_cooldown() -> None:
    db.set_meta("cooldown_minutes", "0")
    db.set_meta("cooldown_until", "")


# ---------------- evaluation (also used when a user changes filters / resume) ----------------

def too_old(row, f: Filters) -> str:
    """In ⚡ live mode, a reason if the job was posted longer ago than LIVE_MAX_AGE_MINUTES."""
    if f.posted != LIVE:
        return ""
    age = db.age_minutes(row["posted_at"])
    if age is None or age <= settings.live_max_age_minutes:
        return ""
    return f"posted {row['posted'] or 'too long ago'} (live mode: ≤ {settings.live_max_age_minutes} min)"


def judge(user_id: int, row, f: Filters, profile: ResumeProfile | None) -> str:
    """Score one delivery row that has its job details; store the result. Returns new status."""
    old = too_old(row, f)
    if old:
        db.set_result(user_id, row["job_id"], "rejected", None, old, "old")
        return "rejected"
    if f.easy_apply and row["easy_apply"] == 0:
        db.set_result(user_id, row["job_id"], "rejected", None, "applies on the company site (you chose Easy Apply only)",
                      "not_easy_apply")
        return "rejected"
    if db.is_duplicate(user_id, row["job_id"], row["title"], row["company"]):
        db.set_result(user_id, row["job_id"], "rejected", None, "same role already sent (other city)", "duplicate")
        return "rejected"
    m = evaluate(row["title"], row["company"], row["description"] or "", row["applicants"] or "", profile, f)
    status = "pending" if m.ok else "rejected"
    db.set_result(user_id, row["job_id"], status, m.score, m.reason, m.code,
                  {"match": m.to_dict(), "keyword": row["keyword"]})
    return status


def migrate_easy_apply_default() -> int:
    """One-time: existing users get the new Easy Apply-only default. Returns how many changed."""
    if db.get_meta("migrated_easy_apply_default") == "1":
        return 0
    changed = 0
    for u in db.users():
        f = Filters.from_json(u["filters"])
        if not f.easy_apply:
            f.easy_apply = True
            db.update_user(u["id"], filters=f.to_json())
            reevaluate_user(u["id"])
            changed += 1
    db.set_meta("migrated_easy_apply_default", "1")
    return changed


def migrate_live_default() -> int:
    """One-time: users still on the old 24 h default switch to ⚡ live. Returns how many changed."""
    if db.get_meta("migrated_live_default") == "1":
        return 0
    changed = 0
    for u in db.users():
        f = Filters.from_json(u["filters"])
        if f.posted == "r86400":
            f.posted = LIVE
            db.update_user(u["id"], filters=f.to_json())
            reevaluate_user(u["id"])
            changed += 1
    db.set_meta("migrated_live_default", "1")
    return changed


def reevaluate_user(user_id: int) -> int:
    """Re-score this user's recent unsent jobs after their filters or resume changed. Returns #pending."""
    u = db.get_user(user_id)
    if not u:
        return 0
    f, profile = user_context(u)
    hours = max(24, POSTED_HOURS.get(f.posted, 24))
    for row in db.deliveries(user_id, ("pending", "rejected"), since_hours=hours):
        if row["detail_status"] == "none":
            db.set_result(user_id, row["job_id"], "waiting", None)   # scanner fetches details next cycle
        else:
            judge(user_id, row, f, profile)
    return len(db.deliveries(user_id, ("pending",)))


# ---------------- the cycle ----------------

class Scanner:
    def __init__(self, client_factory=LinkedInClient) -> None:
        self.client_factory = client_factory

    def _plan(self, only_user: int | None) -> tuple[dict[str, tuple[Query, set[int]]], dict[int, tuple]]:
        plan: dict[str, tuple[Query, set[int]]] = {}
        ctx: dict[int, tuple] = {}
        for u in db.users("active"):
            if only_user is not None and u["id"] != only_user:
                continue
            if only_user is None and u["paused"]:
                continue
            f, profile = user_context(u)
            if not f.keywords:
                continue
            ctx[u["id"]] = (f, profile)
            for q in queries_for(f):
                plan.setdefault(q.key, (q, set()))[1].add(u["id"])
                if db.attach_query(u["id"], q.key) and db.query_runs(q.key):
                    # search already run for someone else: give this user its recent results too
                    ids = db.query_seen_since(q.key, POSTED_HOURS.get(q.posted, 24))
                    db.add_waiting(u["id"], ids, q.keyword)
        return plan, ctx

    async def run(self, only_user: int | None = None) -> CycleResult:
        res = CycleResult()
        until = in_cooldown()
        if until:
            res.skipped = f"LinkedIn asked us to slow down — paused until {until} UTC"
            return res
        plan, ctx = self._plan(only_user)
        res.users = sorted(ctx)
        if not plan:
            res.skipped = "no active users with keywords"
            return res

        client = self.client_factory()
        try:
            # 1. searches (least recently run first, so nothing starves if a cycle is cut short)
            order = sorted(plan.items(), key=lambda kv: db.query_runs(kv[0]))
            for qkey, (query, uids) in order:
                for page in range(settings.pages_first_scan):
                    cards = await client.search(query, start=page * 10)
                    res.searches += 1
                    if not cards:
                        break
                    res.listings += len(cards)
                    seen = db.query_seen_ids(qkey, [c.id for c in cards])
                    new = [c for c in cards if c.id not in seen]
                    if new:
                        db.upsert_cards(new)
                        db.mark_query_seen(qkey, [c.id for c in new])
                        res.new_jobs += len(new)
                        for uid in uids:
                            db.add_waiting(uid, [c.id for c in new], query.keyword)
                    if len(new) < len(cards) or len(cards) < 10:   # reached known jobs / last page
                        break
                db.query_ran(qkey)

            # 2. details + matching for every waiting row
            for uid, (f, profile) in ctx.items():
                for row in db.deliveries(uid, ("waiting",)):
                    if row["detail_status"] == "none":
                        # cheap rejects before spending a request on the description
                        old = too_old(row, f)
                        if old:
                            db.set_result(uid, row["job_id"], "rejected", None, old, "old")
                            res.rejected += 1
                            continue
                        w = excluded_word(row["title"] or "", f.exclude_words)
                        if w:
                            db.set_result(uid, row["job_id"], "rejected", None, f"title has '{w}'", "title")
                            res.rejected += 1
                            continue
                        if res.details_fetched >= MAX_DETAILS_PER_CYCLE:
                            continue
                        job = Job(id=row["job_id"], title=row["title"], company=row["company"],
                                  location=row["location"], posted=row["posted"] or "")
                        try:
                            await client.details(job)
                            db.save_details(job)
                            res.details_fetched += 1
                        except RateLimited:
                            raise
                        except httpx.HTTPError as e:
                            log.warning("details %s failed: %s", job.id, e)
                            if db.detail_failed(job.id) < 3:
                                continue
                        row = db.get_delivery(uid, row["job_id"])
                    if judge(uid, row, f, profile) == "pending":
                        res.matched += 1
                    else:
                        res.rejected += 1
            clear_cooldown()
        except RateLimited as e:
            minutes = start_cooldown()
            res.error = f"LinkedIn rate limit ({e}); backing off {minutes} min"
            log.warning(res.error)
        except httpx.HTTPError as e:
            res.error = f"network error: {e}"
            log.warning(res.error)
        finally:
            await client.close()
        log.info("cycle: %s", res.summary())
        return res
