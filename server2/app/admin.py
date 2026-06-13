"""
Admin panel API — every endpoint requires role='admin' (require_admin). The
English-only /portal/admin.html consumes these. Covers: overview stats, member
management, access-request review (approve -> issues an invite), invite
management, materials (the trust gate, filterable by uploader), forum moderation,
and the audit log.
"""
import secrets, hashlib, datetime as dt
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, EmailStr, constr

from core import db, audit, client_ip, require_admin, is_admin_email, SITE_ORIGIN
import mailer

router = APIRouter()


@router.get("/api/admin/overview")
def overview(user=Depends(require_admin)):
    with db() as cur:
        cur.execute("""SELECT
          (SELECT count(*) FROM users)                                                              AS users,
          (SELECT count(*) FROM users WHERE verification_status='pending')                          AS users_pending,
          (SELECT count(*) FROM users WHERE verification_status='verified')                         AS users_verified,
          (SELECT count(*) FROM materials)                                                          AS materials,
          (SELECT count(*) FROM materials WHERE status='pending_review')                            AS materials_pending,
          (SELECT count(*) FROM materials WHERE status='approved')                                  AS materials_approved,
          (SELECT count(*) FROM access_requests WHERE status='pending')                             AS requests_pending,
          (SELECT count(*) FROM invitations
             WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at>now())                     AS invites_active,
          (SELECT count(*) FROM forum_topics WHERE status='open')                                   AS topics,
          (SELECT count(*) FROM direct_messages)                                                    AS dms""")
        s = cur.fetchone()
    return {k: s[k] for k in s}


# ── members ─────────────────────────────────────────────────────────────────────
@router.get("/api/admin/members")
def members(q: Optional[str] = None, status: Optional[str] = None, user=Depends(require_admin)):
    sql = ("SELECT u.id,u.email,u.full_name,u.role,u.verification_status,u.is_active,u.specialty,"
           "u.created_at,u.last_login_at,o.name AS org,inv.full_name AS invited_by,"
           "(SELECT count(*) FROM materials m WHERE m.uploaded_by=u.id) AS uploads "
           "FROM users u LEFT JOIN organizations o ON o.id=u.org_id "
           "LEFT JOIN users inv ON inv.id=u.invited_by WHERE 1=1")
    params = []
    if q:
        sql += " AND (u.full_name ILIKE %s OR u.email ILIKE %s)"; params += [f"%{q}%", f"%{q}%"]
    if status in ("pending", "verified", "rejected"):
        sql += " AND u.verification_status=%s"; params.append(status)
    sql += " ORDER BY u.created_at DESC LIMIT 200"
    with db() as cur:
        cur.execute(sql, params); rows = cur.fetchall()
    return {"members": [
        {"id": str(r["id"]), "email": r["email"], "full_name": r["full_name"], "role": r["role"],
         "verification_status": r["verification_status"], "is_active": r["is_active"],
         "specialty": r["specialty"], "org": r["org"], "invited_by": r["invited_by"], "uploads": r["uploads"],
         "created_at": r["created_at"].isoformat(),
         "last_login_at": r["last_login_at"].isoformat() if r["last_login_at"] else None} for r in rows]}


class MemberPatch(BaseModel):
    verification_status: Optional[str] = None
    is_active:           Optional[bool] = None
    role:                Optional[str] = None


@router.post("/api/admin/members/{member_id}")
def update_member(member_id: str, body: MemberPatch, request: Request, user=Depends(require_admin)):
    super_admin = is_admin_email(user["email"])   # the owner (ADMIN_EMAILS) — only they manage admins
    with db() as cur:
        cur.execute("SELECT role FROM users WHERE id=%s", (member_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(404, "member not found")
        # A regular admin has full access EXCEPT adding/modifying admins.
        if (target["role"] == "admin" or body.role == "admin") and not super_admin:
            raise HTTPException(403, "Only the owner can add or modify admins.")
        sets, params = [], []
        if body.verification_status in ("pending", "verified", "rejected"):
            sets.append("verification_status=%s"); params.append(body.verification_status)
        if body.is_active is not None:
            sets.append("is_active=%s"); params.append(body.is_active)
        if body.role in ("contributor", "reviewer", "admin"):
            sets.append("role=%s"); params.append(body.role)
        if not sets:
            raise HTTPException(400, "nothing to update")
        params.append(member_id)
        cur.execute(f"UPDATE users SET {','.join(sets)} WHERE id=%s", params)
        audit(cur, user["id"], "admin_member_update", "user", member_id,
              body.dict(exclude_none=True), client_ip(request))
    return {"ok": True}


# ── access requests ──────────────────────────────────────────────────────────────
def _new_invite(cur, created_by, email, role, note):
    token   = secrets.token_urlsafe(32)
    th      = hashlib.sha256(token.encode()).hexdigest()
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=14)
    cur.execute("INSERT INTO invitations(token_hash,created_by,email,intended_role,note,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (th, created_by, email, role, note, expires))
    return token, cur.fetchone()["id"], expires


@router.get("/api/admin/requests")
def requests(status: Optional[str] = "pending", user=Depends(require_admin)):
    sql, params = "SELECT * FROM access_requests", []
    if status in ("pending", "approved", "rejected", "invited"):
        sql += " WHERE status=%s"; params.append(status)
    sql += " ORDER BY created_at DESC LIMIT 200"
    with db() as cur:
        cur.execute(sql, params); rows = cur.fetchall()
    return {"requests": [
        {"id": str(r["id"]), "full_name": r["full_name"], "email": r["email"], "org_name": r["org_name"],
         "org_type": r["org_type"], "country": r["country"], "credential_type": r["credential_type"],
         "credential_ref": r["credential_ref"], "message": r["message"], "status": r["status"],
         "created_at": r["created_at"].isoformat()} for r in rows]}


@router.post("/api/admin/requests/{req_id}/approve")
def approve_request(req_id: str, request: Request, user=Depends(require_admin)):
    with db() as cur:
        cur.execute("SELECT email FROM access_requests WHERE id=%s", (req_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "request not found")
        token, inv_id, expires = _new_invite(cur, user["id"], r["email"], "contributor", "from access request")
        cur.execute("UPDATE access_requests SET status='approved', reviewed_by=%s, reviewed_at=now(), "
                    "invitation_id=%s WHERE id=%s", (user["id"], inv_id, req_id))
        audit(cur, user["id"], "request_approve", "access_request", req_id, {"email": r["email"]}, client_ip(request))
    url = f"{SITE_ORIGIN}/portal/?invite={token}"
    emailed = mailer.try_send_invite(r["email"], url, expires, approved=True)
    return {"ok": True, "email": r["email"], "invite_url": url, "emailed": emailed}


@router.post("/api/admin/requests/{req_id}/reject")
def reject_request(req_id: str, user=Depends(require_admin)):
    with db() as cur:
        cur.execute("UPDATE access_requests SET status='rejected', reviewed_by=%s, reviewed_at=now() "
                    "WHERE id=%s RETURNING id", (user["id"], req_id))
        if not cur.fetchone():
            raise HTTPException(404, "request not found")
    return {"ok": True}


# ── invites ──────────────────────────────────────────────────────────────────────
class AdminInviteIn(BaseModel):
    email: Optional[EmailStr] = None
    role:  Optional[str] = "contributor"
    note:  Optional[constr(max_length=300)] = None


@router.post("/api/admin/invites")
def admin_create_invite(body: AdminInviteIn, request: Request, user=Depends(require_admin)):
    role = body.role if body.role in ("contributor", "reviewer", "admin") else "contributor"
    if role == "admin" and not is_admin_email(user["email"]):
        raise HTTPException(403, "Only the owner can issue admin invitations.")
    with db() as cur:
        token, inv_id, expires = _new_invite(cur, user["id"], body.email, role, body.note)
        audit(cur, user["id"], "admin_invite", "invitation", inv_id, {"email": body.email, "role": role}, client_ip(request))
    url = f"{SITE_ORIGIN}/portal/?invite={token}"
    emailed = False
    if body.email:
        emailed = mailer.try_send_invite(body.email, url, expires, inviter=user["full_name"])
    return {"ok": True, "id": str(inv_id), "invite_url": url, "emailed": emailed}


@router.get("/api/admin/invites")
def admin_invites(user=Depends(require_admin)):
    with db() as cur:
        cur.execute("SELECT i.id,i.email,i.intended_role,i.note,i.expires_at,i.used_at,i.revoked_at,"
                    "i.created_at,c.full_name AS creator,uu.full_name AS used_by_name FROM invitations i "
                    "LEFT JOIN users c ON c.id=i.created_by LEFT JOIN users uu ON uu.id=i.used_by "
                    "ORDER BY i.created_at DESC LIMIT 200")
        rows = cur.fetchall()
    now = dt.datetime.now(dt.timezone.utc)
    def st(r):
        if r["used_at"]:    return "used"
        if r["revoked_at"]: return "revoked"
        if r["expires_at"] <= now: return "expired"
        return "active"
    return {"invites": [
        {"id": str(r["id"]), "email": r["email"], "role": r["intended_role"], "note": r["note"],
         "status": st(r), "creator": r["creator"], "used_by": r["used_by_name"],
         "expires_at": r["expires_at"].isoformat(), "created_at": r["created_at"].isoformat()} for r in rows]}


@router.post("/api/admin/invites/{invite_id}/revoke")
def admin_revoke_invite(invite_id: str, user=Depends(require_admin)):
    with db() as cur:
        cur.execute("UPDATE invitations SET revoked_at=now() "
                    "WHERE id=%s AND used_at IS NULL AND revoked_at IS NULL RETURNING id", (invite_id,))
        if not cur.fetchone():
            raise HTTPException(409, "invite not found, already used, or already revoked")
    return {"ok": True}


# ── materials (the trust gate) ───────────────────────────────────────────────────
@router.get("/api/admin/materials")
def admin_materials(uploader: Optional[str] = None, status: Optional[str] = None,
                    q: Optional[str] = None, user=Depends(require_admin)):
    sql = ("SELECT m.id,m.title,m.source_type,m.status,m.original_filename,m.size_bytes,m.created_at,"
           "m.uploaded_by,u.full_name AS uploader,u.email AS uploader_email "
           "FROM materials m JOIN users u ON u.id=m.uploaded_by WHERE 1=1")
    params = []
    if uploader:
        sql += " AND m.uploaded_by=%s"; params.append(uploader)
    if status in ("pending_review", "approved", "rejected", "processing", "error"):
        sql += " AND m.status=%s"; params.append(status)
    if q:
        sql += " AND m.title ILIKE %s"; params.append(f"%{q}%")
    sql += " ORDER BY m.created_at DESC LIMIT 300"
    with db() as cur:
        cur.execute(sql, params); rows = cur.fetchall()
    return {"materials": [
        {"id": str(r["id"]), "title": r["title"], "source_type": r["source_type"], "status": r["status"],
         "filename": r["original_filename"], "size_bytes": r["size_bytes"], "uploader": r["uploader"],
         "uploader_email": r["uploader_email"], "uploader_id": str(r["uploaded_by"]),
         "created_at": r["created_at"].isoformat()} for r in rows]}


class MaterialReview(BaseModel):
    status:       str
    review_notes: Optional[constr(max_length=2000)] = None


@router.post("/api/admin/materials/{material_id}/review")
def review_material(material_id: str, body: MaterialReview, request: Request, user=Depends(require_admin)):
    if body.status not in ("approved", "rejected", "pending_review"):
        raise HTTPException(400, "invalid status")
    with db() as cur:
        cur.execute("UPDATE materials SET status=%s, review_notes=%s, reviewed_by=%s, reviewed_at=now() "
                    "WHERE id=%s RETURNING id", (body.status, body.review_notes, user["id"], material_id))
        if not cur.fetchone():
            raise HTTPException(404, "material not found")
        audit(cur, user["id"], "material_review", "material", material_id, {"status": body.status}, client_ip(request))
    return {"ok": True}


# ── forum moderation + audit ─────────────────────────────────────────────────────
@router.post("/api/admin/forum/topics/{topic_id}/remove")
def remove_topic(topic_id: str, user=Depends(require_admin)):
    with db() as cur:
        cur.execute("UPDATE forum_topics SET status='removed' WHERE id=%s RETURNING id", (topic_id,))
        if not cur.fetchone():
            raise HTTPException(404, "not found")
    return {"ok": True}


@router.get("/api/admin/audit")
def audit_log(limit: int = 100, user=Depends(require_admin)):
    limit = max(1, min(limit, 300))
    with db() as cur:
        cur.execute("SELECT a.created_at,a.action,a.entity_type,a.entity_id,a.detail,a.ip,"
                    "u.full_name,u.email FROM audit_log a LEFT JOIN users u ON u.id=a.user_id "
                    "ORDER BY a.created_at DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    return {"events": [
        {"at": r["created_at"].isoformat(), "action": r["action"], "entity": r["entity_type"],
         "entity_id": r["entity_id"], "detail": r["detail"], "ip": r["ip"],
         "who": r["full_name"] or r["email"] or "—"} for r in rows]}
