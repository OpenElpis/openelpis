"""
OpenElpis clinician portal — backend API (FastAPI).
Runs on server2 (localhost:8000), behind server1's Caddy at openelpis.com/api/*.

Endpoints (all JSON, prefix /api):
  GET  /api/health
  POST /api/signup        {email, password, full_name, org_name?, org_type?, country?}
  POST /api/login         {email, password}          -> sets httpOnly cookie
  POST /api/logout
  GET  /api/me
  POST /api/materials     (multipart: file, title, source_type?, description?)  [auth]
  GET  /api/materials     -> the caller's own uploads                            [auth]

Security: argon2 password hashing, JWT in an httpOnly+Secure+SameSite cookie,
parameterized SQL, upload size/type limits, every file content-hashed (provenance).
Secrets (DATABASE_URL, JWT_SECRET) come from /etc/openelpis.env — never hard-coded.
"""
import os, re, json, hashlib, datetime as dt, uuid, pathlib
from typing import Optional

import jwt
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, Request, Response, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, constr

# ── config ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET   = os.environ["JWT_SECRET"]
UPLOAD_DIR   = pathlib.Path(os.environ.get("UPLOAD_DIR", "/var/lib/openelpis/uploads"))
COOKIE_NAME  = "oe_session"
TOKEN_TTL    = dt.timedelta(days=7)
MAX_UPLOAD   = 25 * 1024 * 1024  # 25 MB
ALLOWED_MIME = {
    "application/pdf", "text/plain", "text/csv", "text/markdown",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/json",
}
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ph   = PasswordHasher()
pool = ThreadedConnectionPool(1, 8, dsn=DATABASE_URL)
app  = FastAPI(title="OpenElpis portal API", docs_url=None, redoc_url=None)


# ── db helpers ──────────────────────────────────────────────────────────────
class db:
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


# ── auth helpers ────────────────────────────────────────────────────────────
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
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid session")
    with db() as cur:
        cur.execute("SELECT id,email,full_name,role,verification_status,is_active,org_id "
                    "FROM users WHERE id=%s", (payload["sub"],))
        user = cur.fetchone()
    if not user or not user["is_active"]:
        raise HTTPException(401, "account not found or disabled")
    return user


def client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)


# ── models ──────────────────────────────────────────────────────────────────
class SignupIn(BaseModel):
    email: EmailStr
    password: constr(min_length=10, max_length=200)
    full_name: constr(min_length=2, max_length=200)
    org_name: Optional[constr(max_length=200)] = None
    org_type: Optional[str] = None
    country: Optional[constr(max_length=100)] = None

class LoginIn(BaseModel):
    email: EmailStr
    password: str

ORG_TYPES = {"clinic", "hospital", "lab", "university", "individual", "other"}


# ── routes ──────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    with db() as cur:
        cur.execute("SELECT 1")
    return {"ok": True, "service": "openelpis-portal"}


@app.post("/api/signup")
def signup(body: SignupIn, request: Request):
    otype = body.org_type if body.org_type in ORG_TYPES else "other"
    pw_hash = ph.hash(body.password)
    with db() as cur:
        cur.execute("SELECT 1 FROM users WHERE email=%s", (body.email,))
        if cur.fetchone():
            raise HTTPException(409, "an account with this email already exists")
        org_id = None
        if body.org_name:
            cur.execute("INSERT INTO organizations(name,org_type,country) VALUES (%s,%s,%s) RETURNING id",
                        (body.org_name, otype, body.country))
            org_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO users(org_id,email,password_hash,full_name) VALUES (%s,%s,%s,%s) "
            "RETURNING id,email,full_name,role,verification_status",
            (org_id, body.email, pw_hash, body.full_name))
        user = cur.fetchone()
        audit(cur, user["id"], "signup", "user", user["id"], {"org": body.org_name}, client_ip(request))
    return {"ok": True, "message": "Account created. It's pending verification by our team.",
            "user": {"email": user["email"], "full_name": user["full_name"],
                     "verification_status": user["verification_status"]}}


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    with db() as cur:
        cur.execute("SELECT id,email,full_name,role,password_hash,is_active FROM users WHERE email=%s",
                    (body.email,))
        user = cur.fetchone()
    if not user or not user["is_active"]:
        raise HTTPException(401, "invalid email or password")
    try:
        ph.verify(user["password_hash"], body.password)
    except VerifyMismatchError:
        raise HTTPException(401, "invalid email or password")
    with db() as cur:
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
    return {"email": user["email"], "full_name": user["full_name"], "role": user["role"],
            "verification_status": user["verification_status"]}


@app.post("/api/materials")
async def upload_material(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    source_type: str = Form("literature"),
    description: str = Form(""),
    user=Depends(current_user),
):
    SRC = {"literature", "dataset", "finding", "report", "guideline", "other"}
    stype = source_type if source_type in SRC else "other"
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(415, f"file type not allowed: {file.content_type}")

    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large (max 25 MB)")
    if not data:
        raise HTTPException(400, "empty file")
    sha = hashlib.sha256(data).hexdigest()

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", (file.filename or "upload"))[:120]
    key = f"{dt.date.today():%Y/%m}/{uuid.uuid4().hex}-{safe}"
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
            "VALUES (%s,%s,%s,%s,%s,'local',%s,%s,%s,%s,%s) RETURNING id,status,created_at",
            (user["id"], user["org_id"], title, description or None, stype, key,
             file.filename, file.content_type, len(data), sha))
        m = cur.fetchone()
        audit(cur, user["id"], "upload", "material", m["id"],
              {"title": title, "bytes": len(data)}, client_ip(request))
    return {"ok": True, "message": "Uploaded. It's pending expert review before it enters the corpus.",
            "material": {"id": str(m["id"]), "title": title, "status": m["status"]}}


@app.get("/api/materials")
def my_materials(user=Depends(current_user)):
    with db() as cur:
        cur.execute(
            "SELECT id,title,source_type,status,original_filename,size_bytes,created_at "
            "FROM materials WHERE uploaded_by=%s ORDER BY created_at DESC LIMIT 200", (user["id"],))
        rows = cur.fetchall()
    return {"materials": [
        {"id": str(r["id"]), "title": r["title"], "source_type": r["source_type"],
         "status": r["status"], "filename": r["original_filename"],
         "size_bytes": r["size_bytes"], "created_at": r["created_at"].isoformat()} for r in rows]}
