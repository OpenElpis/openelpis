"""
Materials: upload, list-mine, and a visibility-checked file download (so a member
can open a document another member shared into the forum or a DM).
"""
import re, os, hashlib, datetime as dt, uuid, pathlib
from typing import Optional

from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, constr

from core import db, audit, client_ip, current_user, require_reviewer
from shares import material_shared_with

router = APIRouter()

UPLOAD_DIR = pathlib.Path(os.environ.get("UPLOAD_DIR", "/var/lib/openelpis/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD = 25 * 1024 * 1024  # 25 MB
ALLOWED_MIME = {
    "application/pdf", "text/plain", "text/csv", "text/markdown",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/json",
}
SRC = {"literature", "dataset", "finding", "report", "guideline", "other"}


def _visible(cur, m, user) -> bool:
    """m: row with id, uploaded_by, status. Visible to owner, admin, when approved,
    or when shared somewhere the user can see it."""
    if (str(m["uploaded_by"]) == str(user["id"]) or user["role"] == "admin"
            or m["status"] == "approved"):
        return True
    return material_shared_with(cur, m["id"], user["id"])


@router.post("/api/materials")
async def upload_material(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    source_type: str = Form("literature"),
    description: str = Form(""),
    user=Depends(current_user),
):
    stype = source_type if source_type in SRC else "other"
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(415, f"file type not allowed: {file.content_type}")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large (max 25 MB)")
    if not data:
        raise HTTPException(400, "empty file")
    sha  = hashlib.sha256(data).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", (file.filename or "upload"))[:120]
    key  = f"{dt.date.today():%Y/%m}/{uuid.uuid4().hex}-{safe}"
    dest = UPLOAD_DIR / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    with db() as cur:
        cur.execute("SELECT id FROM materials WHERE sha256=%s", (sha,))
        if cur.fetchone():
            raise HTTPException(409, "this exact file has already been uploaded")
        dest.write_bytes(data)
        cur.execute(
            "INSERT INTO materials(uploaded_by,org_id,title,description,source_type,"
            "storage_backend,storage_key,original_filename,mime_type,size_bytes,sha256) "
            "VALUES (%s,%s,%s,%s,%s,'local',%s,%s,%s,%s,%s) RETURNING id,status",
            (user["id"], user["org_id"], title, description or None, stype, key,
             file.filename, file.content_type, len(data), sha))
        m = cur.fetchone()
        audit(cur, user["id"], "upload", "material", m["id"], {"title": title, "bytes": len(data)}, client_ip(request))
    return {"ok": True, "message": "Uploaded. It's pending expert review before it enters the corpus.",
            "material": {"id": str(m["id"]), "title": title, "status": m["status"]}}


@router.get("/api/materials")
def my_materials(user=Depends(current_user)):
    with db() as cur:
        cur.execute(
            "SELECT id,title,source_type,status,original_filename,size_bytes,created_at "
            "FROM materials WHERE uploaded_by=%s ORDER BY created_at DESC LIMIT 200", (user["id"],))
        rows = cur.fetchall()
    return {"materials": [
        {"id": str(r["id"]), "title": r["title"], "source_type": r["source_type"], "status": r["status"],
         "filename": r["original_filename"], "size_bytes": r["size_bytes"],
         "created_at": r["created_at"].isoformat()} for r in rows]}


@router.get("/api/materials/{material_id}/file")
def material_file(material_id: str, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT id,uploaded_by,status,storage_key,original_filename,mime_type "
                    "FROM materials WHERE id=%s", (material_id,))
        m = cur.fetchone()
        if not m:
            raise HTTPException(404, "not found")
        if not _visible(cur, m, user):
            raise HTTPException(403, "not allowed")
    path = UPLOAD_DIR / m["storage_key"]
    if not path.exists():
        raise HTTPException(410, "file missing")
    return FileResponse(path, media_type=m["mime_type"] or "application/octet-stream",
                        filename=m["original_filename"] or "material")


# ── reviewer queue (role 'reviewer' or 'admin', from the member dashboard) ──────
class ReviewIn(BaseModel):
    status:       str
    review_notes: Optional[constr(max_length=2000)] = None


@router.get("/api/review/queue")
def review_queue(status: Optional[str] = None, user=Depends(require_reviewer)):
    st = status if status in ("pending_review", "approved", "rejected") else "pending_review"
    with db() as cur:
        cur.execute(
            "SELECT m.id,m.title,m.source_type,m.status,m.original_filename,m.size_bytes,m.created_at,"
            "u.full_name AS uploader,u.email AS uploader_email FROM materials m JOIN users u ON u.id=m.uploaded_by "
            "WHERE m.status=%s AND m.uploaded_by<>%s ORDER BY m.created_at LIMIT 200", (st, user["id"]))
        rows = cur.fetchall()
    return {"materials": [
        {"id": str(r["id"]), "title": r["title"], "source_type": r["source_type"], "status": r["status"],
         "filename": r["original_filename"], "size_bytes": r["size_bytes"],
         "uploader": r["uploader"], "uploader_email": r["uploader_email"],
         "created_at": r["created_at"].isoformat()} for r in rows]}


@router.post("/api/review/materials/{material_id}")
def review_one(material_id: str, body: ReviewIn, request: Request, user=Depends(require_reviewer)):
    if body.status not in ("approved", "rejected"):
        raise HTTPException(400, "status must be approved or rejected")
    with db() as cur:
        cur.execute("SELECT uploaded_by FROM materials WHERE id=%s", (material_id,))
        m = cur.fetchone()
        if not m:
            raise HTTPException(404, "material not found")
        if str(m["uploaded_by"]) == str(user["id"]):
            raise HTTPException(403, "you can't review your own upload")
        cur.execute("UPDATE materials SET status=%s, review_notes=%s, reviewed_by=%s, reviewed_at=now() WHERE id=%s",
                    (body.status, body.review_notes, user["id"], material_id))
        audit(cur, user["id"], "material_review", "material", material_id,
              {"status": body.status, "via": "reviewer"}, client_ip(request))
    return {"ok": True}
