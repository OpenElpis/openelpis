"""
OpenElpis portal — shared core: config, DB pool, auth helpers + FastAPI deps.
Imported by main.py and every route module. Secrets come from /etc/openelpis.env.
"""
import os, json, datetime as dt
from typing import Optional

import jwt
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from argon2 import PasswordHasher
from fastapi import Request, Response, HTTPException, Depends

# ── config ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET   = os.environ["JWT_SECRET"]
COOKIE_NAME  = "oe_session"
TOKEN_TTL    = dt.timedelta(days=7)
SITE_ORIGIN  = os.environ.get("SITE_ORIGIN", "https://openelpis.com")
# Emails that are auto-admin + auto-verified and may sign up WITHOUT an invite
# (the founder's bootstrap). Comma-separated in /etc/openelpis.env.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

ph   = PasswordHasher()
pool = ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)


# ── db ────────────────────────────────────────────────────────────────────────
class db:
    """`with db() as cur:` — commits on clean exit, rolls back on exception,
    always returns the connection to the pool."""
    def __enter__(self):
        self.conn = pool.getconn()
        self.cur = self.conn.cursor(cursor_factory=RealDictCursor)
        return self.cur
    def __exit__(self, exc_type, *_):
        if exc_type: self.conn.rollback()
        else: self.conn.commit()
        self.cur.close(); pool.putconn(self.conn)


def audit(cur, user_id, action, etype=None, eid=None, detail=None, ip=None):
    cur.execute(
        "INSERT INTO audit_log(user_id,action,entity_type,entity_id,detail,ip) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (user_id, action, etype, str(eid) if eid else None, json.dumps(detail or {}), ip),
    )


def client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)


def is_admin_email(email: str) -> bool:
    return bool(email) and email.lower() in ADMIN_EMAILS


# ── auth ──────────────────────────────────────────────────────────────────────
def make_token(user) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return jwt.encode(
        {"sub": str(user["id"]), "email": user["email"], "role": user["role"],
         "iat": now, "exp": now + TOKEN_TTL},
        JWT_SECRET, algorithm="HS256")


def set_cookie(resp: Response, token: str):
    resp.set_cookie(COOKIE_NAME, token, max_age=int(TOKEN_TTL.total_seconds()),
                    httponly=True, secure=True, samesite="lax", path="/")


def current_user(request: Request):
    """FastAPI dependency — resolves the logged-in user from the session cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid session")
    with db() as cur:
        cur.execute("SELECT id,email,full_name,role,verification_status,is_active,org_id,specialty,bio "
                    "FROM users WHERE id=%s", (payload["sub"],))
        user = cur.fetchone()
    if not user or not user["is_active"]:
        raise HTTPException(401, "account not found or disabled")
    return user


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "admin only")
    return user


def require_reviewer(user=Depends(current_user)):
    """Material reviewers (and admins) may approve/reject others' uploads from their
    own dashboard — no admin-panel access required."""
    if user["role"] not in ("reviewer", "admin"):
        raise HTTPException(403, "reviewer or admin only")
    return user
