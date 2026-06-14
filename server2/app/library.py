"""
Library: browse / search the approved corpus + a per-article reading view with
on-demand machine translation (Groq, cached), up/down votes, favorites, member
comments, and a natural-language ("ask the copilot") search.

Scales with continuous inserts + concurrent uploads:
  - FTS via the partial GIN index (materials_lib_fts); user input parsed safely.
  - Keyset pagination (created_at,id) so results don't shift while rows stream in.
  - Partial indexes on status='approved' → pending uploads never affect this.
  - Social stats (votes/comments/favorite) are fetched only for the ≤50 ids on the
    current page — never an aggregate join across the whole growing table.
"""
import json, base64
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, constr, conint

from core import db, audit, client_ip, current_user
from materials import _visible, material_text          # reuse storage + visibility
from llm import groq_chat, GROQ_ENABLED, GROQ_MODEL

router = APIRouter()

LANGS = {"en", "tr", "es", "de", "fr", "it", "ru", "nl"}
LANG_NAMES = {"en": "English", "tr": "Turkish", "es": "Spanish", "de": "German",
              "fr": "French", "it": "Italian", "ru": "Russian", "nl": "Dutch"}
TRANSLATE_CAP = 30000   # chars of body translated on demand (English original stays canonical)

# Same searchable vector as the partial GIN index materials_lib_fts (title + authors
# + journal + abstract/description). One box → all of those for a keyword.
_FTS = ("to_tsvector('english', coalesce(m.title,'')||' '||coalesce(m.metadata->>'author_string','')"
        "||' '||coalesce(m.metadata->'journal'->>'title','')||' '||coalesce(m.description,''))")
# Sort key for new/old = the article's PUBLISHED date (not the row's DB insert time).
# ISO 'YYYY-MM-DD' sorts chronologically as text; fall back to pub_year, then epoch-floor.
_PUBDATE = ("coalesce(m.metadata->>'first_pub_date', nullif(m.metadata->>'pub_year','')||'-01-01', '0001-01-01')")


def _enc(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()


def _dec(s):
    try:
        return json.loads(base64.urlsafe_b64decode(s.encode()).decode())
    except Exception:
        return None


def _stats(cur, ids, uid):
    """Votes/comments/favorite + the caller's own vote, for just the page's ids."""
    base = {i: {"up": 0, "down": 0, "comments": 0, "my_vote": 0, "favorited": False} for i in ids}
    if not ids:
        return base
    cur.execute("SELECT material_id, count(*) FILTER (WHERE value=1) AS up, "
                "count(*) FILTER (WHERE value=-1) AS down FROM material_votes "
                "WHERE material_id = ANY(%s::uuid[]) GROUP BY material_id", (ids,))
    for r in cur.fetchall():
        base[str(r["material_id"])].update(up=r["up"], down=r["down"])
    cur.execute("SELECT material_id, count(*) AS c FROM material_comments "
                "WHERE material_id = ANY(%s::uuid[]) AND status='visible' GROUP BY material_id", (ids,))
    for r in cur.fetchall():
        base[str(r["material_id"])]["comments"] = r["c"]
    cur.execute("SELECT material_id, value FROM material_votes "
                "WHERE user_id=%s AND material_id = ANY(%s::uuid[])", (uid, ids))
    for r in cur.fetchall():
        base[str(r["material_id"])]["my_vote"] = r["value"]
    cur.execute("SELECT material_id FROM material_favorites "
                "WHERE user_id=%s AND material_id = ANY(%s::uuid[])", (uid, ids))
    for r in cur.fetchall():
        base[str(r["material_id"])]["favorited"] = True
    return base


# ── browse / search ─────────────────────────────────────────────────────────────
@router.get("/api/library")
def library(q: Optional[str] = None, author: Optional[str] = None, journal: Optional[str] = None,
            license: Optional[str] = None, source_type: Optional[str] = None,
            year_from: Optional[str] = None, year_to: Optional[str] = None,
            favorites: int = 0, sort: str = "new", cursor: Optional[str] = None,
            limit: int = 24, user=Depends(current_user)):
    limit = max(1, min(limit, 50))
    where = ["m.status='approved'"]
    params = {}
    tsq_parts = []
    if q:       tsq_parts.append("websearch_to_tsquery('english', %(q)s)");  params["q"] = q
    if author:  tsq_parts.append("plainto_tsquery('english', %(author)s)");  params["author"] = author
    if journal: tsq_parts.append("plainto_tsquery('english', %(journal)s)"); params["journal"] = journal
    has_fts = bool(tsq_parts)
    tsq = " && ".join(tsq_parts) if has_fts else None
    if has_fts:
        where.append(f"{_FTS} @@ ({tsq})")
    if license:
        where.append("m.metadata @> %(licj)s::jsonb"); params["licj"] = json.dumps({"license": license})
    if source_type:
        where.append("m.source_type = %(st)s"); params["st"] = source_type
    if year_from:
        where.append("(m.metadata->>'pub_year') >= %(yf)s"); params["yf"] = str(year_from)
    if year_to:
        where.append("(m.metadata->>'pub_year') <= %(yt)s"); params["yt"] = str(year_to)
    only_favs = bool(favorites)
    if only_favs:
        where.append("EXISTS (SELECT 1 FROM material_favorites f WHERE f.material_id=m.id "
                     "AND f.user_id=%(uid)s)"); params["uid"] = user["id"]

    relevance = (sort == "relevance" and has_fts)
    if relevance:
        off = 0
        if cursor:
            d = _dec(cursor)
            if isinstance(d, int): off = max(0, min(d, 5000))
        page = f"ORDER BY ts_rank({_FTS}, ({tsq})) DESC, m.id DESC LIMIT {limit+1} OFFSET {off}"
    else:
        desc = (sort != "old")
        if cursor:
            d = _dec(cursor) or {}
            if d.get("ca"):
                where.append(f"({_PUBDATE}, m.id) {'<' if desc else '>'} (%(cca)s, %(cci)s::uuid)")
                params["cca"] = d["ca"]; params["cci"] = d["ci"]
        page = (f"ORDER BY {_PUBDATE} {'DESC' if desc else 'ASC'}, m.id {'DESC' if desc else 'ASC'} "
                f"LIMIT {limit+1}")

    sql = ("SELECT m.id,m.title,m.source_type,m.created_at,m.size_bytes,"
           f"{_PUBDATE} AS sort_date,"
           "m.metadata->>'author_string' AS authors,m.metadata->'journal'->>'title' AS journal,"
           "m.metadata->>'pub_year' AS year,m.metadata->>'license' AS license,"
           "m.metadata->>'doi' AS doi,m.metadata->>'url' AS url "
           f"FROM materials m WHERE {' AND '.join(where)} {page}")
    with db() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        ids = [str(r["id"]) for r in rows]
        stats = _stats(cur, ids, user["id"])
        total = None
        if not (has_fts or license or source_type or year_from or year_to or only_favs):
            cur.execute("SELECT count(*) AS n FROM materials WHERE status='approved'")
            total = cur.fetchone()["n"]
    nxt = None
    if more:
        if relevance:
            nxt = _enc(off + limit)
        else:
            last = rows[-1]
            nxt = _enc({"ca": last["sort_date"], "ci": str(last["id"])})
    items = [{"id": str(r["id"]), "title": r["title"], "authors": r["authors"], "journal": r["journal"],
              "year": r["year"], "license": r["license"], "doi": r["doi"], "url": r["url"],
              "source_type": r["source_type"], "size_bytes": r["size_bytes"],
              "created_at": r["created_at"].isoformat(), **stats[str(r["id"])]} for r in rows]
    return {"items": items, "next": nxt, "total": total}


# ── article reading view (preview) ───────────────────────────────────────────────
def _fetch_visible(cur, material_id, user):
    cur.execute("SELECT id,uploaded_by,status,storage_key,title,description,language,metadata "
                "FROM materials WHERE id=%s", (material_id,))
    m = cur.fetchone()
    if not m:
        raise HTTPException(404, "not found")
    if not _visible(cur, m, user):
        raise HTTPException(403, "not allowed")
    return m


@router.get("/api/library/{material_id}")
def article(material_id: str, user=Depends(current_user)):
    with db() as cur:
        m = _fetch_visible(cur, material_id, user)
        st = _stats(cur, [str(m["id"])], user["id"])[str(m["id"])]
    md = m["metadata"] or {}
    text = material_text(m)
    return {
        "id": str(m["id"]), "title": m["title"], "abstract": m["description"],
        "language": m["language"] or "en", "content": text,
        "clean": str(md.get("extract_v")) in ("2", "3"),  # re-extracted <body>-only (no front-matter)
        "body_kind": md.get("body_kind"),
        "authors": md.get("author_string"),
        "journal": (md.get("journal") or {}).get("title") if isinstance(md.get("journal"), dict) else None,
        "year": md.get("pub_year"), "license": md.get("license"),
        "doi": md.get("doi"), "url": md.get("url"), "pmcid": md.get("pmcid"),
        "copyright": md.get("copyright"), **st,
    }


class TranslateIn(BaseModel):
    lang: constr(min_length=2, max_length=5)


def _split(text, cap):
    """Split into <=cap-char blocks on paragraph boundaries."""
    out, buf = [], ""
    for para in text.split("\n"):
        if len(buf) + len(para) + 1 > cap and buf:
            out.append(buf); buf = ""
        buf += para + "\n"
    if buf.strip():
        out.append(buf)
    return out


@router.post("/api/library/{material_id}/translate")
def translate(material_id: str, body: TranslateIn, user=Depends(current_user)):
    lang = body.lang if body.lang in LANGS else None
    if not lang:
        raise HTTPException(400, "unsupported language")
    with db() as cur:
        m = _fetch_visible(cur, material_id, user)
        if lang == (m["language"] or "en"):     # already in this language → original is canonical
            return {"available": True, "lang": lang, "engine": "original",
                    "title": m["title"], "content": material_text(m), "cached": True}
        cur.execute("SELECT title,content,engine,truncated FROM article_translations "
                    "WHERE material_id=%s AND lang=%s", (material_id, lang))
        cached = cur.fetchone()
        if cached:
            return {"available": True, "lang": lang, "cached": True, "engine": cached["engine"],
                    "title": cached["title"], "content": cached["content"], "truncated": cached["truncated"]}
    if not GROQ_ENABLED:
        # Groq key not configured yet — the English original stays available.
        return {"available": False, "lang": lang}

    text = material_text(m) or ""
    truncated = len(text) > TRANSLATE_CAP
    text = text[:TRANSLATE_CAP]
    name = LANG_NAMES.get(lang, lang)
    sys = (f"You are a professional medical translator. Translate the user's text into {name}, "
           "preserving meaning and clinical/medical terminology precisely. Keep paragraph breaks. "
           "Output ONLY the translation — no preamble, notes, or quotes.")
    parts = []
    for block in _split(text, 3500):
        out = groq_chat([{"role": "system", "content": sys}, {"role": "user", "content": block}],
                        max_tokens=4096, temperature=0.1)
        if out is None:
            raise HTTPException(503, "translation_failed")
        parts.append(out)
    content = "\n\n".join(parts).strip()
    title = m["title"]
    if title:
        tt = groq_chat([{"role": "system", "content": f"Translate into {name}. Output only the translation."},
                        {"role": "user", "content": title}], max_tokens=256, temperature=0.1)
        title = tt or m["title"]
    engine = f"groq:{GROQ_MODEL}"
    with db() as cur:
        cur.execute("INSERT INTO article_translations(material_id,lang,title,content,engine,truncated) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (material_id,lang) DO UPDATE SET "
                    "title=EXCLUDED.title,content=EXCLUDED.content,engine=EXCLUDED.engine,"
                    "truncated=EXCLUDED.truncated,created_at=now()",
                    (material_id, lang, title, content, engine, truncated))
        audit(cur, user["id"], "translate", "material", material_id, {"lang": lang}, None)
    return {"available": True, "lang": lang, "cached": False, "engine": engine,
            "title": title, "content": content, "truncated": truncated}


# ── votes / favorites ────────────────────────────────────────────────────────────
class VoteIn(BaseModel):
    value: conint(ge=-1, le=1)     # 1 up, -1 down, 0 clears


@router.post("/api/library/{material_id}/vote")
def vote(material_id: str, body: VoteIn, request: Request, user=Depends(current_user)):
    with db() as cur:
        m = _fetch_visible(cur, material_id, user)
        if body.value == 0:
            cur.execute("DELETE FROM material_votes WHERE material_id=%s AND user_id=%s",
                        (material_id, user["id"]))
        else:
            cur.execute("INSERT INTO material_votes(material_id,user_id,value) VALUES (%s,%s,%s) "
                        "ON CONFLICT (material_id,user_id) DO UPDATE SET value=EXCLUDED.value,updated_at=now()",
                        (material_id, user["id"], body.value))
        st = _stats(cur, [str(m["id"])], user["id"])[str(m["id"])]
    return {"ok": True, "up": st["up"], "down": st["down"], "my_vote": st["my_vote"]}


@router.post("/api/library/{material_id}/favorite")
def favorite(material_id: str, user=Depends(current_user)):
    with db() as cur:
        m = _fetch_visible(cur, material_id, user)
        cur.execute("SELECT 1 FROM material_favorites WHERE user_id=%s AND material_id=%s",
                    (user["id"], material_id))
        if cur.fetchone():
            cur.execute("DELETE FROM material_favorites WHERE user_id=%s AND material_id=%s",
                        (user["id"], material_id))
            fav = False
        else:
            cur.execute("INSERT INTO material_favorites(user_id,material_id) VALUES (%s,%s)",
                        (user["id"], material_id))
            fav = True
    return {"ok": True, "favorited": fav}


# ── comments ─────────────────────────────────────────────────────────────────────
@router.get("/api/library/{material_id}/comments")
def list_comments(material_id: str, user=Depends(current_user)):
    with db() as cur:
        _fetch_visible(cur, material_id, user)
        cur.execute("SELECT c.id,c.body,c.reason,c.created_at,c.user_id,u.full_name,u.specialty "
                    "FROM material_comments c JOIN users u ON u.id=c.user_id "
                    "WHERE c.material_id=%s AND c.status='visible' ORDER BY c.created_at DESC LIMIT 200",
                    (material_id,))
        rows = cur.fetchall()
    return {"comments": [
        {"id": str(r["id"]), "body": r["body"], "reason": r["reason"],
         "author": r["full_name"], "specialty": r["specialty"],
         "mine": str(r["user_id"]) == str(user["id"]),
         "created_at": r["created_at"].isoformat()} for r in rows]}


class CommentIn(BaseModel):
    body:   constr(min_length=1, max_length=4000)
    reason: Optional[constr(max_length=60)] = None


@router.post("/api/library/{material_id}/comments")
def add_comment(material_id: str, body: CommentIn, request: Request, user=Depends(current_user)):
    with db() as cur:
        _fetch_visible(cur, material_id, user)
        cur.execute("INSERT INTO material_comments(material_id,user_id,body,reason) "
                    "VALUES (%s,%s,%s,%s) RETURNING id,created_at",
                    (material_id, user["id"], body.body.strip(), (body.reason or "").strip() or None))
        r = cur.fetchone()
        audit(cur, user["id"], "comment", "material", material_id, {}, client_ip(request))
    return {"ok": True, "id": str(r["id"]), "created_at": r["created_at"].isoformat()}


@router.delete("/api/library/comments/{comment_id}")
def delete_comment(comment_id: str, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT user_id FROM material_comments WHERE id=%s AND status='visible'", (comment_id,))
        c = cur.fetchone()
        if not c:
            raise HTTPException(404, "not found")
        if str(c["user_id"]) != str(user["id"]) and user["role"] != "admin":
            raise HTTPException(403, "not allowed")
        cur.execute("UPDATE material_comments SET status='removed' WHERE id=%s", (comment_id,))
    return {"ok": True}


# ── natural-language search ("ask the copilot to search the library") ─────────────
class AskIn(BaseModel):
    question: constr(min_length=1, max_length=500)


@router.post("/api/library/ask")
def library_ask(body: AskIn, user=Depends(current_user)):
    """Turn a plain-language request into Library filters. With Groq it extracts
    structured filters; without it, the text is used as a keyword search. The frontend
    applies the returned params to the filter fields and runs the normal search."""
    q = body.question.strip()
    params = {"q": q}
    interpreted = False
    if GROQ_ENABLED:
        sys = ("Extract search filters from a clinician's request about a breast-cancer article "
               "library. Reply with ONLY compact JSON, keys (all optional): q (main keywords), "
               "author, journal, year_from, year_to. Omit keys you can't infer. No prose.")
        raw = groq_chat([{"role": "system", "content": sys}, {"role": "user", "content": q}],
                        max_tokens=200, temperature=0)
        if raw:
            try:
                j = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
                cleaned = {k: str(v) for k, v in j.items()
                           if k in ("q", "author", "journal", "year_from", "year_to") and v}
                if cleaned:
                    params = cleaned; interpreted = True
            except (ValueError, AttributeError):
                pass
    return {"params": params, "interpreted": interpreted, "groq": GROQ_ENABLED}
