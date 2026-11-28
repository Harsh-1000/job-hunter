"""Admin CLI (used by the `jobhunter` helper):

    python -m app.admin users              list users
    python -m app.admin approve <id>       let a waiting user in
    python -m app.admin block <id>         remove someone's access
    python -m app.admin make-admin <id>    make a user admin
    python -m app.admin stats              totals
    python -m app.admin reset-cooldown     forget a LinkedIn rate-limit pause
    python -m app.admin ai-status          Claude judge: worker health, state, who may use it
    python -m app.admin ai-allow <id>      let a user use the Claude judge (admins always can)
    python -m app.admin ai-block <id>      stop a user using it
    python -m app.admin ai-test [<id>]     ask Claude to judge the newest job for a user (default: first admin)
    python -m app.admin connect-info <site> [<id>]   login URL, hosts and session file (for `jobhunter connect`)
    python -m app.admin disconnect <site> [<id>]     forget a saved site sign-in
    python -m app.admin connections [<id>]           which sites a user is signed in to
"""
from __future__ import annotations

import asyncio
import json
import sys

from . import ai as ai_mod
from . import db, storage
from .apply_sites import SITES, login_sites
from .scanner import ai_allowed, clear_cooldown, in_cooldown, user_context


async def _ai_test(uid: int | None) -> int:
    users = [u for u in db.users("active") if (uid is None and u["is_admin"]) or u["id"] == uid]
    if not users:
        print("No such active user (or no admin yet).")
        return 1
    u = users[0]
    f, p = user_context(u)
    if not p:
        print("That user has no resume yet.")
        return 1
    with db.conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE detail_status='ok' AND description <> '' "
                        "ORDER BY first_seen DESC LIMIT 1").fetchone()
    if not row:
        print("No job with a description yet; wait for a scan.")
        return 1
    client = ai_mod.AIClient()
    try:
        print("worker:", json.dumps(await client.health()))
        print(f"judging: {row['title']} at {row['company']} ...")
        v = await client.judge(ai_mod.payload(row, storage.resume_text(u["id"]), p.skills, f.keywords,
                                              f.years(p.years)))
    finally:
        await client.close()
    if not v:
        print("FAILED:", client.last_error)
        return 1
    ai_mod.record_status("")
    print(json.dumps(v.to_dict(), indent=2))
    return 0


def _target_user(arg: str):
    """The user a sign-in belongs to: the given ID, else the first admin."""
    for u in db.users("active"):
        if (arg.isdigit() and u["id"] == int(arg)) or (not arg and u["is_admin"]):
            return u
    return None


def main(argv: list[str]) -> int:
    db.init()
    cmd = argv[0] if argv else "help"
    arg = argv[1] if len(argv) > 1 else ""
    if cmd == "users":
        for u in db.users():
            f, p = user_context(u)
            print(f"{u['id']:>12}  {u['status']:<8} {'admin ' if u['is_admin'] else '      '}"
                  f"{'paused ' if u['paused'] else '       '}{(u['first_name'] or '')[:18]:<18} "
                  f"@{u['username'] or '-':<16} kw={len(f.keywords)} resume={'yes' if p else 'no'}")
        return 0
    if cmd in ("approve", "block", "make-admin"):
        if not arg.isdigit() or not db.get_user(int(arg)):
            print(f"No user {arg!r}. See: jobhunter users")
            return 1
        if cmd == "make-admin":
            db.update_user(int(arg), is_admin=1, status="active")
        else:
            db.update_user(int(arg), status="active" if cmd == "approve" else "blocked")
        print(f"{cmd}: {arg} done (the user isn't messaged when done from here; approve in Telegram to notify).")
        return 0
    if cmd == "stats":
        s = db.global_stats()
        s["cooldown_until"] = in_cooldown() or "-"
        s["last_cycle"] = db.get_meta("last_cycle_at", "-") + " " + db.get_meta("last_cycle", "")
        print(json.dumps(s, indent=2))
        return 0
    if cmd in ("ai-allow", "ai-block"):
        if not arg.isdigit() or not db.get_user(int(arg)):
            print(f"No user {arg!r}. See: jobhunter users")
            return 1
        db.update_user(int(arg), ai_allowed=1 if cmd == "ai-allow" else 0)
        print(f"{cmd}: {arg} done")
        return 0
    if cmd == "ai-status":
        st = ai_mod.status()
        st["users_with_ai"] = [u["id"] for u in db.users("active") if ai_allowed(u)]
        st["judged_24h"] = db.ai_calls_since(24)
        print(json.dumps(st, indent=2))
        return 0
    if cmd == "ai-test":
        return asyncio.run(_ai_test(int(arg) if arg.isdigit() else None))
    if cmd in ("connect-info", "disconnect"):
        if arg not in login_sites():
            print(f"Unknown site {arg!r}. Sites you sign in to: {', '.join(login_sites())}")
            return 1
        u = _target_user(argv[2] if len(argv) > 2 else "")
        if not u:
            print("No such active user (or no admin yet). See: jobhunter users")
            return 1
        if cmd == "disconnect":
            print("Signed out: saved sign-in deleted." if storage.delete_session(u["id"], arg) else "Nothing saved.")
            return 0
        site = SITES[arg]
        rel = storage.session_path(u["id"], arg).relative_to(storage.settings.users_dir.parent)
        print("\t".join([site["login_url"], ",".join(site["hosts"]), str(rel), str(u["id"])]))
        return 0
    if cmd == "connections":
        u = _target_user(arg)
        if not u:
            print("No such active user (or no admin yet).")
            return 1
        st = storage.session_status(u["id"])
        for site in login_sites():
            s_ = st.get(site)
            print(f"{site:<10} " + (f"signed in {s_['saved']} · {s_['cookies']} cookies · expires {s_['expires'] or '?'}"
                                    if s_ else "not signed in"))
        return 0
    if cmd == "reset-cooldown":
        clear_cooldown()
        print("cooldown cleared")
        return 0
    print(__doc__)
    return 0 if cmd == "help" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
