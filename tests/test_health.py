"""'Is it working?', cycle recording, in-bot watchdog, daily report, server watchdog script."""
import asyncio
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import bot, db, ui
from app.filters import Filters
from app.scanner import CycleResult
from tests.conftest import ROOT
from tests.fakes import FakeBot, make_ctx, make_update
from tests.test_bot import active, pending_job


def run(coro):
    return asyncio.run(coro)


def cycle(minutes_ago=0, error="", searches=6, listings=18, new=2):
    db.record_cycle(CycleResult(searches=searches, listings=listings, new_jobs=new, matched=1, error=error))
    if minutes_ago:
        with db.conn() as c:
            c.execute("UPDATE cycles SET ts=? WHERE rowid=(SELECT MAX(rowid) FROM cycles)",
                      (db.iso(db.now() - timedelta(minutes=minutes_ago)),))


def app_obj(uptime_min=60):
    return SimpleNamespace(bot=FakeBot(), bot_data={"started_at": time.time() - uptime_min * 60},
                           job_queue=SimpleNamespace(get_jobs_by_name=lambda n: []))


# ---------------- verdict ----------------

@pytest.mark.parametrize("age,err,cool,uptime,verdict", [
    (2, "", "", 60, "ok"), (9, "", "", 60, "ok"), (11, "", "", 60, "down"),
    (2, "network error", "", 60, "slow"), (2, "", "2026-11-27 10:00:00", 60, "slow"),
    (None, "", "", 3, "starting"), (None, "", "", 60, "down"),
])
def test_health_verdict(age, err, cool, uptime, verdict):
    assert ui.health_verdict(age, err, cool, 3, uptime) == verdict


# ---------------- cycles in the database ----------------

def test_record_and_read_cycles():
    assert db.last_cycle() is None
    cycle(10, listings=5)
    cycle(0, error="network error: boom")
    assert db.last_cycle()["error"].startswith("network") and db.last_cycle(ok_only=True)["listings"] == 5
    s = db.cycles_since(1)
    assert s == {"cycles": 2, "listings": 23, "new_jobs": 4, "errors": 1}


def test_skipped_cycles_are_not_errors():
    db.record_cycle(CycleResult(skipped="no active users with keywords"))
    assert db.last_cycle()["error"] == "" and db.cycles_since(1)["errors"] == 0


def test_cycles_pruned_after_two_days():
    cycle(60 * 24 * 3)
    cycle(5)
    db.prune()
    assert db.cycles_since(24 * 7)["cycles"] == 1


def test_user_window_counts():
    active(1)
    pending_job(1, "1", 70)
    pending_job(1, "2", 40, title="B", company="Y")
    db.mark_sent(1, "1", 9)
    db.upsert_cards([__import__("app.linkedin", fromlist=["Job"]).Job(id="3", title="t", company="c", location="l")])
    db.add_waiting(1, ["3"], "kw")
    db.set_result(1, "3", "rejected", None, "x", "title")
    w = db.user_window(1, db.now() - timedelta(hours=1))
    assert w == {"checked": 3, "matched": 2, "best": 70, "sent": 1}


# ---------------- /alive ----------------

def alive_reply(uid=1, uptime_min=60):
    ctx = make_ctx()
    ctx.application = app_obj(uptime_min)
    ctx.application.bot = ctx.bot
    run(bot.cmd_alive(make_update(uid=uid), ctx))
    return ctx.bot.sent[-1]


def test_alive_healthy_shows_proof():
    active(1, keywords=["AI Engineer", "Backend Developer"])
    cycle(2)
    pending_job(1, "1", 81)
    msg = alive_reply()
    assert "Yes — I'm hunting for you right now" in msg.text
    assert "2 min ago" in msg.text and "6 searches, 18 listings" in msg.text
    assert "AI Engineer, Backend Developer" in msg.text and "Delhi NCR" in msg.text
    assert "Just posted" in msg.text and "Easy Apply only" in msg.text
    assert "Today for you: checked <b>1</b>" in msg.text and "best 81%" in msg.text


def test_alive_down_and_slow():
    active(1, keywords=["AI Engineer"])
    cycle(45)
    assert "Something's wrong" in alive_reply().text
    cycle(1, error="network error: dns")
    assert "slowed down" in alive_reply().text and "dns" in alive_reply().text
    db.set_meta("cooldown_until", db.iso(db.now() + timedelta(minutes=20)))
    assert "LinkedIn asked me to slow down" in alive_reply().text


def test_alive_points_out_user_blockers_with_fix_buttons():
    active(1, keywords=[])
    db.update_user(1, paused=1)
    cycle(1)
    msg = alive_reply()
    assert "your alerts are paused" in msg.text and "no job titles set" in msg.text and "No resume yet" in msg.text
    datas = [b["callback_data"] for row in msg.kw["reply_markup"].to_dict()["inline_keyboard"] for b in row]
    assert datas == ["alive:resume", "f:kw"]


def test_alive_resume_button_unpauses():
    active(1)
    db.update_user(1, paused=1)
    run(bot.on_alive_button(make_update(uid=1, data="alive:resume"), make_ctx()))
    assert db.get_user(1)["paused"] == 0


def test_alive_quiet_day_explains_why():
    active(1, keywords=["AI Engineer"])
    cycle(1)
    assert "That's normal" in alive_reply().text


def test_alive_right_after_restart():
    active(1, keywords=["AI Engineer"])
    assert "Just restarted" in alive_reply(uptime_min=1).text


def test_alive_is_a_command_and_menu_button():
    assert "alive" in [c for c, _ in bot.COMMANDS]
    assert ui.BTN_ALIVE in ui.MENU_BUTTONS and ui.BTN_ALIVE in str(ui.MAIN_KB.to_dict())


# ---------------- in-bot watchdog ----------------

def test_watchdog_alerts_admin_once_then_recovers():
    active(1, admin=True)
    active(2)                      # not an admin: never gets alerts
    app = app_obj()
    cycle(40)
    assert "Job Hunter problem" in run(bot.run_watchdog(app))
    assert run(bot.run_watchdog(app)) == ""            # no spam while still broken
    assert [m.chat_id for m in app.bot.sent] == [10]
    cycle(1)
    assert "back to normal" in run(bot.run_watchdog(app))
    assert run(bot.run_watchdog(app)) == ""
    assert len(app.bot.sent) == 2


def test_watchdog_detects_cooldown_and_repeated_errors():
    app = app_obj()
    cycle(1)
    assert bot.watchdog_problem(app) == ""
    for _ in range(3):
        cycle(0, error="network error: dns")
    assert "last 3 scans failed" in bot.watchdog_problem(app)
    cycle(0)
    db.set_meta("cooldown_until", db.iso(db.now() + timedelta(minutes=10)))
    assert "rate-limited" in bot.watchdog_problem(app)


def test_watchdog_quiet_during_startup():
    assert bot.watchdog_problem(app_obj(uptime_min=2)) == ""
    assert "no scan has run" in bot.watchdog_problem(app_obj(uptime_min=60))


# ---------------- daily report ----------------

def test_daily_report_sent_to_active_users_with_it_on():
    active(1, keywords=["AI Engineer"])
    active(2, keywords=["x"], daily_report=False)
    active(3, keywords=["x"])
    db.update_user(3, paused=1)
    pending_job(1, "1", 77)
    db.mark_sent(1, "1", 5)
    for _ in range(3):
        cycle(30)
    app = app_obj()
    assert run(bot.send_daily_reports(app)) == 1
    msg = app.bot.sent[0]
    assert msg.chat_id == 10 and "Good morning" in msg.text and "Sent you <b>1</b>" in msg.text
    assert "best match 77%" in msg.text and "only 3 scans ran" in msg.text


def test_daily_report_text_full_day():
    text = ui.daily_report_text("Asha", {"checked": 40, "matched": 6, "best": 88, "sent": 6},
                                {"cycles": 480, "listings": 900, "new_jobs": 40, "errors": 0}, 3)
    assert "scans ran all day" in text and "Asha" in text and "Quiet day" not in text


def test_daily_report_toggle_in_filters():
    active(1)
    assert Filters().daily_report is True
    upd = make_update(uid=1, data="f:daily")
    run(bot.on_filter_button(upd, make_ctx()))
    assert Filters.from_json(db.get_user(1)["filters"]).daily_report is False
    assert "f:daily" in str(ui.filters_kb(Filters()).to_dict())


def test_old_filters_json_gets_daily_report_default():
    assert Filters.from_json('{"keywords": ["x"]}').daily_report is True


# ---------------- server watchdog script ----------------

def _fake_bin(tmp: Path, docker_status: str) -> Path:
    b = tmp / "bin"
    b.mkdir(exist_ok=True)
    (b / "docker").write_text(f"""#!/bin/sh
echo "docker $*" >> "{tmp}/calls"
if [ "$1" = inspect ]; then echo "{docker_status}"; fi
exit 0
""")
    (b / "curl").write_text(f"""#!/bin/sh
echo "curl $*" >> "{tmp}/calls"
""")
    for f in b.iterdir():
        f.chmod(0o755)
    return b


def _run_watchdog(tmp: Path, docker_status: str, heartbeat_age: int) -> str:
    if not (ROOT / "scripts" / "watchdog.sh").exists():
        pytest.skip("repo-level script, not copied into the Docker image (checked by hooks and CI)")
    (tmp / "data").mkdir(exist_ok=True)
    hb = tmp / "data" / "heartbeat"
    hb.touch()
    os.utime(hb, (time.time() - heartbeat_age,) * 2)
    env = {**os.environ, "JOBHUNTER_DIR": str(tmp), "PATH": f"{_fake_bin(tmp, docker_status)}:{os.environ['PATH']}"}
    out = subprocess.run(["bash", str(ROOT / "scripts" / "watchdog.sh")], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _calls(tmp: Path) -> str:
    p = tmp / "calls"
    text = p.read_text() if p.exists() else ""
    p.unlink(missing_ok=True)
    return text


def test_watchdog_script_restarts_alerts_once_and_recovers(tmp_path):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\nADMIN_IDS=555\n")
    assert _run_watchdog(tmp_path, "running healthy", 10) == "ok"
    assert "curl" not in _calls(tmp_path)

    assert _run_watchdog(tmp_path, "exited ", 10).startswith("problem: container is exited")
    calls = _calls(tmp_path)
    assert "docker compose up -d" in calls and "chat_id=555" in calls and "Restarting it now" in calls

    _run_watchdog(tmp_path, "exited ", 10)                    # still down: restart again, but no 2nd alert
    calls = _calls(tmp_path)
    assert "compose up -d" in calls and "curl" not in calls

    assert _run_watchdog(tmp_path, "running healthy", 10) == "recovered"
    assert "running again" in _calls(tmp_path)
    assert not (tmp_path / "data" / "watchdog.alerted").exists()


def test_watchdog_script_catches_frozen_bot(tmp_path):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\nADMIN_IDS=555\n")
    assert "heartbeat is" in _run_watchdog(tmp_path, "running healthy", 900)


def test_watchdog_script_reads_admins_from_database(tmp_path):
    import sqlite3
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\nADMIN_IDS=\n")
    (tmp_path / "data").mkdir()
    c = sqlite3.connect(tmp_path / "data" / "jobhunter.db")
    c.execute("CREATE TABLE users (chat_id INTEGER, is_admin INTEGER)")
    c.execute("INSERT INTO users VALUES (777, 1), (888, 0)")
    c.commit()
    c.close()
    _run_watchdog(tmp_path, "missing", 10)
    calls = _calls(tmp_path)
    assert "chat_id=777" in calls and "chat_id=888" not in calls
