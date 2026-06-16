"""
Admin panel API — every endpoint requires role='admin' (require_admin). The
English-only /portal/admin.html consumes these. Covers: overview stats, member
management, access-request review (approve -> issues an invite), invite
management, materials (the trust gate, filterable by uploader), forum moderation,
and the audit log.
"""
import secrets, hashlib, shutil, datetime as dt
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, EmailStr, constr

from core import db, audit, client_ip, require_admin, is_admin_email, SITE_ORIGIN
from library import _is_degenerate, _FTS, kind_counts   # quality detector + title FTS + corpus kind counts
import mailer

router = APIRouter()

_TR_LANGS = {"en", "tr", "es", "de", "fr", "it", "ru", "nl"}


@router.get("/api/admin/system")
def system(user=Depends(require_admin)):
    """Server (openelpis-db) disk + memory + swap, for the admin panel."""
    du = shutil.disk_usage("/")
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                parts = v.split()
                if parts and parts[0].isdigit():
                    mem[k.strip()] = int(parts[0]) * 1024   # kB → bytes
    except Exception:
        pass
    return {
        "disk": {"total": du.total, "used": du.used, "free": du.free},
        "mem":  {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable")},
        "swap": {"total": mem.get("SwapTotal"), "free": mem.get("SwapFree")},
    }


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
    return {**{k: s[k] for k in s}, "kinds": kind_counts()["counts"]}


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


# ── cached translations (management) ─────────────────────────────────────────────
@router.get("/api/admin/translations")
def admin_translations(lang: Optional[str] = None, flagged: int = 0, user=Depends(require_admin)):
    """List cached machine translations with a health flag (degenerate = looks like an LLM
    repetition loop). `flagged=1` shows only the broken ones; `lang` filters by language."""
    sql = ("SELECT t.material_id, t.lang, t.engine, t.truncated, t.created_at, t.title AS tr_title, "
           "t.content, m.title AS src_title "
           "FROM article_translations t JOIN materials m ON m.id=t.material_id WHERE 1=1")
    params = []
    if lang in _TR_LANGS:
        sql += " AND t.lang=%s"; params.append(lang)
    sql += " ORDER BY t.created_at DESC LIMIT 300"
    with db() as cur:
        cur.execute(sql, params); rows = cur.fetchall()
    items, flagged_count = [], 0
    for r in rows:
        content = r["content"] or ""
        degen = _is_degenerate(content, None)
        if degen:
            flagged_count += 1
        if flagged and not degen:
            continue
        items.append({
            "material_id": str(r["material_id"]), "lang": r["lang"], "engine": r["engine"],
            "truncated": r["truncated"], "chars": len(content), "degenerate": degen,
            "tr_title": r["tr_title"], "src_title": r["src_title"],
            "preview": content[:200], "created_at": r["created_at"].isoformat()})
    return {"translations": items, "flagged_count": flagged_count, "scanned": len(rows)}


@router.get("/api/admin/translations/{material_id}/{lang}")
def admin_translation_view(material_id: str, lang: str, user=Depends(require_admin)):
    with db() as cur:
        cur.execute("SELECT t.title, t.content, t.engine, t.truncated, t.created_at, m.title AS src_title "
                    "FROM article_translations t JOIN materials m ON m.id=t.material_id "
                    "WHERE t.material_id=%s AND t.lang=%s", (material_id, lang))
        r = cur.fetchone()
    if not r:
        raise HTTPException(404, "translation not found")
    content = r["content"] or ""
    return {"material_id": material_id, "lang": lang, "title": r["title"], "src_title": r["src_title"],
            "content": content, "engine": r["engine"], "truncated": r["truncated"],
            "degenerate": _is_degenerate(content, None), "created_at": r["created_at"].isoformat()}


@router.post("/api/admin/translations/{material_id}/{lang}/delete")
def admin_translation_delete(material_id: str, lang: str, request: Request, user=Depends(require_admin)):
    with db() as cur:
        cur.execute("DELETE FROM article_translations WHERE material_id=%s AND lang=%s RETURNING material_id",
                    (material_id, lang))
        if not cur.fetchone():
            raise HTTPException(404, "translation not found")
        audit(cur, user["id"], "translation_delete", "material", material_id, {"lang": lang}, client_ip(request))
    return {"ok": True}     # re-translated automatically next time a member opens it


@router.post("/api/admin/translations/purge-flagged")
def admin_translations_purge(request: Request, user=Depends(require_admin)):
    """Delete every cached translation that looks degenerate (repetition loop). Each is
    re-translated cleanly the next time a member requests it."""
    deleted = 0
    with db() as cur:
        cur.execute("SELECT material_id, lang, content FROM article_translations")
        bad = [(r["material_id"], r["lang"]) for r in cur.fetchall() if _is_degenerate(r["content"] or "", None)]
        for mid, lg in bad:
            cur.execute("DELETE FROM article_translations WHERE material_id=%s AND lang=%s", (mid, lg))
            deleted += 1
        if deleted:
            audit(cur, user["id"], "translation_purge_flagged", "translation", None, {"deleted": deleted}, client_ip(request))
    return {"ok": True, "deleted": deleted}


# ── article TITLE translation coverage (matrix view) ──────────────────────────────
_TITLE_TARGETS = ["tr", "es", "de", "fr", "it", "ru", "nl"]   # English stays canonical


@router.get("/api/admin/title-translations")
def admin_title_translations(q: Optional[str] = None, page: int = 1, limit: int = 50,
                             user=Depends(require_admin)):
    """A coverage matrix: one row per approved article (title + which target languages
    have a stored title translation), for the admin Title-status tab. Paginated."""
    limit = max(1, min(limit, 200))
    page = max(1, page)
    where = ["m.status='approved'"]
    params = {}
    if q and q.strip():
        where.append(f"{_FTS} @@ websearch_to_tsquery('english', %(q)s)")   # uses the partial GIN index
        params["q"] = q.strip()
    where_sql = " AND ".join(where)
    offset = min((page - 1) * limit, 1000000)
    with db() as cur:
        cur.execute(f"SELECT count(*) AS n FROM materials m WHERE {where_sql}", params)
        total = cur.fetchone()["n"]
        cur.execute(f"SELECT m.id, m.title FROM materials m WHERE {where_sql} "
                    f"ORDER BY m.id LIMIT {limit} OFFSET {offset}", params)
        rows = cur.fetchall()
        ids = [str(r["id"]) for r in rows]
        cov = {i: set() for i in ids}
        if ids:
            cur.execute("SELECT material_id, lang FROM material_title_translations "
                        "WHERE material_id = ANY(%s::uuid[])", (ids,))
            for r in cur.fetchall():
                cov[str(r["material_id"])].add(r["lang"])
        cur.execute("SELECT lang, count(*) AS n FROM material_title_translations GROUP BY lang")
        totals = {r["lang"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT count(*) AS n FROM materials WHERE status='approved'")
        approved_total = cur.fetchone()["n"]
    pages = max(1, -(-total // limit))
    items = [{"id": str(r["id"]), "title": r["title"] or "",
              "langs": sorted(cov[str(r["id"])] & set(_TITLE_TARGETS))} for r in rows]
    return {"items": items, "page": page, "pages": pages, "total": total,
            "langs": _TITLE_TARGETS, "approved_total": approved_total,
            "totals": {l: totals.get(l, 0) for l in _TITLE_TARGETS}}


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
