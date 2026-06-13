"""
Copilot — PLACEHOLDER for now.

`/api/copilot/ask` returns a clearly-labeled test answer to ANY question (no Groq,
no corpus retrieval, no embeddings yet). A member can save an answer (-> saved_answers)
so the *sharing* feature is wired end-to-end; when the real citation-grounded RAG
engine lands it just fills `sources` and replaces the placeholder text.
"""
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, constr
from psycopg2.extras import Json

from core import db, current_user

router = APIRouter()

PLACEHOLDER = (
    "🔬 **OpenElpis copilot — preview mode.**\n\n"
    "The citation-grounded engine isn't connected to the validated corpus yet, so this is a "
    "test response and it does **not** cite real sources. Once it's live, every answer here will "
    "be grounded in expert-approved material and each claim will carry a citation.\n\n"
    "> You asked: *{q}*\n\n"
    "_OpenElpis produces research hypotheses for qualified professionals — never diagnosis or "
    "treatment advice._"
)


class AskIn(BaseModel):
    question: constr(min_length=1, max_length=2000)


@router.post("/api/copilot/ask")
def ask(body: AskIn, user=Depends(current_user)):
    return {"answer": PLACEHOLDER.format(q=body.question.strip()), "sources": [], "placeholder": True}


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
