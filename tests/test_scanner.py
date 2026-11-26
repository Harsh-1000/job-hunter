import asyncio
import json

from app import db
from app.filters import Filters
from app.resume import ResumeProfile
from app.scanner import Scanner, in_cooldown, reevaluate_user
from tests.fakes import FakeLinkedIn, factory

ME = ResumeProfile(skills=["Node.js", "TypeScript", "RAG", "LLMs", "MCP", "AWS"], years=2.5)
GOOD = "2-4 years of experience. Node.js, TypeScript, RAG, LLMs, MCP on AWS. Python and Kafka a plus."
SENIOR = "8+ years of experience with Node.js"


def add_user(uid, keywords=("AI Engineer",), resume=ME, **flt):
    db.upsert_user(uid, uid * 10, f"u{uid}")
    db.update_user(uid, status="active", filters=Filters(keywords=list(keywords), **flt).to_json(),
                   resume=json.dumps(resume.to_dict()) if resume else None)


def run(scanner, **kw):
    return asyncio.run(scanner.run(**kw))


LISTING = {"AI Engineer": [("1", "AI Engineer", "Acme"), ("2", "Senior AI Engineer", "Beta"),
                           ("3", "AI Engineer", "Gamma"), ("4", "Sales Executive", "Delta")]}
DESCS = {"1": GOOD, "2": GOOD, "3": SENIOR, "4": "Sell things. 2 years experience"}


def test_cycle_matches_and_rejects_with_reasons():
    add_user(1)
    res = run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    assert res.new_jobs == 4 and res.matched == 1 and res.rejected == 3 and not res.error
    pending = db.deliveries(1, ("pending",))
    assert [r["job_id"] for r in pending] == ["1"] and pending[0]["score"] >= 70
    reasons = {r["job_id"]: r["reason_code"] for r in db.deliveries(1, ("rejected",))}
    assert reasons == {"2": "title", "3": "years", "4": "unrelated"}
    # the senior title was rejected without spending a request on its description
    assert "2" not in FakeLinkedIn.instances[-1].detail_calls


def test_same_search_shared_and_details_fetched_once():
    add_user(1)
    add_user(2)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    fake = FakeLinkedIn.instances[-1]
    assert len(fake.searches) == 1           # one search for two users
    assert sorted(fake.detail_calls) == ["1", "3", "4"]   # each description once
    assert len(db.deliveries(1, ("pending",))) == len(db.deliveries(2, ("pending",))) == 1


def test_second_cycle_only_new_jobs():
    add_user(1)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    listing2 = {"AI Engineer": [("9", "AI Engineer", "New Co")] + LISTING["AI Engineer"]}
    res = run(Scanner(factory(listings=listing2, descriptions={**DESCS, "9": GOOD})))
    assert res.new_jobs == 1 and res.matched == 1
    assert FakeLinkedIn.instances[-1].detail_calls == ["9"]


def test_paging_stops_when_page_has_known_jobs():
    add_user(1)
    big = {"AI Engineer": [(str(i), "AI Engineer", f"C{i}") for i in range(100, 135)]}
    run(Scanner(factory(listings=big, descriptions={})))
    assert [s[2] for s in FakeLinkedIn.instances[-1].searches] == [0, 10, 20]   # PAGES_FIRST_SCAN=3
    run(Scanner(factory(listings=big, descriptions={})))
    assert [s[2] for s in FakeLinkedIn.instances[-1].searches] == [0]


def test_new_user_on_existing_search_gets_backfill():
    add_user(1)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    add_user(2)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    assert [r["job_id"] for r in db.deliveries(2, ("pending",))] == ["1"]


def test_duplicate_role_other_city_rejected():
    add_user(1)
    listing = {"AI Engineer": [("1", "AI Engineer", "Acme"), ("5", "AI Engineer", "Acme")]}
    run(Scanner(factory(listings=listing, descriptions={"1": GOOD, "5": GOOD})))
    assert len(db.deliveries(1, ("pending",))) == 1
    assert db.deliveries(1, ("rejected",))[0]["reason_code"] == "duplicate"


def test_paused_and_inactive_users_skipped_unless_manual():
    add_user(1)
    db.update_user(1, paused=1)
    add_user(2)
    db.update_user(2, status="pending")
    res = run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    assert res.skipped == "no active users with keywords"
    res = run(Scanner(factory(listings=LISTING, descriptions=DESCS)), only_user=1)
    assert res.users == [1] and res.matched == 1


def test_rate_limit_starts_cooldown_and_keeps_work_waiting():
    add_user(1)
    res = run(Scanner(factory(listings=LISTING, descriptions=DESCS, rate_limit_after=2)))
    assert "rate limit" in res.error and in_cooldown()
    assert db.deliveries(1, ("waiting",))                 # not lost: retried later
    res = run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    assert res.skipped.startswith("LinkedIn asked us to slow down")
    db.set_meta("cooldown_until", "")
    res = run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    assert not res.error and not in_cooldown() and db.get_meta("cooldown_minutes") == "0"
    assert not db.deliveries(1, ("waiting",))              # leftover work finished
    assert [r["job_id"] for r in db.deliveries(1, ("pending",))] == ["1"]


def test_cooldown_doubles():
    from app.scanner import start_cooldown
    assert [start_cooldown() for _ in range(5)] == [10, 20, 40, 60, 60]


def test_threshold_change_reevaluates_without_refetch():
    add_user(1, min_score=99)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    assert not db.deliveries(1, ("pending",))
    db.update_user(1, filters=Filters(keywords=["AI Engineer"], min_score=20).to_json())
    assert reevaluate_user(1) == 1


def test_no_resume_jobs_still_sent_without_score():
    add_user(1, resume=None)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS)))
    rows = db.deliveries(1, ("pending",))
    assert rows and all(r["score"] is None for r in rows)


# ---------------- ⚡ live mode (default): only just-posted jobs ----------------

def test_live_is_default_and_searches_past_hour():
    from app.filters import Filters, queries_for
    q = queries_for(Filters(keywords=["AI Engineer"]))[0]
    assert q.posted == "live" and q.params()["f_TPR"] == "r3600"


def test_live_mode_sends_only_fresh_jobs_and_skips_old_without_fetching():
    add_user(1)
    listing = {"AI Engineer": [("1", "AI Engineer", "Acme"), ("2", "AI Engineer", "Beta")]}

    class Mixed(FakeLinkedIn):
        async def search(self, query, start=0):
            jobs = await super().search(query, start)
            for j in jobs:
                j.posted = "4 minutes ago" if j.id == "1" else "52 minutes ago"
            return jobs
    run(Scanner(lambda: Mixed(listings=listing, descriptions={"1": GOOD, "2": GOOD})))
    assert [r["job_id"] for r in db.deliveries(1, ("pending",))] == ["1"]
    old = db.deliveries(1, ("rejected",))[0]
    assert old["job_id"] == "2" and old["reason_code"] == "old" and "52 minutes ago" in old["reason"]
    assert FakeLinkedIn.instances[-1].detail_calls == ["1"]       # old job: no description request


def test_wider_window_keeps_older_jobs():
    add_user(1, posted="r86400")
    run(Scanner(factory(listings=LISTING, descriptions=DESCS, posted="10 hours ago")))
    assert [r["job_id"] for r in db.deliveries(1, ("pending",))] == ["1"]


def test_unknown_age_is_not_dropped():
    add_user(1)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS, posted="")))
    assert [r["job_id"] for r in db.deliveries(1, ("pending",))] == ["1"]


def test_switching_to_live_drops_queued_old_jobs():
    add_user(1, posted="r86400")
    run(Scanner(factory(listings=LISTING, descriptions=DESCS, posted="3 hours ago")))
    assert db.deliveries(1, ("pending",))
    db.update_user(1, filters=Filters(keywords=["AI Engineer"]).to_json())   # back to live default
    assert reevaluate_user(1) == 0


def test_migration_moves_old_default_users_to_live_once():
    from app.scanner import migrate_live_default
    add_user(1, posted="r86400")
    add_user(2, posted="r604800")                 # explicitly chose a week: untouched
    assert migrate_live_default() == 1
    assert Filters.from_json(db.get_user(1)["filters"]).posted == "live"
    assert Filters.from_json(db.get_user(2)["filters"]).posted == "r604800"
    db.update_user(1, filters=Filters(keywords=["x"], posted="r86400").to_json())
    assert migrate_live_default() == 0             # only once: a later choice of 24 h is respected


def test_posted_at_stored_from_card_text():
    db.upsert_user(1, 10)
    from app.linkedin import Job
    db.upsert_cards([Job(id="7", title="t", company="c", location="l", posted="10 minutes ago")])
    assert 9.5 < db.age_minutes(db.get_job("7")["posted_at"]) < 10.5


def test_db_migration_adds_posted_at_to_old_databases():
    with db.conn() as c:
        c.executescript("DROP TABLE jobs; CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, "
                        "location TEXT, url TEXT, posted TEXT, posted_date TEXT, description TEXT, criteria TEXT, "
                        "applicants TEXT, seniority TEXT, employment_type TEXT, detail_status TEXT NOT NULL "
                        "DEFAULT 'none', detail_attempts INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL);")
    db.init()
    with db.conn() as c:
        assert "posted_at" in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}



def test_offsite_jobs_rejected_when_easy_apply_only():
    add_user(1)                                   # default: easy_apply on

    class Offsite(FakeLinkedIn):
        async def details(self, job):
            await super().details(job)
            job.easy_apply = job.id != "1"        # job 1 applies on the company site
            return job
    listing = {"AI Engineer": [("1", "AI Engineer", "Acme"), ("5", "AI Engineer", "Other")]}
    run(Scanner(lambda: Offsite(listings=listing, descriptions={"1": GOOD, "5": GOOD})))
    assert [r["job_id"] for r in db.deliveries(1, ("pending",))] == ["5"]
    assert db.deliveries(1, ("rejected",))[0]["reason_code"] == "not_easy_apply"
    assert db.get_job("5")["easy_apply"] == 1 and db.get_job("1")["easy_apply"] == 0


def test_easy_apply_off_keeps_offsite_jobs():
    add_user(1, easy_apply=False)

    class Offsite(FakeLinkedIn):
        async def details(self, job):
            await super().details(job)
            job.easy_apply = False
            return job
    run(Scanner(lambda: Offsite(listings=LISTING, descriptions=DESCS)))
    assert [r["job_id"] for r in db.deliveries(1, ("pending",))] == ["1"]


def test_migration_turns_on_easy_apply_once():
    from app.scanner import migrate_easy_apply_default
    add_user(1, easy_apply=False)
    assert migrate_easy_apply_default() == 1
    assert Filters.from_json(db.get_user(1)["filters"]).easy_apply is True
    db.update_user(1, filters=Filters(keywords=["x"], easy_apply=False).to_json())
    assert migrate_easy_apply_default() == 0   # a later choice to turn it off is respected
