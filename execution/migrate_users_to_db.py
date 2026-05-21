"""
One-time migration: copy .tmp/users.json → Postgres `users` table.

Usage (locally):
    export DATABASE_URL='postgresql://...'  # from Supabase → Project Settings → Database
    python3 execution/migrate_users_to_db.py

Idempotent: re-running skips existing emails (ON CONFLICT DO NOTHING).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
USERS_FILE = ROOT / ".tmp" / "users.json"

# Allow running from any cwd
sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402


def main() -> int:
    if not db.is_enabled():
        print("ERROR: DATABASE_URL not set. Export it before running this script.", file=sys.stderr)
        return 1

    if not USERS_FILE.exists():
        print(f"No users file at {USERS_FILE} — nothing to migrate.")
        return 0

    try:
        users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: failed to read {USERS_FILE}: {e}", file=sys.stderr)
        return 1

    if not isinstance(users, dict) or not users:
        print(f"users.json is empty.")
        return 0

    inserted = 0
    skipped = 0
    for email, u in users.items():
        if not isinstance(u, dict):
            continue
        uid = u.get("id")
        password = u.get("password")
        if not uid or not password:
            print(f"  skip {email}: missing id or password")
            skipped += 1
            continue
        ok = db.user_insert(
            user_id=uid,
            email=u.get("email", email),
            name=u.get("name", ""),
            password_hash=password,
            credits=int(u.get("credits", 0) or 0),
        )
        if ok:
            print(f"  + {email}  (id={uid})")
            inserted += 1
        else:
            print(f"  · {email}  (already in DB)")
            skipped += 1

    print(f"\nDone. Inserted {inserted}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
