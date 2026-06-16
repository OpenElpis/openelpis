"""
Copilot chat history — persistent, per-member conversations so a clinician can
revisit and continue past chats.

Two surfaces write here (via record_exchange):
  • the Copilot tab  — multi-turn Q&A,         source='copilot'
  • the Library "ask the copilot" search — a single-turn log, source='library'

This is distinct from `saved_answers`, which is the explicit "save & share" bookmark
unit. A conversation groups an ordered thread of messages; ordering is by the message
bigserial id (two messages inserted in one transaction share now(), so created_at can
tie — id never does).
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from psycopg2.extras import Json

from core import db, current_user

router = APIRouter()

TITLE_CAP = 80


def _title_from(q):
    q = " ".join((q or "").split())
    if not q:
        return "New chat"
    return (q[:TITLE_CAP] + "…") if len(q) > TITLE_CAP else q


def ensure_conversation(cur, user_id, conversation_id, source, first_question):
    """Return a conversation id owned by user_id. Reuses conversation_id when it's a
    valid one of theirs; otherwise creates a new conversation titled from the first
    question. (A library ask always passes None → its own one-off conversation.)"""
    if conversation_id:
        cur.execute("SELECT id FROM chat_conversations WHERE id=%s AND user_id=%s",
                    (conversation_id, user_id))
        if cur.fetchone():
            return str(conversation_id)
    cur.execute("INSERT INTO chat_conversations(user_id,title,source) VALUES (%s,%s,%s) RETURNING id",
                (user_id, _title_from(first_question), source))
    return str(cur.fetchone()["id"])


def _add_message(cur, conversation_id, role, content, sources=None):
    cur.execute("INSERT INTO chat_messages(conversation_id,role,content,sources) VALUES (%s,%s,%s,%s)",
                (conversation_id, role, content, Json(sources or [])))


def record_exchange(cur, user_id, conversation_id, source, question, answer, sources=None):
    """Persist one user→assistant exchange and bump the conversation. Returns the
    conversation id (string). Callers wrap this best-effort so a history write never
    breaks the actual answer."""
    cid = ensure_conversation(cur, user_id, conversation_id, source, question)
    _add_message(cur, cid, "user", question)
    _add_message(cur, cid, "assistant", answer, sources)
    cur.execute("UPDATE chat_conversations SET last_message_at=now() WHERE id=%s", (cid,))
    return cid


@router.get("/api/chats")
def list_chats(user=Depends(current_user)):
    """The caller's conversations, newest activity first (for the History panel)."""
    with db() as cur:
        cur.execute(
            "SELECT c.id,c.title,c.source,c.last_message_at,"
            "(SELECT count(*) FROM chat_messages m WHERE m.conversation_id=c.id) AS msgs "
            "FROM chat_conversations c WHERE c.user_id=%s "
            "ORDER BY c.last_message_at DESC LIMIT 200", (user["id"],))
        rows = cur.fetchall()
    return {"chats": [{"id": str(r["id"]), "title": r["title"], "source": r["source"],
                       "messages": r["msgs"], "last_message_at": r["last_message_at"].isoformat()}
                      for r in rows]}


@router.get("/api/chats/{conversation_id}")
def get_chat(conversation_id: str, user=Depends(current_user)):
    """A conversation + its ordered messages (to rehydrate the chat view)."""
    with db() as cur:
        cur.execute("SELECT id,title,source,created_at FROM chat_conversations "
                    "WHERE id=%s AND user_id=%s", (conversation_id, user["id"]))
        c = cur.fetchone()
        if not c:
            raise HTTPException(404, "not found")
        cur.execute("SELECT role,content,sources,created_at FROM chat_messages "
                    "WHERE conversation_id=%s ORDER BY id", (conversation_id,))
        msgs = cur.fetchall()
    return {"id": str(c["id"]), "title": c["title"], "source": c["source"],
            "created_at": c["created_at"].isoformat(),
            "messages": [{"role": m["role"], "content": m["content"],
                          "sources": m["sources"] or [],
                          "created_at": m["created_at"].isoformat()} for m in msgs]}


@router.post("/api/chats/{conversation_id}/delete")
def delete_chat(conversation_id: str, user=Depends(current_user)):
    """Remove one of the caller's own conversations (messages cascade)."""
    with db() as cur:
        cur.execute("DELETE FROM chat_conversations WHERE id=%s AND user_id=%s RETURNING id",
                    (conversation_id, user["id"]))
        if not cur.fetchone():
            raise HTTPException(404, "not found")
    return {"ok": True}
