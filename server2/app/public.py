"""
Public, unauthenticated, read-only library for open researchers.

Same corpus and the SAME search logic as the member Library (library.py), but every
member-only / metered path is stripped:
  - NO copilot, NO natural-language "ask"
  - NO on-demand machine translation (article titles & bodies stay in their original,
    usually English, language — the public page's own UI chrome is translated client-side)
  - NO votes / comments / favorites and NO per-user state of any kind
  - NO member-identifiable data (who voted, who commented) is ever returned here

Only status='approved' rows (the vetted, CC-licensed corpus) are exposed; pending member
uploads and shared-only items are not. Served at openelpis.com/api/public/* and proxied
by Caddy exactly like the rest of /api/* — no auth dependency on any route below.
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException

from core import db
from materials import material_text
from library import _FTS, _PUBDATE, _ESCALATE, _BODY_HIT_CAP, _KIND_KNOWN, _kind_filter, kind_counts

router = APIRouter()


# ── corpus-wide counts per content kind (same cached values as the member Library) ──
# NOTE: declared BEFORE /api/public/library/{material_id} so "kinds" isn't captured as an id.
@router.get("/api/public/library/kinds")
def public_kinds():
    return kind_counts()


# ── browse / search (mirrors library.py's escalation strategy, minus auth/stats) ────
@router.get("/api/public/library")
def public_library(q: Optional[str] = None, author: Optional[str] = None,
                   journal: Optional[str] = None, license: Optional[str] = None,
                   source_type: Optional[str] = None, year_from: Optional[str] = None,
                   year_to: Optional[str] = None, kind: Optional[str] = None, sort: str = "new",
                   page: int = 1, limit: int = 24):
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
    if kind in _KIND_KNOWN:
        _kind_filter(kind, where, params)
    kind_only = (kind in _KIND_KNOWN and not (q or author or journal or license
                 or source_type or year_from or year_to))
    cached_total = kind_counts()["counts"].get(kind, 0) if kind_only else None

    relevance = (sort == "relevance" and bool(q))
    if relevance:
        order = f"ORDER BY ts_rank({_FTS}, websearch_to_tsquery('english', %(q)s)) DESC, m.id DESC"
    else:
        d = "DESC" if sort != "old" else "ASC"
        order = f"ORDER BY {_PUBDATE} {d}, m.id {d}"
    where_sql = " AND ".join(where)
    offset = min((page - 1) * limit, 200000)          # guard pathological deep scans
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
    pages = max(1, -(-total // limit))                # ceil division
    items = [{"id": str(r["id"]), "title": r["title"],
              "authors": r["authors"], "journal": r["journal"],
              "year": r["year"], "license": r["license"], "doi": r["doi"], "url": r["url"],
              "source_type": r["source_type"], "size_bytes": r["size_bytes"], "kind": r["kind"],
              "created_at": r["created_at"].isoformat()} for r in rows]
    return {"items": items, "total": total, "page": page, "pages": pages, "limit": limit}


# ── full article reading view (full text — content is CC-licensed; attribution shown) ─
@router.get("/api/public/library/{material_id}")
def public_article(material_id: str):
    with db() as cur:
        cur.execute("SELECT id,status,storage_key,title,description,language,metadata "
                    "FROM materials WHERE id=%s", (material_id,))
        m = cur.fetchone()
    if not m or m["status"] != "approved":            # only the vetted corpus is public
        raise HTTPException(404, "not found")
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
    }
