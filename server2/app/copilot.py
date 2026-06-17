"""
Copilot — citation-grounded Q&A over the approved corpus.

Retrieval is HYBRID over `material_chunks` (full article body, not just the abstract):
  • keyword  — Postgres FTS on each chunk's `tsv`
  • semantic — pgvector cosine over each chunk's 384-dim multilingual `embedding` (HNSW)
fused with reciprocal-rank fusion, deduped to the best passage per article. The semantic
arm is multilingual, so a Turkish question matches English passages by MEANING (synonyms,
paraphrases) — no shared words needed. Generation: Groq answers grounded ONLY in the
retrieved passages, cites [n], is COVERAGE-AWARE (says what the sources do/don't cover
instead of guessing), and replies in the SAME language as the question.

Degrades cleanly at every layer: no embedder installed / embeddings not built yet →
keyword-only (still full-body); GroqRateLimited → 429; GroqUnavailable → 503.
"""
import re
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, constr
from psycopg2.extras import Json

from core import db, current_user
from chat import record_exchange
from llm import groq_chat, GROQ_ENABLED, GROQ_FAST_MODEL, GroqRateLimited, GroqUnavailable
from embed import embed_query, vec_literal, EMBED_OK

router = APIRouter()

LANG_NAMES = {"en": "English", "tr": "Turkish", "es": "Spanish", "de": "German",
              "fr": "French", "it": "Italian", "ru": "Russian", "nl": "Dutch"}
TOPK = 6          # articles fed to the model
CAND = 30         # candidate chunks per retrieval arm before fusion
RRF_K = 60        # reciprocal-rank-fusion constant


def _keyword_chunks(cur, query, n=CAND):
    cur.execute(
        "SELECT c.material_id, c.content, ts_rank(c.tsv, q) AS s "
        "FROM material_chunks c JOIN materials m ON m.id = c.material_id, "
        "websearch_to_tsquery('english', %s) q "
        "WHERE m.status='approved' AND c.tsv @@ q "
        "ORDER BY s DESC LIMIT %s", (query, n))
    return cur.fetchall()


def _identifiers(text):
    """Distinctive scientific identifiers in the text — gene/protein/mutation/drug symbols
    like RhoC, K188, BRCA1, PD-L1, TP53. These are rare + specific, so ANDing just them is
    both precise and high-recall (it ignores the surrounding prose that would otherwise make
    a full-prompt AND miss a chunk that mentions the symbol but not every other word)."""
    out, seen = [], set()
    for t in re.findall(r"[A-Za-z][A-Za-z0-9\-]*[0-9][A-Za-z0-9\-]*|[A-Za-z]*[a-z][A-Z][A-Za-z0-9\-]*|[A-Z]{2,}", text or ""):
        k = t.lower()
        if len(t) >= 2 and k not in seen:
            seen.add(k); out.append(t)
    return out[:6]


def _keyword_ident(cur, text, n=CAND):
    """Body-chunk keyword arm over ONLY the distinctive identifiers (ANDed)."""
    ids = _identifiers(text)
    return _keyword_chunks(cur, " ".join(ids), n) if ids else []


def _vector_chunks(cur, qvec, n=CAND):
    lit = vec_literal(qvec)
    cur.execute(
        "SELECT c.material_id, c.content "
        "FROM material_chunks c JOIN materials m ON m.id = c.material_id "
        "WHERE m.status='approved' AND c.embedding IS NOT NULL "
        "ORDER BY c.embedding <=> %s::vector LIMIT %s", (lit, n))
    return cur.fetchall()


# Title + authors + journal + abstract — same searchable vector as the Library's partial GIN
# index (materials_lib_fts), so a query can match an article by its TITLE/abstract too, not
# only its body chunks.
_META_FTS = ("to_tsvector('english', coalesce(m.title,'')||' '||coalesce(m.metadata->>'author_string','')"
             "||' '||coalesce(m.metadata->'journal'->>'title','')||' '||coalesce(m.description,''))")


def _meta_chunks(cur, query, n=CAND):
    """Title/abstract keyword hits → (material_id, passage). Lets the copilot find articles
    by their title even when the term never appears in the indexed body."""
    if not query or not query.strip():
        return []
    cur.execute(
        "SELECT m.id AS material_id, coalesce(m.description, m.title) AS content "
        f"FROM materials m, websearch_to_tsquery('english', %s) q "
        f"WHERE m.status='approved' AND {_META_FTS} @@ q "
        f"ORDER BY ts_rank({_META_FTS}, q) DESC LIMIT %s", (query, n))
    return cur.fetchall()


def _meta(cur, ids):
    if not ids:
        return {}
    cur.execute(
        "SELECT id,title,description,language,metadata->>'author_string' AS authors,"
        "metadata->'journal'->>'title' AS journal,metadata->>'pub_year' AS year,"
        "metadata->>'doi' AS doi,metadata->>'url' AS url,"
        "coalesce(metadata->>'kind', case when metadata->>'source'='europepmc' then 'pmc' else 'article' end) AS kind "
        "FROM materials WHERE id = ANY(%s::uuid[])", (ids,))
    return {str(r["id"]): r for r in cur.fetchall()}


def _retrieve(cur, query, terms=None, qvec=None, k=TOPK):
    """Hybrid retrieve → top-k articles, each with its best-matching passage. Four arms,
    reciprocal-rank fused:
      • keyword over body chunks, using the user's full prompt (precise),
      • keyword over body chunks, using the extracted KEY TERMS (recall — finds a specific
        identifier like 'RhoC K188' buried in the body even inside a long prompt),
      • keyword over TITLE+abstract (so a title match surfaces too),
      • semantic over the front-of-article embeddings (synonyms / similar meanings).
    Chunk passages win over abstract passages, so the model sees the exact matching text."""
    query = (query or "").strip()
    if not query:
        return []
    terms = (terms or "").strip()
    kw_arms = [_keyword_chunks(cur, query)]
    if terms and terms.lower() != query.lower():
        kw_arms.append(_keyword_chunks(cur, terms))
    ident_arm = _keyword_ident(cur, query) or _keyword_ident(cur, terms)
    if ident_arm:
        kw_arms.append(ident_arm)
    meta_arm = _meta_chunks(cur, terms or query)
    vec_arm = _vector_chunks(cur, qvec) if qvec is not None else []
    fused = {}    # material_id -> {"score": float, "passage": str, "chunk": bool}
    def add(arm, is_chunk):
        for rank, r in enumerate(arm, 1):
            mid = str(r["material_id"])
            e = fused.setdefault(mid, {"score": 0.0, "passage": r["content"], "chunk": is_chunk})
            e["score"] += 1.0 / (RRF_K + rank)
            if is_chunk and not e["chunk"]:        # prefer an exact body passage over the abstract
                e["passage"], e["chunk"] = r["content"], True
    for arm in kw_arms:
        add(arm, True)
    add(vec_arm, True)
    add(meta_arm, False)
    order = sorted(fused.items(), key=lambda kv: kv[1]["score"], reverse=True)[:k]
    meta = _meta(cur, [mid for mid, _ in order])
    out = []
    for mid, e in order:
        m = meta.get(mid)
        if not m:
            continue
        d = dict(m)
        d["passage"] = e["passage"]
        out.append(d)
    return out


_KIND_TAG = {"preprint": " [PREPRINT — NOT peer-reviewed]", "clinical_trial": " [CLINICAL TRIAL record]",
             "book": " [BOOK]", "dataset": " [DATASET]", "article": " [non-PMC article]"}


def _sources_block(rows):
    out = []
    for i, r in enumerate(rows, 1):
        psg = (r.get("passage") or r.get("description") or "").strip().replace("\n", " ")
        if len(psg) > 1100:
            psg = psg[:1100] + "…"
        meta = " ".join(x for x in [r["authors"], r["journal"], r["year"]] if x)
        tag = _KIND_TAG.get(r.get("kind"), "")
        lg = r.get("language")
        if lg and lg != "en":
            tag += f" [in {LANG_NAMES.get(lg, lg)}]"      # tell the model this source isn't English
        out.append(f"[{i}]{tag} {r['title']}. {meta}\nExcerpt: {psg or '(no excerpt available)'}")
    return "\n\n".join(out)


class AskIn(BaseModel):
    question: constr(min_length=1, max_length=2000)
    lang: Optional[str] = None      # member UI language (fallback for ambiguous questions)
    conversation_id: Optional[str] = None   # continue an existing chat; None starts a new one


@router.post("/api/copilot/ask")
def ask(body: AskIn, user=Depends(current_user)):
    q = body.question.strip()
    ui_lang = LANG_NAMES.get((body.lang or "en"), "English")
    if not GROQ_ENABLED:
        return {"answer": "The research copilot isn't switched on yet.", "sources": [], "placeholder": True}

    qvec = None
    if EMBED_OK:
        try:
            qvec = embed_query(q)
        except Exception:
            qvec = None
    # Always distill the question to its KEY search terms (gene/mutation/drug symbols kept
    # verbatim). This is what lets a specific identifier buried in a long prompt — e.g.
    # "RhoC K188" — drive a precise keyword hit, and it doubles as the cross-lingual query.
    terms = None
    try:
        terms = groq_chat([
            {"role": "system", "content": "Extract a concise English search query of the KEY terms from the "
             "user's question to find relevant breast-cancer literature: gene/protein/mutation symbols "
             "(e.g. RhoC, K188, BRCA1, TP53), drugs, and the main concepts. Keep all symbols and identifiers "
             "VERBATIM. Output ONLY the query (terms separated by spaces), max 12 words, no extra punctuation."},
            {"role": "user", "content": q}], model=GROQ_FAST_MODEL, max_tokens=40, temperature=0)
    except (GroqRateLimited, GroqUnavailable):
        terms = None
    try:
        with db() as cur:
            rows = _retrieve(cur, q, terms, qvec)
        rows = rows[:TOPK]
        srcs = _sources_block(rows)
        sysmsg = (
            "You are OpenElpis, a citation-grounded research copilot for breast-cancer clinicians. "
            "Answer the user's question using ONLY the numbered sources provided (each is an excerpt from an "
            "approved article). Cite every claim inline as [1], [2] matching the source numbers. Be concise, "
            "clinical and accurate. "
            "BE COVERAGE-AWARE AND HONEST: if the sources only PARTIALLY answer the question — e.g. they give "
            "figures for some countries/subtypes/years but not a global total or not every case — say plainly "
            "what they DO and DO NOT cover (name what's available, flag what's missing) instead of guessing, "
            "extrapolating, or presenting a partial figure as if it were complete. If the sources don't address "
            "the question at all, say so. Never invent facts, numbers, or citations. "
            "SOURCE TYPES: some sources are tagged (e.g. [PREPRINT — NOT peer-reviewed], [CLINICAL TRIAL record], "
            "[non-PMC article], [BOOK]). When you rely on such a source, make its nature clear to the reader "
            "(e.g. note that a finding comes from a preprint that hasn't been peer-reviewed, or from a trial record). "
            f"LANGUAGE (critical): Write the ENTIRE answer in {ui_lang}. Do this even when the question is short or "
            f"contains English medical terms (e.g. 'cancer', 'HER2', 'BRCA') — a few English words do NOT make the "
            f"question English. Only answer in a different language if the user's question is clearly and fully "
            f"written in that other language. The source excerpts are mostly English; translate any facts you cite "
            f"into {ui_lang} — do not copy English sentences from the sources. "
            "These are research hypotheses for qualified professionals — never diagnosis or treatment advice.")
        usr = f"Question: {q}\n\n" + (f"Sources:\n{srcs}" if srcs else "Sources: (none found in the library)")
        answer = groq_chat([{"role": "system", "content": sysmsg}, {"role": "user", "content": usr}],
                           max_tokens=1100, temperature=0.2)
    except GroqRateLimited:
        raise HTTPException(429, "rate_limited")
    except GroqUnavailable:
        raise HTTPException(503, "ai_unavailable")
    # Localize the cited source titles to the question's language (English → original).
    lang_code = (body.lang or "en")
    tmap = {}
    if lang_code in LANG_NAMES and rows:
        try:
            with db() as cur:
                cur.execute("SELECT material_id, title FROM material_title_translations "
                            "WHERE lang=%s AND material_id = ANY(%s::uuid[])",
                            (lang_code, [str(r["id"]) for r in rows]))
                tmap = {str(x["material_id"]): x["title"] for x in cur.fetchall()}
        except Exception:
            tmap = {}
    sources = [{"n": i, "id": str(r["id"]), "title": tmap.get(str(r["id"])) or r["title"],
                "authors": r["authors"], "journal": r["journal"], "year": r["year"],
                "doi": r["doi"], "url": r["url"], "kind": r.get("kind"), "language": r.get("language")}
               for i, r in enumerate(rows, 1)]
    # Persist to chat history (best-effort — a history hiccup must not lose the answer).
    cid = body.conversation_id
    if answer:
        try:
            with db() as cur:
                cid = record_exchange(cur, user["id"], body.conversation_id, "copilot", q, answer, sources)
        except Exception:
            cid = body.conversation_id
    return {"answer": answer or "", "sources": sources, "placeholder": False,
            "grounded": bool(sources), "conversation_id": cid}


class SaveAnswerIn(BaseModel):
    question: constr(min_length=1, max_length=2000)
    answer:   constr(min_length=1, max_length=20000)
    sources:  Optional[List[dict]] = None


@router.post("/api/answers")
def save_answer(body: SaveAnswerIn, user=Depends(current_user)):
    with db() as cur:
        cur.execute("INSERT INTO saved_answers(user_id,question,answer,sources) "
                    "VALUES (%s,%s,%s,%s) RETURNING id",
                    (user["id"], body.question, body.answer, Json(body.sources or [])))
        aid = cur.fetchone()["id"]
    return {"ok": True, "id": str(aid)}


@router.get("/api/answers")
def my_answers(user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT id,question,created_at FROM saved_answers WHERE user_id=%s "
                    "ORDER BY created_at DESC LIMIT 100", (user["id"],))
        rows = cur.fetchall()
    return {"answers": [{"id": str(r["id"]), "question": r["question"],
                         "created_at": r["created_at"].isoformat()} for r in rows]}


@router.get("/api/answers/{answer_id}")
def get_answer(answer_id: str, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT id,question,answer,sources,created_at FROM saved_answers WHERE id=%s", (answer_id,))
        a = cur.fetchone()
    if not a:
        raise HTTPException(404, "not found")
    return {"id": str(a["id"]), "question": a["question"], "answer": a["answer"],
            "sources": a["sources"] or [], "created_at": a["created_at"].isoformat()}


@router.post("/api/answers/{answer_id}/delete")
def delete_answer(answer_id: str, user=Depends(current_user)):
    """Remove one of the caller's own saved answers."""
    with db() as cur:
        cur.execute("DELETE FROM saved_answers WHERE id=%s AND user_id=%s RETURNING id",
                    (answer_id, user["id"]))
        if not cur.fetchone():
            raise HTTPException(404, "not found")
    return {"ok": True}
