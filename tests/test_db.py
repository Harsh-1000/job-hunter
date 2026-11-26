from datetime import timedelta

import pytest

from app import db
from app.linkedin import Job


def job(i, title="AI Engineer", company="Acme"):
    return Job(id=str(i), title=title, company=company, location="India")


def test_user_lifecycle():
    u = db.upsert_user(5, 50, "Asha", "asha")
    assert u["status"] == "pending" and u["is_admin"] == 0
    db.update_user(5, status="active", paused=1)
    assert db.get_user(5)["paused"] == 1 and db.count_users("active") == 1
    db.upsert_user(5, 51, "Asha V", "asha")          # refresh keeps status, updates chat/name
    u = db.get_user(5)
    assert u["status"] == "active" and u["chat_id"] == 51 and u["first_name"] == "Asha V"
    with pytest.raises(ValueError):
        db.update_user(5, id=6)


def test_delete_user_removes_everything():
    db.upsert_user(5, 50)
    db.upsert_cards([job(1)])
    db.add_waiting(5, ["1"], "kw")
    db.track_message(50, 10, "job")
    db.attach_query(5, "q")
    db.delete_user(5)
    assert db.get_user(5) is None and db.deliveries(5, ("waiting",)) == [] and db.tracked_messages(50) == []
    assert db.attach_query(5, "q") is True


def test_cards_and_details():
    db.upsert_cards([job(1), job(1)])
    j = job(1)
    j.description, j.applicants, j.criteria = "desc", "35 applicants", {"Seniority level": "Entry"}
    db.save_details(j)
    row = db.get_job("1")
    assert row["description"] == "desc" and row["detail_status"] == "ok"


def test_detail_failed_after_three_attempts():
    db.upsert_cards([job(1)])
    assert [db.detail_failed("1") for _ in range(3)] == [1, 2, 3]
    assert db.get_job("1")["detail_status"] == "failed"


def test_query_seen_and_runs():
    db.mark_query_seen("q", ["1", "2"])
    assert db.query_seen_ids("q", ["1", "3"]) == {"1"} and db.query_seen_ids("other", ["1"]) == set()
    assert db.query_runs("q") == 0
    db.query_ran("q")
    db.query_ran("q")
    assert db.query_runs("q") == 2
    assert set(db.query_seen_since("q", 1)) == {"1", "2"}


def test_delivery_flow_and_ordering():
    db.upsert_user(5, 50)
    db.upsert_cards([job(1), job(2), job(3)])
    assert db.add_waiting(5, ["1", "2", "3"], "kw") == 3
    assert db.add_waiting(5, ["1"], "kw") == 0          # never twice
    db.set_result(5, "1", "pending", 40)
    db.set_result(5, "2", "pending", 90)
    db.set_result(5, "3", "rejected", 10, "low", "threshold")
    assert [r["job_id"] for r in db.deliveries(5, ("pending",))] == ["2", "1"]
    db.mark_sent(5, "2", 777)
    assert db.get_delivery(5, "2")["message_id"] == 777
    assert db.set_status(5, "2", "applied") and not db.set_status(5, "nope", "applied")
    s = db.user_stats(5)
    assert s["applied"] == 1 and s["pending"] == 1 and s["rejected"] == 1 and s["sent_today"] == 1


def test_duplicate_same_title_company():
    db.upsert_user(5, 50)
    db.upsert_cards([job(1, "AI Engineer", "Acme"), job(2, "ai engineer", "ACME"), job(3, "AI Engineer", "Other")])
    db.add_waiting(5, ["1", "2", "3"], "kw")
    assert not db.is_duplicate(5, "2", "ai engineer", "ACME")
    db.set_result(5, "1", "pending", 50)
    assert db.is_duplicate(5, "2", "ai engineer", "ACME")
    assert not db.is_duplicate(5, "3", "AI Engineer", "Other")


def test_tracked_messages_respect_48h_and_kinds():
    db.track_message(50, 1, "ui")
    db.track_message(50, 2, "job")
    db.track_message(50, 3, "user")
    with db.conn() as c:
        c.execute("UPDATE messages SET ts=? WHERE message_id=3", (db.iso(db.now() - timedelta(hours=49)),))
    assert db.tracked_messages(50) == [1, 2]
    assert db.tracked_messages(50, keep_kinds=("job",)) == [1]
    db.forget_messages(50, [1])
    assert db.tracked_messages(50) == [2]


def test_prune_keeps_applied_and_saved():
    db.upsert_user(5, 50)
    db.upsert_cards([job(1), job(2)])
    db.add_waiting(5, ["1", "2"], "kw")
    db.set_status(5, "1", "applied")
    old = db.iso(db.now() - timedelta(days=40))
    with db.conn() as c:
        c.execute("UPDATE deliveries SET created_at=?", (old,))
        c.execute("UPDATE jobs SET first_seen=?", (old,))
    db.prune()
    assert db.get_delivery(5, "1") and db.get_delivery(5, "2") is None
    assert db.get_job("1") and db.get_job("2") is None


def test_meta():
    assert db.get_meta("x", "d") == "d"
    db.set_meta("x", "1")
    db.set_meta("x", "2")
    assert db.get_meta("x") == "2"
