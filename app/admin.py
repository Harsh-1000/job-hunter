"""Admin CLI (used by the `jobhunter` helper):

    python -m app.admin users              list users
    python -m app.admin approve <id>       let a waiting user in
    python -m app.admin block <id>         remove someone's access
    python -m app.admin make-admin <id>    make a user admin
    python -m app.admin stats              totals
    python -m app.admin reset-cooldown     forget a LinkedIn rate-limit pause
"""
from __future__ import annotations

import json
import sys

from . import db
from .scanner import clear_cooldown, in_cooldown, user_context


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
    if cmd == "reset-cooldown":
        clear_cooldown()
        print("cooldown cleared")
        return 0
    print(__doc__)
    return 0 if cmd == "help" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
