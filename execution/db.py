"""
Postgres data layer for AI Brand Scale.

Activates when DATABASE_URL is set (production / Supabase).
Falls back to filesystem (.tmp/users.json) when not set, so local dev still works.

Connection pool is lazy and thread-safe (ThreadingHTTPServer-friendly).
"""

import os
import json
import threading
from contextlib import contextmanager
from typing import Optional

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_pool = None
_pool_lock = threading.Lock()


def is_enabled() -> bool:
    """True iff DATABASE_URL is set — caller can branch to filesystem fallback."""
    return bool(DATABASE_URL)


def _get_pool():
    """Lazy-init a thread-safe connection pool. Imports psycopg2 only when needed."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        from psycopg2.pool import ThreadedConnectionPool
        # Supabase pooler defaults are fine. minconn=1, maxconn=10 sized for Starter dyno.
        _pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)
        return _pool


@contextmanager
def conn():
    """Borrow a connection from the pool. Auto-commits on success, rolls back on error."""
    pool = _get_pool()
    c = pool.getconn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        pool.putconn(c)


@contextmanager
def cursor():
    """Convenience: borrow a connection and yield a cursor in one step."""
    with conn() as c:
        cur = c.cursor()
        try:
            yield cur
        finally:
            cur.close()


# ═══════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════

def user_get_by_email(email: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    if not email:
        return None
    with cursor() as cur:
        cur.execute(
            "SELECT id, email, name, password_hash, credits FROM users WHERE lower(email) = %s",
            (email,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "email": row[1], "name": row[2] or "",
        "password": row[3], "credits": row[4] or 0,
    }


def user_get_by_id(user_id: str) -> Optional[dict]:
    if not user_id:
        return None
    with cursor() as cur:
        cur.execute(
            "SELECT id, email, name, credits FROM users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "email": row[1], "name": row[2] or "", "credits": row[3] or 0}


def user_insert(user_id: str, email: str, name: str, password_hash: str, credits: int = 0) -> bool:
    """Returns True on success, False if email already exists."""
    email = email.strip().lower()
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (id, email, name, password_hash, credits)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (email) DO NOTHING
            RETURNING id
            """,
            (user_id, email, name or email.split("@")[0], password_hash, credits),
        )
        row = cur.fetchone()
    return row is not None


def user_update_credits(user_id: str, delta: int) -> int:
    """Atomically adjust credits. Returns new balance, or -1 if user not found / would go negative."""
    with cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET credits = credits + %s
            WHERE id = %s AND credits + %s >= 0
            RETURNING credits
            """,
            (delta, user_id, delta),
        )
        row = cur.fetchone()
    return row[0] if row else -1


# ═══════════════════════════════════════════════════════════
# JOB HISTORY
# ═══════════════════════════════════════════════════════════

def history_record(
    job_id: str,
    user_id: str,
    feature: str,
    title: str = "",
    brief: Optional[dict] = None,
    status: str = "running",
    credits_cost: int = 0,
) -> None:
    """Insert a new job history row. Idempotent on job_id (re-run = no-op)."""
    if not user_id:
        return
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_history (id, user_id, feature, title, brief, status, credits_cost)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                job_id, user_id, feature, title or None,
                json.dumps(brief or {}, ensure_ascii=False),
                status, credits_cost,
            ),
        )


def history_update_status(
    job_id: str,
    status: str,
    result: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Update status (and optionally result/error) on an existing job history row."""
    with cursor() as cur:
        cur.execute(
            """
            UPDATE job_history
            SET status = %s,
                result = COALESCE(%s::jsonb, result),
                error  = COALESCE(%s, error)
            WHERE id = %s
            """,
            (
                status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error,
                job_id,
            ),
        )


def history_list_by_user(user_id: str, feature: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Return latest N jobs for a user, optionally filtered by feature."""
    if not user_id:
        return []
    sql = """
        SELECT id, feature, title, status, brief, result, error, credits_cost, created_at, updated_at
        FROM job_history
        WHERE user_id = %s
    """
    params: list = [user_id]
    if feature:
        sql += " AND feature = %s"
        params.append(feature)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    with cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "feature": r[1],
            "title": r[2] or "",
            "status": r[3],
            "brief": r[4] or {},
            "result": r[5] or {},
            "error": r[6],
            "credits_cost": r[7] or 0,
            "created_at": r[8].isoformat() if r[8] else None,
            "updated_at": r[9].isoformat() if r[9] else None,
        })
    return out
