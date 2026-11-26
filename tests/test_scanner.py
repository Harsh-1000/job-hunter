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
