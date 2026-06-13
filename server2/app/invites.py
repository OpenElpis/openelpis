"""
Invitations (one-time, 14-day) + public invite validation + access requests.

Signup is invite-only. Any active member can mint up to MAX_ACTIVE_INVITES live
links; admins can target an email / set a role (see admin.py). Only the sha256
HASH of a token is stored — the raw token exists only inside the share link.
Non-invited people POST /api/access-request, which an admin later reviews.
"""
import secrets, hashlib, datetime as dt
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, EmailStr, constr

from core import db, audit, client_ip, current_user, SITE_ORIGIN
import mailer

router = APIRouter()

INVITE_TTL_DAYS    = 14
MAX_ACTIVE_INVITES = 5


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _status(inv) -> str:
    if inv["used_at"]:    return "used"
    if inv["revoked_at"]: return "revoked"
    if inv["expires_at"] <= dt.datetime.now(dt.timezone.utc): return "expired"
    return "active"


# ── members create / list / revoke their own invites ───────────────────────────
class InviteIn(BaseModel):
    email: Optional[EmailStr] = None
    note:  Optional[constr(max_length=300)] = None
    lang:  Optional[constr(max_length=5)] = None   # inviter's UI language (localizes the email)


@router.post("/api/invites")
def create_invite(body: InviteIn, request: Request, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT count(*) AS n FROM invitations WHERE created_by=%s "
                    "AND used_at IS NULL AND revoked_at IS NULL AND expires_at>now()", (user["id"],))
        if cur.fetchone()["n"] >= MAX_ACTIVE_INVITES:
            raise HTTPException(429, f"You already have {MAX_ACTIVE_INVITES} active invites — "
                                     "revoke one or wait for them to be used.")
        token   = secrets.token_urlsafe(32)
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=INVITE_TTL_DAYS)
        cur.execute("INSERT INTO invitations(token_hash,created_by,email,note,expires_at) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING id,expires_at",
                    (_hash(token), user["id"], body.email, body.note, expires))
        inv = cur.fetchone()
        audit(cur, user["id"], "invite_create", "invitation", inv["id"], {"email": body.email}, client_ip(request))
    url = f"{SITE_ORIGIN}/portal/?invite={token}"
    emailed = False
    if body.email:
        emailed = mailer.try_send_invite(body.email, url, inv["expires_at"],
                                         inviter=user["full_name"], lang=body.lang)
    return {"ok": True, "id": str(inv["id"]), "token": token, "url": url,
            "expires_at": inv["expires_at"].isoformat(),
            "emailed": emailed, "emailed_to": body.email if emailed else None}


@router.get("/api/invites")
def my_invites(user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT id,email,note,expires_at,used_at,revoked_at,created_at "
                    "FROM invitations WHERE created_by=%s ORDER BY created_at DESC LIMIT 50", (user["id"],))
        rows = cur.fetchall()
    return {"invites": [
        {"id": str(r["id"]), "email": r["email"], "note": r["note"], "status": _status(r),
         "expires_at": r["expires_at"].isoformat(), "created_at": r["created_at"].isoformat()} for r in rows]}


@router.post("/api/invites/{invite_id}/revoke")
def revoke_invite(invite_id: str, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT created_by,used_at FROM invitations WHERE id=%s", (invite_id,))
        inv = cur.fetchone()
        if not inv: raise HTTPException(404, "invite not found")
        if str(inv["created_by"]) != str(user["id"]) and user["role"] != "admin":
            raise HTTPException(403, "not your invite")
        if inv["used_at"]: raise HTTPException(409, "invite already used")
        cur.execute("UPDATE invitations SET revoked_at=now() WHERE id=%s AND revoked_at IS NULL", (invite_id,))
    return {"ok": True}


@router.get("/api/invite/{token}")
def validate_invite(token: str):
    """PUBLIC — the signup page calls this to decide whether to show the form."""
    with db() as cur:
        cur.execute("SELECT email,used_at,revoked_at,expires_at FROM invitations WHERE token_hash=%s",
                    (_hash(token),))
        inv = cur.fetchone()
    if not inv:
        return {"valid": False, "reason": "not_found"}
    st = _status(inv)
    if st != "active":
        return {"valid": False, "reason": st}
    return {"valid": True, "email": inv["email"]}


# ── public: request access (no invite) ─────────────────────────────────────────
class AccessRequestIn(BaseModel):
    full_name:       constr(min_length=2, max_length=200)
    email:           EmailStr
    org_name:        Optional[constr(max_length=200)] = None
    org_type:        Optional[constr(max_length=40)] = None
    country:         Optional[constr(max_length=100)] = None
    credential_type: Optional[constr(max_length=80)] = None
    credential_ref:  Optional[constr(max_length=120)] = None
    message:         Optional[constr(max_length=1000)] = None


@router.post("/api/access-request")
def access_request(body: AccessRequestIn, request: Request):
    """PUBLIC — a non-invited person asks to join; an admin reviews later."""
    with db() as cur:
        cur.execute("SELECT 1 FROM users WHERE email=%s", (body.email,))
        if cur.fetchone():
            return {"ok": True, "message": "You already have an account — try logging in instead."}
        # collapse duplicates: refresh an existing pending request rather than stacking
        cur.execute("SELECT id FROM access_requests WHERE email=%s AND status='pending'", (body.email,))
        ex = cur.fetchone()
        if ex:
            cur.execute("UPDATE access_requests SET full_name=%s,org_name=%s,org_type=%s,country=%s,"
                        "credential_type=%s,credential_ref=%s,message=%s,created_at=now() WHERE id=%s",
                        (body.full_name, body.org_name, body.org_type, body.country,
                         body.credential_type, body.credential_ref, body.message, ex["id"]))
        else:
            cur.execute("INSERT INTO access_requests(full_name,email,org_name,org_type,country,"
                        "credential_type,credential_ref,message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (body.full_name, body.email, body.org_name, body.org_type, body.country,
                         body.credential_type, body.credential_ref, body.message))
        audit(cur, None, "access_request", "access_request", None, {"email": body.email}, client_ip(request))
    return {"ok": True, "message": "Thanks — your request has been received. We review every applicant; "
            "if approved, you'll receive an invitation by email."}
