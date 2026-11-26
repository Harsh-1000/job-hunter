import asyncio
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden

from app import bot, db, ui
from app.config import settings
from app.filters import Filters
from app.linkedin import Job
from tests.conftest import SAMPLE_RESUME
from tests.fakes import FakeBot, make_ctx, make_update


def run(coro):
    return asyncio.run(coro)


def active(uid=1, admin=False, **flt):
    db.upsert_user(uid, uid * 10, f"u{uid}")
    db.update_user(uid, status="active", is_admin=int(admin), filters=Filters(**flt).to_json())


def pending_job(uid, jid="1", score=80, title="AI <Engineer>", company="Acme & Co"):
    db.upsert_cards([Job(id=jid, title=title, company=company, location="India", posted="1 hour ago")])
    db.add_waiting(uid, [jid], "AI Engineer")
    db.set_result(uid, jid, "pending", score, details={"match": {"matched": ["Node.js"], "missing": ["Python"],
                                                                 "years_text": "2-4 yrs"}, "keyword": "AI Engineer"})


# ---------------- access ----------------

def test_first_user_becomes_admin_next_waits_for_approval():
    ctx = make_ctx()
    run(bot.cmd_start(make_update(uid=1), ctx))
    assert db.get_user(1)["is_admin"] == 1 and db.get_user(1)["status"] == "active"
    run(bot.cmd_start(make_update(uid=2, first_name="Ravi", username="ravi"), ctx))
    assert db.get_user(2)["status"] == "pending"
    admin_msgs = [m for m in ctx.bot.sent if m.chat_id == 10 and "Access request" in m.text]
    assert admin_msgs and "a:approve:2" in str(admin_msgs[0].kw["reply_markup"].to_dict())


def test_open_mode_activates(monkeypatch):
    monkeypatch.setattr(bot, "settings", dataclasses.replace(settings, access_mode="open"))
    active(99, admin=True)
    run(bot.cmd_start(make_update(uid=3), make_ctx()))
    assert db.get_user(3)["status"] == "active"


def test_pending_user_cannot_use_features():
    active(1, admin=True)
    db.upsert_user(2, 20)
    ctx = make_ctx()
    run(bot.cmd_filters(make_update(uid=2), ctx))
    assert "waiting for the admin" in ctx.bot.sent[-1].text


def test_admin_approves_and_user_is_notified():
    active(1, admin=True)
    db.upsert_user(2, 20, "Ravi")
    ctx = make_ctx()
    run(bot.on_admin_button(make_update(uid=1, data="a:approve:2"), ctx))
    assert db.get_user(2)["status"] == "active"
    assert any(m.chat_id == 20 and "approved" in m.text for m in ctx.bot.sent)


def test_non_admin_cannot_approve():
    active(1, admin=True)
    active(2)
    db.upsert_user(3, 30)
    run(bot.cmd_approve(make_update(uid=2), make_ctx(args=["3"])))
    assert db.get_user(3)["status"] == "pending"


def test_admin_cannot_be_blocked():
    active(1, admin=True)
    assert "Can't block" in run(bot.set_access(make_ctx(), 1, "blocked"))


# ---------------- resume upload ----------------

def doc(data: bytes, name="cv.txt", size=None):
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(data)))
    return SimpleNamespace(file_name=name, file_size=size or len(data), get_file=AsyncMock(return_value=file))


def test_resume_upload_parses_saves_and_sets_keywords():
    active(1, keywords=[])
    ctx = make_ctx()
    run(bot.on_document(make_update(uid=1, document=doc(SAMPLE_RESUME.encode())), ctx))
    u = db.get_user(1)
    profile = json.loads(u["resume"])
    assert "RAG" in profile["skills"] and profile["years"] is not None
    assert Filters.from_json(u["filters"]).keywords[:2] == ["AI Engineer", "Generative AI Engineer"]
    assert (settings.users_dir / "1" / "resume.txt").exists()
    assert "Your resume" in ctx.bot.sent[-1].text


def test_resume_upload_keeps_existing_keywords():
    active(1, keywords=["Java Developer"])
    run(bot.on_document(make_update(uid=1, document=doc(SAMPLE_RESUME.encode())), make_ctx()))
    assert Filters.from_json(db.get_user(1)["filters"]).keywords == ["Java Developer"]


def test_resume_bad_file_and_too_big():
    active(1)
    ctx = make_ctx()
    run(bot.on_document(make_update(uid=1, document=doc(b"x", "virus.exe")), ctx))
    ctx.bot.sent[-1].edit_text.assert_awaited()
    assert "PDF, DOCX or TXT" in ctx.bot.sent[-1].edit_text.await_args.args[0]
    run(bot.on_document(make_update(uid=1, document=doc(b"x", "cv.pdf", size=50 * 1024 * 1024)), ctx))
    assert "too big" in ctx.bot.sent[-1].text
    assert db.get_user(1)["resume"] is None


def test_delete_resume_button():
    active(1)
    run(bot.on_document(make_update(uid=1, document=doc(SAMPLE_RESUME.encode())), make_ctx()))
    run(bot.on_resume_button(make_update(uid=1, data="r:delete"), make_ctx()))
    assert db.get_user(1)["resume"] is None and not (settings.users_dir / "1").exists()


# ---------------- filters ----------------

@pytest.mark.parametrize("key,text,check", [
    ("kw", "AI Engineer, GenAI Engineer, LLM Engineer", lambda f: f.keywords == ["AI Engineer", "GenAI Engineer",
                                                                                 "LLM Engineer"]),
    ("loc", "Bengaluru, Remote", lambda f: f.locations == ["Bengaluru", "Remote"]),
    ("excl", "Senior, Lead", lambda f: f.exclude_words == ["senior", "lead"]),
    ("excl", "none", lambda f: f.exclude_words == []),
    ("comp", "Foo Inc, Bar", lambda f: f.exclude_companies == ["Foo Inc", "Bar"]),
    ("apps", "100", lambda f: f.max_applicants == 100),
    ("quiet", "23-8", lambda f: (f.quiet_start, f.quiet_end) == (23, 8)),
    ("myy", "3.5", lambda f: f.my_years == 3.5),
    ("myy", "auto", lambda f: f.my_years is None),
    ("thr", "20", lambda f: f.min_score == 20),
])
def test_apply_text_setting(key, text, check):
    f = Filters()
    bot.apply_text_setting(f, key, text)
    assert check(f)


@pytest.mark.parametrize("key,text", [("kw", " , "), ("thr", "150"), ("apps", "lots"), ("quiet", "99-1")])
def test_apply_text_setting_errors(key, text):
    with pytest.raises(ValueError):
        bot.apply_text_setting(Filters(), key, text)


def test_keyword_button_opens_titles_menu():
    active(1, keywords=["AI Engineer", "Backend Developer"])
    upd = make_update(uid=1, data="f:kw")
    run(bot.on_filter_button(upd, make_ctx()))
    text = upd.callback_query.edit_message_text.await_args.args[0]
    kb = upd.callback_query.edit_message_text.await_args.kwargs["reply_markup"].to_dict()["inline_keyboard"]
    assert "Job titles" in text and "AI Engineer" in text and "Backend Developer" in text
    datas = [b["callback_data"] for row in kb for b in row]
    assert {"f:kwrm:0", "f:kwrm:1", "f:kwadd", "f:kwset", "f:menu"} <= set(datas)


def test_add_titles_flow_appends_not_replaces():
    active(1, keywords=["AI Engineer"])
    ctx = make_ctx()
    run(bot.on_filter_button(make_update(uid=1, data="f:kwadd"), ctx))
    assert ctx.user_data["awaiting"] == "kwadd"
    run(bot.on_text(make_update(uid=1, text="ML Engineer, ai engineer, Python Developer"), ctx))
    assert Filters.from_json(db.get_user(1)["filters"]).keywords == ["AI Engineer", "ML Engineer", "Python Developer"]
    assert "Added: ML Engineer, Python Developer" in ctx.bot.sent[-1].text and "awaiting" not in ctx.user_data
    assert "f:kwrm:2" in str(ctx.bot.sent[-1].kw["reply_markup"].to_dict())   # back on the titles menu


def test_replace_all_titles():
    active(1, keywords=["AI Engineer"])
    ctx = make_ctx()
    run(bot.on_filter_button(make_update(uid=1, data="f:kwset"), ctx))
    assert ctx.user_data["awaiting"] == "kw"
    run(bot.on_text(make_update(uid=1, text="Java Developer, Backend Developer"), ctx))
    assert Filters.from_json(db.get_user(1)["filters"]).keywords == ["Java Developer", "Backend Developer"]


def test_remove_title_button():
    active(1, keywords=["AI Engineer", "Backend Developer", "LLM Engineer"])
    run(bot.on_filter_button(make_update(uid=1, data="f:kwrm:1"), make_ctx()))
    assert Filters.from_json(db.get_user(1)["filters"]).keywords == ["AI Engineer", "LLM Engineer"]
    run(bot.on_filter_button(make_update(uid=1, data="f:kwrm:9"), make_ctx()))   # stale index: no-op
    assert len(Filters.from_json(db.get_user(1)["filters"]).keywords) == 2


def test_add_resume_suggestions_button():
    active(1, keywords=["Java Developer"])
    run(bot.on_document(make_update(uid=1, document=doc(SAMPLE_RESUME.encode())), make_ctx()))
    run(bot.on_filter_button(make_update(uid=1, data="f:kwsug"), make_ctx()))
    kws = Filters.from_json(db.get_user(1)["filters"]).keywords
    assert kws[0] == "Java Developer" and "AI Engineer" in kws and len(kws) <= 10


def test_titles_limit():
    from app.filters import MAX_KEYWORDS
    f = Filters(keywords=[f"T{i}" for i in range(MAX_KEYWORDS)])
    with pytest.raises(ValueError, match="max"):
        bot.apply_text_setting(f, "kwadd", "New One")
    f = Filters(keywords=[f"T{i}" for i in range(MAX_KEYWORDS - 1)])
    msg = bot.apply_text_setting(f, "kwadd", "A, B")
    assert f.keywords[-1] == "A" and "skipped" in msg and len(f.keywords) == MAX_KEYWORDS


def test_addtitle_and_titles_commands():
    active(1, keywords=["AI Engineer"])
    run(bot._quick_setting("kwadd", "")(make_update(uid=1), make_ctx(args=["ML", "Engineer,", "SDE"])))
    assert Filters.from_json(db.get_user(1)["filters"]).keywords == ["AI Engineer", "ML Engineer", "SDE"]
    ctx = make_ctx()
    run(bot.cmd_titles(make_update(uid=1), ctx))
    assert "ML Engineer" in ctx.bot.sent[-1].text
    names = [c for c, _ in bot.COMMANDS]
    assert "titles" in names and "addtitle" in names


def test_network_errors_logged_as_one_line(caplog):
    from telegram.error import NetworkError
    ctx = SimpleNamespace(error=NetworkError("Temporary failure in name resolution"))
    with caplog.at_level("WARNING"):
        run(bot.on_error(None, ctx))
    rec = [r for r in caplog.records if "network problem" in r.message]
    assert rec and rec[0].levelname == "WARNING" and rec[0].exc_info is None


def test_bad_text_keeps_waiting():
    active(1)
    ctx = make_ctx()
    ctx.user_data["awaiting"] = "thr"
    run(bot.on_text(make_update(uid=1, text="lots"), ctx))
    assert ctx.user_data["awaiting"] == "thr" and "⚠️" in ctx.bot.sent[-1].text


def test_toggle_buttons():
    active(1)
    for data in ("f:exp:1", "f:exp:2", "f:wt:2", "f:jt:F", "f:posted:r3600", "f:thr:30", "f:maxy:any", "f:ea"):
        run(bot.on_filter_button(make_update(uid=1, data=data), make_ctx()))
    f = Filters.from_json(db.get_user(1)["filters"])
    assert f.experience_levels == ["3", "4", "1"] and f.work_types == ["2"] and f.job_types == ["F"]
    assert f.posted == "r3600" and f.min_score == 30 and f.max_years == -1
    assert f.easy_apply is False            # default is ON, one tap turns it off
    run(bot.on_filter_button(make_update(uid=1, data="f:exp:any"), make_ctx()))
    assert Filters.from_json(db.get_user(1)["filters"]).experience_levels == []


def test_reset_keeps_keywords():
    active(1, keywords=["AI Engineer"], min_score=50)
    run(bot.on_filter_button(make_update(uid=1, data="f:reset"), make_ctx()))
    f = Filters.from_json(db.get_user(1)["filters"])
    assert f.keywords == ["AI Engineer"] and f.min_score == 0


def test_quick_threshold_command():
    active(1)
    handler = bot._quick_setting("thr", "/threshold 20")
    run(handler(make_update(uid=1), make_ctx(args=["25"])))
    assert Filters.from_json(db.get_user(1)["filters"]).min_score == 25


# ---------------- job messages ----------------

def test_job_text_escapes_html_and_shows_match():
    active(1)
    pending_job(1)
    row = db.deliveries(1, ("pending",))[0]
    text = ui.job_text(row)
    assert "AI &lt;Engineer&gt;" in text and "Acme &amp; Co" in text and "Match 80%" in text
    assert "Node.js" in text and "Python" in text
    kb = ui.job_kb(row).to_dict()["inline_keyboard"]
    assert kb[0][0]["url"].endswith("/jobs/view/1/")
    assert [b["callback_data"] for b in kb[1]] == ["j:applied:1", "j:saved:1", "j:hidden:1"]


def test_job_text_without_score_asks_for_resume():
    active(1)
    pending_job(1, score=None)
    assert "send your resume" in ui.job_text(db.deliveries(1, ("pending",))[0])


def test_job_buttons_change_status_and_undo():
    active(1)
    pending_job(1)
    db.mark_sent(1, "1", 5)
    upd = make_update(uid=1, data="j:applied:1")
    run(bot.on_job_button(upd, make_ctx()))
    assert db.get_delivery(1, "1")["status"] == "applied"
    assert "Applied" in upd.callback_query.edit_message_text.await_args.args[0]
    run(bot.on_job_button(make_update(uid=1, data="j:sent:1"), make_ctx()))
    assert db.get_delivery(1, "1")["status"] == "sent"


def test_cannot_touch_other_users_job():
    active(1)
    active(2)
    pending_job(1)
    run(bot.on_job_button(make_update(uid=2, data="j:applied:1"), make_ctx()))
    assert db.get_delivery(1, "1")["status"] == "pending"


# ---------------- delivery ----------------

def app_with(fake_bot):
    return SimpleNamespace(bot=fake_bot, bot_data={})


def test_deliver_sends_best_first_and_tracks():
    active(1)
    pending_job(1, "1", 40)
    pending_job(1, "2", 90, title="Backend Developer")
    fb = FakeBot()
    assert run(bot.deliver_all(app_with(fb))) == 2
    assert "Match 90%" in fb.sent[0].text
    assert db.get_delivery(1, "2")["status"] == "sent" and db.get_delivery(1, "2")["message_id"] == fb.sent[0].message_id
    assert db.tracked_messages(10, keep_kinds=("job",)) == []      # tracked as job alerts


def test_deliver_respects_pause_quiet_hours_and_cap(monkeypatch):
    active(1)
    for i in range(15):
        pending_job(1, str(i), 50, title=f"Role {i}", company=f"C{i}")
    fb = FakeBot()
    db.update_user(1, paused=1)
    assert run(bot.deliver_all(app_with(fb))) == 0
    db.update_user(1, paused=0)
    db.update_user(1, filters=Filters(quiet_start=0, quiet_end=0).to_json())  # equal = off
    assert run(bot.deliver_all(app_with(fb))) == 10                           # MAX_PER_CYCLE
    db.update_user(1, filters=Filters(quiet_start=0, quiet_end=23).to_json())
    monkeypatch.setattr(bot, "datetime", SimpleNamespace(now=lambda tz=None: SimpleNamespace(hour=3)))
    assert run(bot.deliver_all(app_with(fb))) == 0


def test_user_who_blocked_bot_gets_paused():
    active(1)
    pending_job(1)
    fb = FakeBot()
    fb.fail_with = Forbidden("bot was blocked by the user")
    run(bot.deliver_all(app_with(fb)))
    assert db.get_user(1)["paused"] == 1 and db.get_delivery(1, "1")["status"] == "pending"


# ---------------- clear chat ----------------

def test_clear_chat_all_and_keep_jobs():
    db.track_message(10, 1, "ui")
    db.track_message(10, 2, "job")
    db.track_message(10, 3, "user")
    fb = FakeBot()
    assert run(bot.clear_chat(fb, 10, keep_jobs=True)) == 2
    assert fb.deleted == [(10, [1, 3])] and db.tracked_messages(10) == [2]
    assert run(bot.clear_chat(fb, 10, keep_jobs=False)) == 1
    assert db.tracked_messages(10) == []


def test_clear_chat_chunks_of_100():
    for i in range(250):
        db.track_message(10, i, "ui")
    fb = FakeBot()
    run(bot.clear_chat(fb, 10, keep_jobs=False))
    assert [len(ids) for _, ids in fb.deleted] == [100, 100, 50]


def test_clear_button_sends_fresh_menu():
    active(1)
    db.track_message(10, 7, "ui")
    ctx = make_ctx()
    run(bot.on_clear_button(make_update(uid=1, data="c:all"), ctx))
    assert ctx.bot.deleted == [(10, [7])] and "Chat cleared" in ctx.bot.sent[-1].text


def test_send_tracks_messages():
    fb = FakeBot()
    m = run(bot.send(fb, 10, "hi", kind="ui"))
    assert db.tracked_messages(10) == [m.message_id]


# ---------------- misc ----------------

def test_deleteme_needs_confirm_then_wipes():
    active(1)
    run(bot.on_document(make_update(uid=1, document=doc(SAMPLE_RESUME.encode())), make_ctx()))
    run(bot.cmd_deleteme(make_update(uid=1), make_ctx()))
    assert db.get_user(1)
    run(bot.cmd_deleteme(make_update(uid=1), make_ctx(args=["confirm"])))
    assert db.get_user(1) is None and not (settings.users_dir / "1").exists()


def test_manual_scan_cooldown():
    active(1, keywords=["AI Engineer"])
    db.update_user(1, last_manual_scan=db.iso())
    ctx = make_ctx()
    run(bot.cmd_scan(make_update(uid=1), ctx))
    assert "just scanned" in ctx.bot.sent[-1].text


def test_scan_without_keywords():
    active(1, keywords=[])
    ctx = make_ctx()
    run(bot.cmd_scan(make_update(uid=1), ctx))
    assert "keywords first" in ctx.bot.sent[-1].text


def test_insights():
    active(1)
    pending_job(1, "1", 60)
    pending_job(1, "2", 80, title="Other", company="Beta")
    text = ui.insights_text(db.deliveries(1, ("pending",)))
    assert "Average match: <b>70%</b>" in text and "Python — 2 jobs" in text


def test_build_registers_handlers():
    app = bot.build()
    assert sum(len(h) for h in app.handlers.values()) > 30


def test_every_menu_button_has_a_handler():
    app = bot.build()
    wired = " ".join(str(h.filters) for hs in app.handlers.values() for h in hs if hasattr(h, "filters"))
    for label in ui.MENU_BUTTONS:
        assert label in wired, label


# ---------------- intro / tour ----------------

def test_intro_pages_cover_every_feature():
    text = " ".join(ui.intro_text(i) for i in range(len(ui.INTRO_PAGES)))
    for feature in ("resume", "Filters", "Keywords", "Location", "Date posted", "Experience level", "Job type",
                    "Remote", "Easy Apply", "threshold", "match %", "Applied", "Save", "Hide", "Insights",
                    "Pause", "Clear chat", "/deleteme", "/years", "Quiet hours"):
        assert feature.lower() in text.lower(), feature
    for label in ui.MENU_BUTTONS:          # every menu button is explained
        assert label.split(" ", 1)[1].split(" /")[0] in text, label


def test_intro_keyboard_navigation():
    last = len(ui.INTRO_PAGES) - 1
    first = [b["callback_data"] for b in ui.intro_kb(0).to_dict()["inline_keyboard"][0]]
    assert first == ["i:0", "i:1"]                                   # no Back on page 1
    middle = [b["callback_data"] for b in ui.intro_kb(2).to_dict()["inline_keyboard"][0]]
    assert middle == ["i:1", "i:2", "i:3"]
    end = [b["callback_data"] for b in ui.intro_kb(last).to_dict()["inline_keyboard"][0]]
    assert end[-1] == "i:done"
    assert ui.intro_text(99) == ui.intro_text(last) and ui.intro_text(-5) == ui.intro_text(0)


def test_intro_works_even_before_approval():
    active(1, admin=True)
    db.upsert_user(2, 20)                  # pending
    ctx = make_ctx()
    run(bot.cmd_intro(make_update(uid=2), ctx))
    assert "What is Job Hunter" in ctx.bot.sent[-1].text
    assert ctx.bot.sent[-1].kw["reply_markup"].to_dict()["inline_keyboard"][0][-1]["callback_data"] == "i:1"


def test_intro_button_pages_and_done():
    active(1)
    upd = make_update(uid=1, data="i:3")
    run(bot.on_intro_button(upd, make_ctx()))
    assert "match %" in upd.callback_query.edit_message_text.await_args.args[0]
    upd = make_update(uid=1, data="i:done")
    run(bot.on_intro_button(upd, make_ctx()))
    assert "Send your resume" in upd.callback_query.edit_message_text.await_args.args[0]
    upd = make_update(uid=1, data="i:junk")
    run(bot.on_intro_button(upd, make_ctx()))
    assert "What is Job Hunter" in upd.callback_query.edit_message_text.await_args.args[0]


def test_start_and_help_offer_the_tour():
    ctx = make_ctx()
    run(bot.cmd_start(make_update(uid=1), ctx))
    assert any("i:0" in str(m.kw.get("reply_markup").to_dict()) for m in ctx.bot.sent
               if hasattr(m.kw.get("reply_markup"), "inline_keyboard"))
    run(bot.cmd_help(make_update(uid=1), ctx))
    assert "/intro" in ctx.bot.sent[-1].text and "i:0" in str(ctx.bot.sent[-1].kw["reply_markup"].to_dict())


def test_intro_in_command_list():
    assert "intro" in [c for c, _ in bot.COMMANDS]


# ---------------- log retention ----------------

def test_clean_old_logs_keeps_last_12_hours(tmp_path):
    import os
    import time
    now = time.time()
    files = {"bot.log": now - 99 * 3600, "bot.log.2026-11-27_01": now - 13 * 3600,
             "bot.log.2026-11-27_10": now - 2 * 3600, "old.txt": now - 48 * 3600}
    for name, mtime in files.items():
        p = tmp_path / name
        p.write_text("x")
        os.utime(p, (mtime, mtime))
    removed = bot.clean_old_logs(tmp_path, hours=12)
    assert sorted(removed) == ["bot.log.2026-11-27_01", "old.txt"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bot.log", "bot.log.2026-11-27_10"]
    assert bot.clean_old_logs(tmp_path / "missing") == []


def test_log_retention_default_is_12h():
    assert settings.log_retention_hours == 12


def test_file_logging_rotates_hourly_and_keeps_12(monkeypatch):
    import logging
    import main
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        main.setup_logging()
        fh = [h for h in root.handlers if h not in before and hasattr(h, "backupCount")][0]
        assert fh.when == "H" and fh.interval == 3600 and fh.backupCount == 12
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_docker_logs_are_capped():
    from tests.conftest import ROOT
    path = ROOT / "docker-compose.yml"
    if not path.exists():
        pytest.skip("repo-level file, not copied into the Docker image (checked by hooks and CI)")
    compose = path.read_text()
    assert 'max-size: "1m"' in compose and 'max-file: "2"' in compose and "restart: unless-stopped" in compose


def test_delivery_runs_while_scan_in_progress_and_never_double_sends():
    active(1)
    for i in range(3):
        pending_job(1, str(i), 50, title=f"Role {i}", company=f"C{i}")
    fb = FakeBot()
    app = app_with(fb)

    async def go():
        scan_lock = bot._lock(app)
        async with scan_lock:                              # a long scan is running…
            await bot.deliver_job(SimpleNamespace(application=app))   # …delivery still happens
        return await asyncio.gather(bot.deliver_all(app), bot.deliver_all(app))
    run(go())
    assert len(fb.sent) == 3 and len({m.text for m in fb.sent}) == 3



# ---------------- ⚡ Easy Apply ----------------

def test_easy_apply_badge_and_button():
    active(1)
    pending_job(1)
    with db.conn() as c:
        c.execute("UPDATE jobs SET easy_apply=1 WHERE id='1'")
    row = db.deliveries(1, ("pending",))[0]
    assert "Easy Apply" in ui.job_text(row)
    assert ui.job_kb(row).to_dict()["inline_keyboard"][0][0]["text"] == "⚡ Easy Apply on LinkedIn"
    with db.conn() as c:
        c.execute("UPDATE jobs SET easy_apply=0 WHERE id='1'")
    row = db.deliveries(1, ("pending",))[0]
    assert "company's website" in ui.job_text(row)
    assert ui.job_kb(row).to_dict()["inline_keyboard"][0][0]["text"] == "🔗 Open on LinkedIn"


def test_intro_mentions_easy_apply_default():
    text = " ".join(ui.intro_text(i) for i in range(len(ui.INTRO_PAGES)))
    assert "Easy Apply only: <b>ON by default</b>" in text
