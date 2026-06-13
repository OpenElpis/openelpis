"""
OpenElpis clinician portal — backend API (FastAPI), modular.
Runs on server2 (localhost:8000), behind server1's Caddy at openelpis.com/api/*.

  main.py      app + /api/health + auth (invite-gated signup, login, logout, me, badges)
  core.py      config, DB pool, auth helpers + FastAPI deps
  shares.py    attach/preview a material or saved answer
  invites.py   invitations + public invite validation + access requests
  materials.py upload / list-mine / visibility-checked file download
  forum.py     question topics + replies (+ shares)
  social.py    member directory + friend requests + direct messages
  copilot.py   PLACEHOLDER ask + saved (shareable) answers
  admin.py     admin panel: members, requests, invites, materials, moderation, audit

Security: argon2 hashing, JWT in an httpOnly+Secure cookie, parameterized SQL,
INVITE-ONLY signup (founder bootstrap via ADMIN_EMAILS), per-file content hashing.
Secrets (DATABASE_URL, JWT_SECRET, ADMIN_EMAILS) come from /etc/openelpis.env.
"""
import datetime as dt, hashlib
from typing import Optional

from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, constr

from core import (db, ph, audit, client_ip, make_token, set_cookie, current_user,
                  is_admin_email, COOKIE_NAME)
import invites, materials, forum, social, copilot, admin

app = FastAPI(title="OpenElpis portal API", docs_url=None, redoc_url=None)
for module in (invites, materials, forum, social, copilot, admin):
    app.include_router(module.router)

ORG_TYPES = {"clinic", "hospital", "lab", "university", "individual", "other"}


class SignupIn(BaseModel):
    email:        EmailStr
    password:     constr(min_length=10, max_length=200)
    full_name:    constr(min_length=2, max_length=200)
    org_name:     Optional[constr(max_length=200)] = None
    org_type:     Optional[str] = None
    country:      Optional[constr(max_length=100)] = None
    specialty:    Optional[constr(max_length=120)] = None
    invite_token: Optional[constr(max_length=200)] = None


class LoginIn(BaseModel):
    email:    EmailStr
    password: str


@app.get("/api/health")
def health():
    with db() as cur:
        cur.execute("SELECT 1")
    return {"ok": True, "service": "openelpis-portal"}


@app.post("/api/signup")
def signup(body: SignupIn, request: Request):
    """Invite-only. Admin-listed emails bootstrap without an invite (founder)."""
    otype = body.org_type if body.org_type in ORG_TYPES else "other"
    admin_bootstrap = is_admin_email(body.email)
    with db() as cur:
        cur.execute("SELECT 1 FROM users WHERE email=%s", (body.email,))
        if cur.fetchone():
            raise HTTPException(409, "an account with this email already exists")

        invited_by, role, vstatus, invite = None, "contributor", "pending", None
        if admin_bootstrap:
            role, vstatus = "admin", "verified"
        else:
            if not body.invite_token:
                raise HTTPException(403, "Sign-up is invite-only. Please request access.")
            th = hashlib.sha256(body.invite_token.encode()).hexdigest()
            cur.execute("SELECT id,created_by,email,intended_role,expires_at,used_at,revoked_at "
                        "FROM invitations WHERE token_hash=%s", (th,))
            invite = cur.fetchone()
            if (not invite or invite["used_at"] or invite["revoked_at"]
                    or invite["expires_at"] <= dt.datetime.now(dt.timezone.utc)):
                raise HTTPException(403, "This invitation is invalid, already used, or expired.")
            if invite["email"] and invite["email"].lower() != body.email.lower():
                raise HTTPException(403, "This invitation was issued for a different email address.")
            invited_by = invite["created_by"]
            role = invite["intended_role"] or "contributor"

        org_id = None
        if body.org_name:
            cur.execute("INSERT INTO organizations(name,org_type,country) VALUES (%s,%s,%s) RETURNING id",
                        (body.org_name, otype, body.country))
            org_id = cur.fetchone()["id"]

        pw_hash = ph.hash(body.password)
        cur.execute(
            "INSERT INTO users(org_id,email,password_hash,full_name,role,verification_status,"
            "invited_by,specialty,email_verified) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id,email,full_name,role,verification_status",
            (org_id, body.email, pw_hash, body.full_name, role, vstatus,
             invited_by, body.specialty, admin_bootstrap))
        u = cur.fetchone()
        if invite:
            cur.execute("UPDATE invitations SET used_at=now(), used_by=%s WHERE id=%s", (u["id"], invite["id"]))
            cur.execute("UPDATE access_requests SET status='invited' "
                        "WHERE email=%s AND status IN ('pending','approved')", (body.email,))
        audit(cur, u["id"], "signup", "user", u["id"],
              {"org": body.org_name, "via": "admin" if admin_bootstrap else "invite"}, client_ip(request))

    resp = JSONResponse({"ok": True, "message": "Welcome to OpenElpis — you're in.",
        "user": {"email": u["email"], "full_name": u["full_name"], "role": u["role"],
                 "verification_status": u["verification_status"]}})
    set_cookie(resp, make_token(u))  # log them straight in
    return resp


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    with db() as cur:
        cur.execute("SELECT id,email,full_name,role,password_hash,is_active,verification_status "
                    "FROM users WHERE email=%s", (body.email,))
        user = cur.fetchone()
    if not user or not user["is_active"]:
        raise HTTPException(401, "invalid email or password")
    try:
        ph.verify(user["password_hash"], body.password)
    except VerifyMismatchError:
        raise HTTPException(401, "invalid email or password")
    with db() as cur:
        # keep founder/admins bootstrapped even if their account predates ADMIN_EMAILS
        if is_admin_email(user["email"]) and (user["role"] != "admin" or user["verification_status"] != "verified"):
            cur.execute("UPDATE users SET role='admin', verification_status='verified' WHERE id=%s", (user["id"],))
            user["role"], user["verification_status"] = "admin", "verified"
        cur.execute("UPDATE users SET last_login_at=now() WHERE id=%s", (user["id"],))
        audit(cur, user["id"], "login", "user", user["id"], None, client_ip(request))
    resp = JSONResponse({"ok": True, "user": {"email": user["email"], "full_name": user["full_name"],
                                              "role": user["role"]}})
    set_cookie(resp, make_token(user))
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/api/me")
def me(user=Depends(current_user)):
    org = None
    if user["org_id"]:
        with db() as cur:
            cur.execute("SELECT name FROM organizations WHERE id=%s", (user["org_id"],))
            o = cur.fetchone()
            org = o["name"] if o else None
    return {"id": str(user["id"]), "email": user["email"], "full_name": user["full_name"],
            "role": user["role"], "verification_status": user["verification_status"],
            "specialty": user.get("specialty"), "bio": user.get("bio"), "org": org,
            "is_super_admin": is_admin_email(user["email"])}


@app.get("/api/badges")
def badges(user=Depends(current_user)):
    """Lightweight counters for the dashboard nav (polled slowly)."""
    out = {"unread_dms": 0, "friend_requests": 0}
    with db() as cur:
        cur.execute("SELECT count(*) AS n FROM direct_messages WHERE recipient_id=%s AND read_at IS NULL", (user["id"],))
        out["unread_dms"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM connections WHERE addressee_id=%s AND status='pending'", (user["id"],))
        out["friend_requests"] = cur.fetchone()["n"]
        if user["role"] in ("reviewer", "admin"):
            cur.execute("SELECT count(*) AS n FROM materials WHERE status='pending_review' AND uploaded_by<>%s", (user["id"],))
            out["review_pending"] = cur.fetchone()["n"]
        if user["role"] == "admin":
            cur.execute("SELECT (SELECT count(*) FROM access_requests WHERE status='pending') AS reqs, "
                        "(SELECT count(*) FROM materials WHERE status='pending_review') AS mats")
            r = cur.fetchone()
            out["admin"] = {"pending_requests": r["reqs"], "pending_materials": r["mats"]}
    return out


@app.get("/api/home")
def home(user=Depends(current_user)):
    """Quick-glance summary for the dashboard home tab."""
    uid = user["id"]
    out = {"role": user["role"]}
    with db() as cur:
        cur.execute("SELECT count(*) AS total, count(*) FILTER (WHERE status='pending_review') AS pending "
                    "FROM materials WHERE uploaded_by=%s", (uid,))
        m = cur.fetchone(); out["materials"] = m["total"]; out["materials_pending"] = m["pending"]
        cur.execute("SELECT count(*) AS n FROM connections WHERE status='accepted' "
                    "AND (requester_id=%s OR addressee_id=%s)", (uid, uid))
        out["connections"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM invitations WHERE created_by=%s "
                    "AND used_at IS NULL AND revoked_at IS NULL AND expires_at>now()", (uid,))
        out["invites_active"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM direct_messages WHERE recipient_id=%s AND read_at IS NULL", (uid,))
        out["unread_dms"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM connections WHERE addressee_id=%s AND status='pending'", (uid,))
        out["friend_requests"] = cur.fetchone()["n"]
        if user["role"] in ("reviewer", "admin"):
            cur.execute("SELECT count(*) AS n FROM materials WHERE status='pending_review' AND uploaded_by<>%s", (uid,))
            out["review_pending"] = cur.fetchone()["n"]
        if user["role"] == "admin":
            cur.execute("SELECT (SELECT count(*) FROM access_requests WHERE status='pending') AS r, "
                        "(SELECT count(*) FROM materials WHERE status='pending_review') AS m")
            r = cur.fetchone(); out["admin"] = {"pending_requests": r["r"], "pending_materials": r["m"]}
    return out
