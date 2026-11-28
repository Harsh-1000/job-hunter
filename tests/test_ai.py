"""Claude judge: verdict validation, the HTTP client, scanner integration, UI, admin controls, watchdog."""
import asyncio
import json

import httpx
import pytest

from app import ai, bot, db, storage, ui
from app.ai import AIClient, AIVerdict, parse_verdict
from app.config import settings
from app.filters import Filters
from app.scanner import Scanner, reevaluate_user
from tests.fakes import FakeBot, factory, make_ctx, make_update
from tests.test_scanner import DESCS, GOOD, LISTING, ME, add_user


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ai_on():
    old = settings.ai_url
    object.__setattr__(settings, "ai_url", "http://ai:8787")
    yield
    object.__setattr__(settings, "ai_url", old)


GOOD_V = {"score": 82, "verdict": "strong", "why": "Node.js + RAG match; years fit.", "missing_skills": ["Kafka"],
          "seniority_ok": True}


class FakeAI:
    """Stands in for AIClient. answers: list of AIVerdict|None returned in order (last one repeats)."""
    instances: list["FakeAI"] = []

    def __init__(self, answers=None, error="offline (ConnectError)"):
        self.answers = list(answers if answers is not None else [parse_verdict(GOOD_V)])
        self.calls, self.closed, self.error, self.last_error = [], False, error, ""
        FakeAI.instances.append(self)

    async def judge(self, body):
        self.calls.append(body)
        v = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        self.last_error = "" if v else self.error
        return v

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeAI.instances.clear()


def ai_factory(**kw):
    def make():
        return FakeAI(**kw)
    return make


def admin_user(uid=1, **flt):
    add_user(uid, **flt)
    db.update_user(uid, is_admin=1)
    storage.save_resume(uid, "cv.txt", b"x", "Node.js TypeScript RAG engineer resume text", ME.to_dict())


# ---------------- verdict validation ----------------

def test_parse_verdict_valid_and_clamped():
    v = parse_verdict({**GOOD_V, "score": 140, "missing_skills": ["A", "", 3, "B" * 99] + list("cdefgh")})
    assert v.score == 100 and v.verdict == "strong" and v.seniority_ok
    assert v.missing[0] == "A" and len(v.missing[1]) == 40 and len(v.missing) == 6
    assert parse_verdict({**GOOD_V, "score": -5}).score == 0
    assert parse_verdict({**GOOD_V, "seniority_ok": False}).seniority_ok is False


@pytest.mark.parametrize("bad", [None, "x", [], {**GOOD_V, "verdict": "maybe"}, {**GOOD_V, "score": "80"},
                                 {**GOOD_V, "score": True}, {**GOOD_V, "why": ""}, {**GOOD_V, "why": 5},
                                 {k: v for k, v in GOOD_V.items() if k != "score"}])
def test_parse_verdict_rejects_malformed(bad):
    assert parse_verdict(bad) is None


def test_parse_verdict_trims_long_why():
    v = parse_verdict({**GOOD_V, "why": "word " * 200})
    assert len(v.why) <= 240 and v.why.endswith("…") and "  " not in v.why


def test_profile_hash_depends_on_resume_targets_years():
    h = ai.profile_hash("cv", ["AI Engineer", "Backend"], 2.5)
    assert h == ai.profile_hash("cv", ["backend", "ai engineer"], 2.5)      # order / case don't matter
    assert h != ai.profile_hash("cv2", ["AI Engineer", "Backend"], 2.5)
    assert h != ai.profile_hash("cv", ["AI Engineer"], 2.5)
    assert h != ai.profile_hash("cv", ["AI Engineer", "Backend"], 3)


def test_payload_truncates_and_has_no_contact_fields():
    row = {"title": "AI Engineer", "company": "Acme", "location": "Delhi", "seniority": None,
           "employment_type": "Full-time", "description": "d" * 50_000}
    p = ai.payload(row, "r" * 50_000, ["Python"] * 99, ["AI"], 2.0)
    assert len(p["job"]["description"]) == ai.MAX_TEXT and len(p["resume"]["text"]) == ai.MAX_TEXT
    assert len(p["resume"]["skills"]) == 40 and p["job"]["seniority"] == ""
    assert set(p["resume"]) == {"text", "skills", "targets", "years"}


# ---------------- HTTP client ----------------

def client_with(handler):
    return AIClient(base_url="http://ai:8787", transport=httpx.MockTransport(handler))


def test_client_success():
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"verdict": GOOD_V})
    c = client_with(h)
    v = run(c.judge({"job": {"title": "x"}}))
    assert v.score == 82 and c.last_error == "" and seen == [{"job": {"title": "x"}}]


def test_client_retries_once_on_invalid_answer():
    answers = [httpx.Response(200, json={"verdict": {"score": "bad"}}), httpx.Response(200, json={"verdict": GOOD_V})]
    c = client_with(lambda req: answers.pop(0))
    assert run(c.judge({})).verdict == "strong" and not answers


def test_client_gives_up_after_two_bad_answers():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(502, json={"error": "claude error"})
    c = client_with(h)
    assert run(c.judge({})) is None and len(calls) == 2 and "502" in c.last_error


@pytest.mark.parametrize("status,expect", [(429, "budget"), (503, "503"), (400, "400")])
def test_client_no_retry_on_budget_busy_bad_request(status, expect):
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(status, json={"error": "nope"})
    c = client_with(h)
    assert run(c.judge({})) is None and len(calls) == 1 and expect in c.last_error


def test_client_no_login_is_not_an_outage(ai_on):
    c = client_with(lambda req: httpx.Response(503, json={"error": "no Claude login: run `jobhunter ai login`"}))
    assert run(c.judge({})) is None and c.last_error == ai.NO_LOGIN
    ai.record_status(c.last_error)
    db.set_meta("ai_failing_since", "2000-01-01 00:00:00")
    assert ai.status()["state"] == "nologin"
    app = make_ctx().application
    app.bot_data["started_at"] = 0
    db.record_cycle(scan_result().__class__(searches=1, listings=5))
    assert bot.watchdog_problem(app) == ""
    assert "not connected to Claude" in ui.AI_STATES["nologin"]


def test_client_offline_and_disabled():
    def h(req):
        raise httpx.ConnectError("down")
    c = client_with(h)
    assert run(c.judge({})) is None and c.last_error.startswith("offline")
    off = AIClient(base_url="")
    assert not off.enabled and run(off.judge({})) is None and run(off.health()) is None


def test_client_health():
    c = client_with(lambda req: httpx.Response(200, json={"ok": True, "auth": True}))
    assert run(c.health()) == {"ok": True, "auth": True}
    bad = client_with(lambda req: httpx.Response(500))
    assert run(bad.health()) is None


# ---------------- scanner integration ----------------

def scan(ai_kw=None, **kw):
    return run(Scanner(factory(listings=LISTING, descriptions=DESCS), ai_factory(**(ai_kw or {}))).run(**kw))


def test_ai_judges_keyword_matches_and_replaces_score(ai_on):
    admin_user(1)
    res = scan()
    fake = FakeAI.instances[-1]
    assert len(fake.calls) == 1 and fake.closed           # only the one keyword match is sent to Claude
    body = fake.calls[0]
    assert body["job"]["title"] == "AI Engineer" and "Node.js" in body["resume"]["text"]
    [row] = db.deliveries(1, ("pending",))
    d = json.loads(row["details"])
    assert row["score"] == 82 and d["ai"]["verdict"] == "strong" and "match" in d
    assert res.ai_judged == 1 and res.matched == 1 and "Claude" in res.summary()
    assert ai.status()["state"] == "ok"


def test_ai_skip_and_threshold_reject(ai_on):
    admin_user(1, min_score=50)
    skip = parse_verdict({**GOOD_V, "verdict": "skip", "score": 20, "why": "It's a sales role."})
    scan({"answers": [skip]})
    r = {x["job_id"]: x for x in db.deliveries(1, ("rejected",))}
    assert r["1"]["reason_code"] == "ai_skip" and "sales role" in r["1"]["reason"]
    db.set_result(1, "1", "ai_wait", 70, details={"match": {}})
    db.update_user(1, filters=Filters(keywords=["AI Engineer"], min_score=90).to_json())
    reevaluate_user(1)    # cached 'skip' verdict is reused without calling Claude
    assert db.get_delivery(1, "1")["reason_code"] in ("ai_skip", "threshold")
    low = parse_verdict({**GOOD_V, "score": 40, "verdict": "stretch"})
    db.update_user(1, filters=Filters(keywords=["AI Engineer"], min_score=50).to_json())
    with db.conn() as c:
        c.execute("DELETE FROM ai_verdicts")
    db.set_result(1, "1", "ai_wait", 70, details={"match": {}})
    run(Scanner(ai_factory=ai_factory(answers=[low])).ai_review(Scanner._users(None), scan_result()))
    d = db.get_delivery(1, "1")
    assert d["status"] == "rejected" and d["reason_code"] == "threshold" and d["score"] == 40


def scan_result():
    from app.scanner import CycleResult
    return CycleResult()


def test_ai_offline_releases_with_keyword_score_and_stops_calling(ai_on):
    admin_user(1)
    listing = {"AI Engineer": [(str(i), "AI Engineer", f"Co{i}") for i in range(1, 5)]}
    res = run(Scanner(factory(listings=listing, descriptions={str(i): GOOD for i in range(1, 5)}),
                      ai_factory(answers=[None])).run())
    fake = FakeAI.instances[-1]
    assert len(fake.calls) == 1                       # first failure: the rest aren't even tried
    rows = db.deliveries(1, ("pending",))
    assert len(rows) == 4 and all(json.loads(r["details"])["ai_offline"].startswith("offline") for r in rows)
    assert res.matched == 4 and res.ai_error.startswith("offline")
    st = ai.status()
    assert st["state"] == "offline" and st["failing_since"]


def test_no_login_releases_without_offline_tag(ai_on):
    admin_user(1)
    scan({"answers": [None], "error": ai.NO_LOGIN})
    [row] = db.deliveries(1, ("pending",))
    assert "ai_offline" not in json.loads(row["details"]) and ai.status()["state"] == "nologin"


def test_verdict_cached_per_resume_shared_between_users(ai_on):
    admin_user(1)
    admin_user(2)          # same resume text, same targets, same years -> same cache key
    scan()
    assert len(FakeAI.instances[-1].calls) == 1
    assert db.deliveries(2, ("pending",))[0]["score"] == 82


def test_not_allowed_user_gets_keyword_match_without_ai(ai_on):
    add_user(5)
    scan()
    assert not FakeAI.instances or not FakeAI.instances[-1].calls
    [row] = db.deliveries(5, ("pending",))
    assert "ai" not in json.loads(row["details"])
    db.update_user(5, ai_allowed=1)
    storage.save_resume(5, "cv.txt", b"x", "resume", ME.to_dict())
    db.set_result(5, "1", "waiting", None)
    scan()
    assert db.deliveries(5, ("pending",))[0]["score"] == 82


def test_ai_off_by_filter_or_server(ai_on):
    admin_user(1, ai_judge=False)
    scan()
    assert not FakeAI.instances or not FakeAI.instances[-1].calls
    assert db.deliveries(1, ("pending",))


def test_ai_disabled_server_wide_never_holds():
    admin_user(1)        # no ai_on fixture: AI_URL empty
    scan()
    assert db.deliveries(1, ("pending",)) and not db.deliveries(1, ("ai_wait",))


def test_rows_waiting_for_ai_released_when_ai_turned_off(ai_on):
    admin_user(1)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS), ai_factory()).run())
    db.set_result(1, "1", "ai_wait", 70, details={"match": {}})
    db.update_user(1, filters=Filters(keywords=["AI Engineer"], ai_judge=False).to_json())
    res = scan_result()
    run(Scanner(ai_factory=ai_factory()).ai_review(Scanner._users(None), res))
    assert db.get_delivery(1, "1")["status"] == "pending" and res.matched == 1


def test_per_cycle_cap_leaves_rest_for_next_cycle(ai_on):
    admin_user(1)
    object.__setattr__(settings, "ai_max_per_cycle", 2)
    try:
        listing = {"AI Engineer": [(str(i), "AI Engineer", f"Co{i}") for i in range(1, 6)]}
        run(Scanner(factory(listings=listing, descriptions={str(i): GOOD for i in range(1, 6)}), ai_factory()).run())
        assert len(FakeAI.instances[-1].calls) == 2
        assert len(db.deliveries(1, ("pending",))) == 2 and len(db.deliveries(1, ("ai_wait",))) == 3
    finally:
        object.__setattr__(settings, "ai_max_per_cycle", 20)


def test_ai_wait_rows_not_delivered(ai_on):
    admin_user(1)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS), ai_factory()).run())
    db.set_result(1, "1", "ai_wait", 70, details={"match": {}})
    b = FakeBot()
    run(bot.deliver_all(make_ctx(b).application))
    assert b.sent == []


def test_review_runs_during_linkedin_cooldown(ai_on):
    admin_user(1)
    run(Scanner(factory(listings=LISTING, descriptions=DESCS), ai_factory()).run())
    db.set_result(1, "1", "ai_wait", 70, details={"match": {}})
    with db.conn() as c:
        c.execute("DELETE FROM ai_verdicts")
    db.set_meta("cooldown_until", db.iso(db.now().replace(year=db.now().year + 1)))
    res = scan()
    assert res.skipped and db.get_delivery(1, "1")["status"] == "pending"


# ---------------- UI ----------------

def _row(details, score=82):
    return {"title": "AI Engineer", "company": "Acme", "location": "Delhi", "posted": "5 minutes ago",
            "applicants": "", "seniority": "", "employment_type": "", "score": score, "job_id": "1",
            "url": "https://x", "easy_apply": 1, "details": json.dumps(details)}


def test_job_text_shows_claude_verdict():
    t = ui.job_text(_row({"match": {"matched": ["Node.js"], "missing": ["Go"]},
                          "ai": {"score": 82, "verdict": "strong", "why": "Fits <well>", "missing": ["Kafka"]}}))
    assert "Claude: 82% · Strong fit" in t and "Fits &lt;well&gt;" in t and "Kafka" in t and "Go" not in t


def test_job_text_offline_tag_and_plain_keyword():
    t = ui.job_text(_row({"match": {}, "ai_offline": "offline"}, score=60))
    assert "Match 60%" in t and "Claude unavailable" in t
    assert "Claude" not in ui.job_text(_row({"match": {}}, score=60))


def test_alive_text_ai_line():
    base = {"verdict": "ok", "last": None, "last_age": None, "next_in": None, "cooldown": "", "error": "",
            "interval": 3, "today": {"checked": 0, "matched": 0, "sent": 0}, "uptime_h": "1 h", "paused": False,
            "keywords": ["AI"], "locations": ["Delhi"], "posted": "live", "easy_apply": True, "has_resume": True}
    assert "Claude judge" not in ui.alive_text(base)[0]
    t = ui.alive_text({**base, "ai": {"enabled": True, "state": "offline", "error": "HTTP 503"}})[0]
    assert "Claude judge: ⚠️ offline" in t and "HTTP 503" in t


def test_filters_summary_and_button():
    assert "Claude judges the fit: On" in ui.filters_text(Filters(), None)
    labels = [b.text for r in ui.filters_kb(Filters(ai_judge=False)).inline_keyboard for b in r]
    assert "🤖 Claude judges the fit: off" in labels


# ---------------- bot: toggle, /ai, watchdog ----------------

def test_toggle_ai_filter_warns_when_not_allowed(ai_on):
    add_user(3)
    off = make_update(uid=3, data="f:ai")
    run(bot.on_filter_button(off, make_ctx()))
    assert Filters.from_json(db.get_user(3)["filters"]).ai_judge is False
    assert off.callback_query.answer.call_args.kwargs["show_alert"] is False
    on = make_update(uid=3, data="f:ai")
    run(bot.on_filter_button(on, make_ctx()))
    assert Filters.from_json(db.get_user(3)["filters"]).ai_judge is True
    args = on.callback_query.answer.call_args
    assert "waiting for the admin" in args.args[0] and args.kwargs["show_alert"] is True
    db.update_user(3, ai_allowed=1)
    for _ in range(2):
        upd = make_update(uid=3, data="f:ai")
        run(bot.on_filter_button(upd, make_ctx()))
    assert upd.callback_query.answer.call_args.kwargs["show_alert"] is False


def test_ai_command_admin_only_and_allow_block(ai_on):
    admin_user(1)
    add_user(7)
    b = FakeBot()
    run(bot.cmd_ai(make_update(uid=7), make_ctx(b, args=["allow", "7"])))
    assert db.get_user(7)["ai_allowed"] == 0              # not an admin: refused
    run(bot.cmd_ai(make_update(uid=1), make_ctx(b, args=["allow", "7"])))
    assert db.get_user(7)["ai_allowed"] == 1 and "allowed" in b.sent[-1].text
    run(bot.cmd_ai(make_update(uid=1), make_ctx(b, args=["block", "7"])))
    assert db.get_user(7)["ai_allowed"] == 0


def test_ai_command_status_when_server_ai_off():
    admin_user(1)
    b = FakeBot()
    run(bot.cmd_ai(make_update(uid=1), make_ctx(b)))
    assert "off" in b.sent[-1].text and "jobhunter ai login" in b.sent[-1].text


def test_watchdog_flags_ai_down_for_30_min(ai_on):
    app = make_ctx().application
    app.bot_data["started_at"] = 0
    db.record_cycle(scan_result().__class__(searches=1, listings=5))
    assert bot.watchdog_problem(app) == ""
    ai.record_status("offline (ConnectError)")
    assert bot.watchdog_problem(app) == ""                # just started failing
    db.set_meta("ai_failing_since", db.iso(db.now().replace(microsecond=0) - __import__("datetime").timedelta(minutes=31)))
    assert "Claude judge has been offline for 31 min" in bot.watchdog_problem(app)
    db.set_meta("ai_last_error", "over hourly budget")    # budget isn't an outage
    assert bot.watchdog_problem(app) == ""
    ai.record_status("")
    assert ai.status()["state"] == "ok" and not db.get_meta("ai_failing_since")


# ---------------- db ----------------

def test_ai_allowed_column_added_to_old_db(tmp_path):
    import sqlite3
    with db.conn() as c:
        c.execute("DROP TABLE users")
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, first_name TEXT, "
                  "username TEXT, status TEXT NOT NULL DEFAULT 'pending', is_admin INTEGER NOT NULL DEFAULT 0, "
                  "filters TEXT, resume TEXT, paused INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
                  "last_seen TEXT, last_manual_scan TEXT)")
    db.init()
    db.upsert_user(9, 90, "x")
    assert db.get_user(9)["ai_allowed"] == 0
    assert isinstance(db.get_user(9), sqlite3.Row)


def test_ai_verdict_cache_and_prune():
    db.put_ai_verdict("1", "h", GOOD_V)
    assert db.get_ai_verdict("1", "h") == GOOD_V and db.get_ai_verdict("1", "other") is None
    assert db.ai_calls_since(1) == 1
    with db.conn() as c:
        c.execute("UPDATE ai_verdicts SET ts='2000-01-01 00:00:00'")
    db.prune()
    assert db.get_ai_verdict("1", "h") is None


def test_ai_verdict_roundtrip():
    v = AIVerdict(score=50, verdict="good", why="ok", missing=["Go"])
    assert AIVerdict.from_dict({**v.to_dict(), "unknown": 1}) == v and AIVerdict.from_dict(None) is None
