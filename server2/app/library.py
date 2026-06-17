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
import json, re, threading, time as _time
from collections import Counter
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, constr, conint

from core import db, audit, client_ip, current_user
from chat import record_exchange
from materials import _visible, material_text          # reuse storage + visibility
from llm import groq_chat, GROQ_ENABLED, GROQ_FAST_MODEL, GroqRateLimited, GroqUnavailable

router = APIRouter()

LANGS = {"en", "tr", "es", "de", "fr", "it", "ru", "nl"}
LANG_NAMES = {"en": "English", "tr": "Turkish", "es": "Spanish", "de": "German",
              "fr": "French", "it": "Italian", "ru": "Russian", "nl": "Dutch"}
TRANSLATE_CAP = 60000   # generous — the whole body is translated for ~90% of articles (median ~30k,
                        # p90 ~54k chars); only the rare giants are capped + flagged `truncated`. The
                        # English original always stays canonical.
TR_BLOCK = 2500         # chars per Groq translate call → a "chunk" that streams to the reader as it lands
# Streaming translation: a background thread translates the article block-by-block (rate-limit-aware)
# and appends each finished chunk to _TR_PROGRESS; the frontend polls and renders chunks as they
# arrive (so the reader can start reading the top while the rest translates). On completion the full
# text is cached in article_translations, so later views are instant.
_TR_PROGRESS = {}       # (material_id, lang) -> {chunks:[str], total:int, title:str|None,
                        #                          truncated:bool, done:bool, error:bool}
_TR_LOCK = threading.Lock()

# Same searchable vector as the partial GIN index materials_lib_fts (title + authors
# + journal + abstract/description). One box → all of those for a keyword.
_FTS = ("to_tsvector('english', coalesce(m.title,'')||' '||coalesce(m.metadata->>'author_string','')"
        "||' '||coalesce(m.metadata->'journal'->>'title','')||' '||coalesce(m.description,''))")
# Sort key for new/old = the article's PUBLISHED date (not the row's DB insert time).
# ISO 'YYYY-MM-DD' sorts chronologically as text; fall back to pub_year, then epoch-floor.
_PUBDATE = ("coalesce(m.metadata->>'first_pub_date', nullif(m.metadata->>'pub_year','')||'-01-01', '0001-01-01')")
# Library full-content search. Strategy (keeps every query fast):
#   • BROAD term (≥ _ESCALATE metadata matches) → metadata FTS only, via the partial GIN index
#     (the body would add little — there are already plenty of hits — and scanning it is slow).
#   • SPECIFIC term (< _ESCALATE metadata matches, e.g. "RhoC K188") → also search the BODY
#     (chunks), union the matching ids, and query by id (PK). Both id sets are small → fast.
# This gives the copilot's full-content reach exactly where it matters (specific terms metadata
# can't see) without the seconds-long scans broad-term body search would cost.
_BODY_HIT_CAP = 400      # max body-matched articles folded in for a specific term
_ESCALATE     = 400      # metadata-match count above which we skip the body search

# Filter the list by display-kind (the same taxonomy the counters show). Kept index-friendly:
# pmc ⟺ source='europepmc' (PMC rows never carry metadata.kind); the non-PMC kinds live in the
# small `materials_nonbulk` partial-index set, so we add that predicate to keep the scan tiny.
_KIND_KNOWN = {"pmc", "article", "preprint", "clinical_trial", "book", "dataset"}


def _kind_filter(kind, where, params):
    if kind == "pmc":
        where.append("(m.metadata->>'source')='europepmc'")
    else:
        where.append("(m.metadata->>'source') IS DISTINCT FROM 'europepmc' "
                     "AND coalesce(m.metadata->>'kind','article')=%(kind)s")
        params["kind"] = kind


def _title_tr(cur, ids, lang):
    """Bulk-translated titles for the given ids in `lang`. English is included on purpose:
    an English-source article simply has no row (→ caller keeps the original title), while a
    non-English upload DOES have an 'en' row so it shows in English to English users."""
    if not ids or not lang or lang not in LANGS:
        return {}
    cur.execute("SELECT material_id, title FROM material_title_translations "
                "WHERE lang=%s AND material_id = ANY(%s::uuid[])", (lang, ids))
    return {str(r["material_id"]): r["title"] for r in cur.fetchall()}


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
            year_from: Optional[str] = None, year_to: Optional[str] = None, kind: Optional[str] = None,
            alang: Optional[str] = None, favorites: int = 0, sort: str = "new", page: int = 1,
            limit: int = 24, lang: Optional[str] = None, user=Depends(current_user)):
    limit = max(1, min(limit, 100))
    page = max(1, page)
    where = ["m.status='approved'"]
    params = {}
    if q:
        params["q"] = q                                   # used by the relevance ORDER BY
        meta_cond = f"{_FTS} @@ websearch_to_tsquery('english', %(q)s)"
        with db() as c0:
            c0.execute(f"SELECT count(*) AS n FROM materials m WHERE m.status='approved' AND "
                       f"{_FTS} @@ websearch_to_tsquery('english', %s)", (q,))
            meta_n = c0.fetchone()["n"]
            if meta_n >= _ESCALATE:                        # broad term → fast metadata-index path
                where.append(meta_cond)
            else:                                          # specific term → also search the BODY
                c0.execute(f"SELECT m.id FROM materials m WHERE m.status='approved' AND "
                           f"{_FTS} @@ websearch_to_tsquery('english', %s)", (q,))
                ids = {str(r["id"]) for r in c0.fetchall()}
                c0.execute("SELECT DISTINCT material_id FROM material_chunks "
                           "WHERE tsv @@ websearch_to_tsquery('english', %s) LIMIT %s", (q, _BODY_HIT_CAP))
                ids |= {str(r["material_id"]) for r in c0.fetchall()}
                where.append("m.id = ANY(%(ids)s::uuid[])"); params["ids"] = list(ids)
    if author:  where.append(f"{_FTS} @@ plainto_tsquery('english', %(author)s)");  params["author"] = author
    if journal: where.append(f"{_FTS} @@ plainto_tsquery('english', %(journal)s)"); params["journal"] = journal
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
    if kind in _KIND_KNOWN:
        _kind_filter(kind, where, params)
    if alang:
        where.append("m.language=%(alang)s"); params["alang"] = alang
    # When kind is the ONLY filter, the (un-indexed) count over the whole corpus is slow — reuse
    # the cached corpus counts instead. Any other filter narrows it enough to count directly.
    kind_only = (kind in _KIND_KNOWN and not (q or author or journal or license
                 or source_type or year_from or year_to or only_favs or alang))
    cached_total = kind_counts()["counts"].get(kind, 0) if kind_only else None

    relevance = (sort == "relevance" and bool(q))
    if relevance:
        order = f"ORDER BY ts_rank({_FTS}, websearch_to_tsquery('english', %(q)s)) DESC, m.id DESC"
    else:
        d = "DESC" if sort != "old" else "ASC"
        order = f"ORDER BY {_PUBDATE} {d}, m.id {d}"
    where_sql = " AND ".join(where)
    offset = min((page - 1) * limit, 200000)         # guard pathological deep scans
    sel = ("SELECT m.id,m.title,m.source_type,m.created_at,m.size_bytes,"
           "m.metadata->>'author_string' AS authors,m.metadata->'journal'->>'title' AS journal,"
           "m.metadata->>'pub_year' AS year,m.metadata->>'license' AS license,"
           "m.metadata->>'doi' AS doi,m.metadata->>'url' AS url,"
           "coalesce(m.metadata->>'kind', case when m.metadata->>'source'='europepmc' then 'pmc' else 'article' end) AS kind "
           f"FROM materials m WHERE {where_sql} {order} LIMIT {limit} OFFSET {offset}")
    with db() as cur:
        if cached_total is not None:
            total = cached_total
        else:
            cur.execute(f"SELECT count(*) AS n FROM materials m WHERE {where_sql}", params)
            total = cur.fetchone()["n"]
        cur.execute(sel, params)
        rows = cur.fetchall()
        ids = [str(r["id"]) for r in rows]
        stats = _stats(cur, ids, user["id"])
        titles = _title_tr(cur, ids, lang)           # localized titles (English → original)
    pages = max(1, -(-total // limit))               # ceil division
    items = [{"id": str(r["id"]), "title": titles.get(str(r["id"])) or r["title"],
              "authors": r["authors"], "journal": r["journal"],
              "year": r["year"], "license": r["license"], "doi": r["doi"], "url": r["url"],
              "source_type": r["source_type"], "size_bytes": r["size_bytes"], "kind": r["kind"],
              "created_at": r["created_at"].isoformat(), **stats[str(r["id"])]} for r in rows]
    return {"items": items, "total": total, "page": page, "pages": pages, "limit": limit}


# ── corpus-wide counts per content kind (for the Library + admin counters) ─────────
_KIND_CACHE = {"t": 0.0, "v": None}


def kind_counts():
    """{total, counts:{pmc,article,…}, recent} over approved materials. Cached ~10 min.
    Fast path: the non-PMC rows are few and covered by the partial index `materials_nonbulk`
    (GROUP BY only those); PMC = total − non-PMC (avoids a full jsonb scan). `recent` = items
    added in the last 24h (the live "+N added" delta). Serves the STALE cache on any DB error
    (e.g. a momentary pool spike) so the counters never disappear from the UI."""
    now = _time.time()
    c = _KIND_CACHE
    if c["v"] and now - c["t"] < 600:
        return c["v"]
    try:
        with db() as cur:
            cur.execute("SELECT count(*) AS n FROM materials WHERE status='approved'")
            total = cur.fetchone()["n"]
            cur.execute("SELECT coalesce(metadata->>'kind','article') AS kind, count(*) AS n "
                        "FROM materials WHERE status='approved' "
                        "AND (metadata->>'source') IS DISTINCT FROM 'europepmc' GROUP BY 1")
            counts = {r["kind"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT count(*) AS n FROM materials "
                        "WHERE status='approved' AND created_at > now() - interval '24 hours'")
            recent = cur.fetchone()["n"]
            cur.execute("SELECT coalesce(language,'?') AS lang, count(*) AS n FROM materials "
                        "WHERE status='approved' GROUP BY 1 ORDER BY 2 DESC")
            languages = {r["lang"]: r["n"] for r in cur.fetchall()}
        counts["pmc"] = total - sum(counts.values())
        c["v"] = {"total": total, "counts": counts, "recent": recent, "recent_window": "24h",
                  "languages": languages}
        c["t"] = now
        return c["v"]
    except Exception:
        if c["v"]:
            return c["v"]              # stale-but-present beats a 500 that blanks the counters
        raise


# NOTE: must be declared BEFORE the /api/library/{material_id} route below, or "kinds"
# would be captured as a material_id.
@router.get("/api/library/kinds")
def library_kinds(user=Depends(current_user)):
    return kind_counts()


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
        "copyright": md.get("copyright"),
        "kind": md.get("kind") or ("pmc" if md.get("source") == "europepmc" else "article"),
        "source": md.get("source"), "peer_reviewed": md.get("peer_reviewed"), "nct": md.get("nct"),
        **st,
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


def _groq_backoff(messages, *, retries=6, **kw):
    """groq_chat with bounded waits on 429. ONLY called inside the background translate
    thread (never a request thread), so the sleeps don't block the API for other users."""
    for _ in range(retries):
        try:
            return groq_chat(messages, **kw)
        except GroqRateLimited as e:
            _time.sleep(min((e.retry_after or 18) + 1, 35))
    raise GroqUnavailable("rate limited too long")


def _is_degenerate(text, src_len):
    """True if a translation looks like an LLM repetition loop (e.g. the same phrase emitted
    over and over until the token limit) or is otherwise garbage — so it never gets cached."""
    t = (text or "").strip()
    if not t:
        return True
    if src_len and len(t) > src_len * 3 + 200:          # length blow-up from a loop
        return True
    words = t.split()
    if len(words) < 12:
        return False                                     # too short to judge
    if len(set(words)) / len(words) < 0.25:             # almost no unique words
        return True
    grams = [" ".join(words[i:i + 4]) for i in range(len(words) - 3)]
    if grams:
        _, cnt = Counter(grams).most_common(1)[0]        # most-repeated 4-word phrase
        if cnt >= 8 or cnt / len(grams) > 0.2:
            return True
    return False


def _translate_block(messages_base, block, src_len):
    """Translate one block; if the model loops/garbles, retry at a higher temperature to break
    the loop. Returns clean text, or None if it couldn't produce a sane translation."""
    for temp in (0.1, 0.5, 0.9):
        out = _groq_backoff(messages_base + [{"role": "user", "content": block}],
                            model=GROQ_FAST_MODEL, max_tokens=4096, temperature=temp)
        if out and not _is_degenerate(out, src_len):
            return out
    return None


def _do_translate(material_id, lang, blocks, src_title, name, truncated):
    """Background worker: translate the title, then each block, appending finished chunks to
    _TR_PROGRESS so the frontend can stream them in. Caches the full result when complete."""
    key = (material_id, lang)
    try:
        if src_title:
            tt = _groq_backoff(
                [{"role": "system", "content": f"Translate into {name}. Output only the translation."},
                 {"role": "user", "content": src_title}], model=GROQ_FAST_MODEL, max_tokens=256, temperature=0.1)
            with _TR_LOCK:
                if key in _TR_PROGRESS:
                    _TR_PROGRESS[key]["title"] = tt if (tt and not _is_degenerate(tt, len(src_title))) else src_title
        sysmsg = (f"You are a professional medical translator. Translate the user's text into {name}, "
                  "preserving meaning and clinical/medical terminology precisely. Keep paragraph breaks. "
                  "Do NOT repeat any phrase or sentence; translate the text once, faithfully. "
                  "Output ONLY the translation — no preamble, notes, or quotes.")
        base = [{"role": "system", "content": sysmsg}]
        for block in blocks:
            out = _translate_block(base, block, len(block))
            if out is None:                    # model kept looping/garbling → fail (do NOT cache garbage)
                raise GroqUnavailable("degenerate translation output")
            with _TR_LOCK:
                if key not in _TR_PROGRESS:    # cancelled / evicted
                    return
                _TR_PROGRESS[key]["chunks"].append(out)
        with _TR_LOCK:
            p = _TR_PROGRESS.get(key)
            title = (p and p.get("title")) or src_title
            content = "\n\n".join(c for c in (p["chunks"] if p else []) if c).strip()
        engine = f"groq:{GROQ_FAST_MODEL}"
        with db() as cur:
            cur.execute("INSERT INTO article_translations(material_id,lang,title,content,engine,truncated) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (material_id,lang) DO UPDATE SET "
                        "title=EXCLUDED.title,content=EXCLUDED.content,engine=EXCLUDED.engine,"
                        "truncated=EXCLUDED.truncated,created_at=now()",
                        (material_id, lang, title, content, engine, truncated))
        with _TR_LOCK:
            if key in _TR_PROGRESS:
                _TR_PROGRESS[key]["done"] = True
    except Exception:
        with _TR_LOCK:
            if key in _TR_PROGRESS:
                _TR_PROGRESS[key]["error"] = True


@router.post("/api/library/{material_id}/translate")
def translate(material_id: str, body: TranslateIn, user=Depends(current_user)):
    """Start or poll a streaming translation. Statuses: done (full content) | translating
    (partial chunks so far) | unavailable | error. The frontend polls until done."""
    lang = body.lang if body.lang in LANGS else None
    if not lang:
        raise HTTPException(400, "unsupported language")
    with db() as cur:
        m = _fetch_visible(cur, material_id, user)
        if lang == (m["language"] or "en"):     # already in this language → original is canonical
            return {"status": "done", "available": True, "lang": lang, "engine": "original",
                    "title": m["title"], "content": material_text(m), "cached": True}
        cur.execute("SELECT title,content,engine,truncated FROM article_translations "
                    "WHERE material_id=%s AND lang=%s", (material_id, lang))
        cached = cur.fetchone()
        if cached:
            return {"status": "done", "available": True, "lang": lang, "cached": True,
                    "engine": cached["engine"], "title": cached["title"],
                    "content": cached["content"], "truncated": cached["truncated"]}
    if not GROQ_ENABLED:
        return {"status": "unavailable", "available": False, "lang": lang}

    key = (material_id, lang)
    with _TR_LOCK:
        p = _TR_PROGRESS.get(key)
        if p:
            if p.get("error"):
                _TR_PROGRESS.pop(key, None)
                return {"status": "error", "lang": lang}
            if p["done"]:
                content = "\n\n".join(c for c in p["chunks"] if c).strip()
                title, truncated = p.get("title"), p["truncated"]
                _TR_PROGRESS.pop(key, None)        # cached now → free memory
                return {"status": "done", "available": True, "lang": lang, "title": title,
                        "content": content, "truncated": truncated}
            return {"status": "translating", "lang": lang, "title": p.get("title"),
                    "chunks": list(p["chunks"]), "total": p["total"], "truncated": p["truncated"]}

    text = material_text(m) or ""
    hm = re.search(r'\n={5,}\s*\n', text)   # drop our provenance header → translate the body, not metadata
    if hm:
        text = text[hm.end():].lstrip()
    truncated = len(text) > TRANSLATE_CAP
    blocks = _split(text[:TRANSLATE_CAP], TR_BLOCK)
    name = LANG_NAMES.get(lang, lang)
    with _TR_LOCK:
        if key in _TR_PROGRESS:                  # lost a race → report current progress
            p = _TR_PROGRESS[key]
            return {"status": "translating", "lang": lang, "title": p.get("title"),
                    "chunks": list(p["chunks"]), "total": p["total"], "truncated": p["truncated"]}
        _TR_PROGRESS[key] = {"chunks": [], "total": len(blocks), "title": None,
                             "truncated": truncated, "done": False, "error": False}
    with db() as cur:
        audit(cur, user["id"], "translate", "material", material_id, {"lang": lang}, None)
    threading.Thread(target=_do_translate,
                     args=(material_id, lang, blocks, m["title"], name, truncated), daemon=True).start()
    return {"status": "translating", "lang": lang, "title": None,
            "chunks": [], "total": len(blocks), "truncated": truncated}


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
# Localized lead-in for the chat-history record of a library search (the assistant
# "turn" we store so this surface shows up in the member's copilot history too).
_ASK_NOTE = {
    "en": "🔎 Searched the library", "tr": "🔎 Kütüphanede arama yapıldı",
    "es": "🔎 Se buscó en la biblioteca", "de": "🔎 Bibliothek durchsucht",
    "fr": "🔎 Recherche dans la bibliothèque", "it": "🔎 Ricerca nella biblioteca",
    "ru": "🔎 Поиск по библиотеке", "nl": "🔎 Bibliotheek doorzocht",
}


class AskIn(BaseModel):
    question: constr(min_length=1, max_length=500)
    lang: Optional[str] = None      # member UI language (for the history record)


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
        try:
            raw = groq_chat([{"role": "system", "content": sys}, {"role": "user", "content": q}],
                            model=GROQ_FAST_MODEL, max_tokens=200, temperature=0)
        except GroqRateLimited:
            raise HTTPException(429, "rate_limited")
        except GroqUnavailable:
            raw = None
        if raw:
            try:
                j = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
                cleaned = {k: str(v) for k, v in j.items()
                           if k in ("q", "author", "journal", "year_from", "year_to") and v}
                if cleaned:
                    params = cleaned; interpreted = True
            except (ValueError, AttributeError):
                pass
    # Record this search in the member's copilot chat history (its own single-turn
    # conversation, badged source='library'). Best-effort — never block the search.
    note = _ASK_NOTE.get(body.lang if body.lang in LANGS else "en", _ASK_NOTE["en"])
    summary = "; ".join(f"{k}: {v}" for k, v in params.items() if v)
    content = f"{note} — {summary}" if summary else note
    try:
        with db() as cur:
            record_exchange(cur, user["id"], None, "library", q, content, [])
    except Exception:
        pass
    return {"params": params, "interpreted": interpreted, "groq": GROQ_ENABLED}
