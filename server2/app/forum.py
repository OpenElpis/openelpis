"""
Forum: question topics + threaded replies. A topic or reply can carry a "share"
(a material or a saved copilot answer) so members can discuss it together.
"""
from typing import Optional, List

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, constr
from psycopg2.extras import Json

from core import db, audit, client_ip, current_user
from shares import validate_share, hydrate_share

router = APIRouter()

CATEGORIES = {"general", "cases", "research", "platform"}


class TopicIn(BaseModel):
    category:   str = "general"
    title:      constr(min_length=3, max_length=200)
    body:       constr(min_length=1, max_length=20000)
    tags:       Optional[List[constr(max_length=40)]] = None
    share_kind: Optional[str] = None
    share_ref:  Optional[dict] = None


class PostIn(BaseModel):
    body:       constr(min_length=1, max_length=20000)
    share_kind: Optional[str] = None
    share_ref:  Optional[dict] = None


@router.get("/api/forum/topics")
def list_topics(category: Optional[str] = None, user=Depends(current_user)):
    sql = ("SELECT t.id,t.category,t.title,t.body,t.tags,t.reply_count,t.last_activity_at,t.is_pinned,"
           "t.created_at,u.full_name AS author FROM forum_topics t JOIN users u ON u.id=t.author_id "
           "WHERE t.status='open'")
    params = []
    if category in CATEGORIES:
        sql += " AND t.category=%s"; params.append(category)
    sql += " ORDER BY t.is_pinned DESC, t.last_activity_at DESC LIMIT 80"
    with db() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    def snip(b): return (b[:200] + "…") if b and len(b) > 200 else (b or "")
    return {"topics": [
        {"id": str(r["id"]), "category": r["category"], "title": r["title"], "snippet": snip(r["body"]),
         "tags": r["tags"] or [], "reply_count": r["reply_count"], "author": r["author"],
         "is_pinned": r["is_pinned"], "last_activity_at": r["last_activity_at"].isoformat(),
         "created_at": r["created_at"].isoformat()} for r in rows]}


@router.post("/api/forum/topics")
def create_topic(body: TopicIn, request: Request, user=Depends(current_user)):
    cat  = body.category if body.category in CATEGORIES else "general"
    tags = [t.strip() for t in (body.tags or []) if t.strip()][:6]
    with db() as cur:
        kind, ref = validate_share(cur, user, body.share_kind, body.share_ref)
        cur.execute("INSERT INTO forum_topics(author_id,category,title,body,tags,share_kind,share_ref) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (user["id"], cat, body.title, body.body, tags, kind, Json(ref) if ref else None))
        tid = cur.fetchone()["id"]
        audit(cur, user["id"], "forum_topic", "forum_topic", tid, {"title": body.title}, client_ip(request))
    return {"ok": True, "id": str(tid)}


@router.get("/api/forum/topics/{topic_id}")
def get_topic(topic_id: str, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT t.id,t.category,t.title,t.body,t.tags,t.is_locked,t.created_at,t.share_kind,"
                    "t.share_ref,u.full_name AS author_name,u.role AS author_role,"
                    "u.verification_status AS author_v FROM forum_topics t JOIN users u ON u.id=t.author_id "
                    "WHERE t.id=%s AND t.status='open'", (topic_id,))
        t = cur.fetchone()
        if not t:
            raise HTTPException(404, "topic not found")
        topic = {"id": str(t["id"]), "category": t["category"], "title": t["title"], "body": t["body"],
                 "tags": t["tags"] or [], "is_locked": t["is_locked"], "created_at": t["created_at"].isoformat(),
                 "author": {"name": t["author_name"], "role": t["author_role"],
                            "verified": t["author_v"] == "verified"},
                 "share": hydrate_share(cur, t["share_kind"], t["share_ref"], user)}
        cur.execute("SELECT p.id,p.body,p.share_kind,p.share_ref,p.created_at,p.author_id,"
                    "u.full_name AS author_name,u.role AS author_role,u.verification_status AS author_v "
                    "FROM forum_posts p JOIN users u ON u.id=p.author_id "
                    "WHERE p.topic_id=%s AND p.status='visible' ORDER BY p.created_at", (topic_id,))
        posts = [{"id": str(p["id"]), "body": p["body"],
                  "author": {"name": p["author_name"], "role": p["author_role"],
                             "verified": p["author_v"] == "verified"},
                  "is_mine": str(p["author_id"]) == str(user["id"]),
                  "created_at": p["created_at"].isoformat(),
                  "share": hydrate_share(cur, p["share_kind"], p["share_ref"], user)} for p in cur.fetchall()]
    return {"topic": topic, "posts": posts}


@router.post("/api/forum/topics/{topic_id}/posts")
def reply(topic_id: str, body: PostIn, request: Request, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT is_locked FROM forum_topics WHERE id=%s AND status='open'", (topic_id,))
        t = cur.fetchone()
        if not t:
            raise HTTPException(404, "topic not found")
        if t["is_locked"]:
            raise HTTPException(403, "topic is locked")
        kind, ref = validate_share(cur, user, body.share_kind, body.share_ref)
        cur.execute("INSERT INTO forum_posts(topic_id,author_id,body,share_kind,share_ref) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (topic_id, user["id"], body.body, kind, Json(ref) if ref else None))
        pid = cur.fetchone()["id"]
        cur.execute("UPDATE forum_topics SET reply_count=reply_count+1, last_activity_at=now() WHERE id=%s",
                    (topic_id,))
        audit(cur, user["id"], "forum_reply", "forum_post", pid, None, client_ip(request))
    return {"ok": True, "id": str(pid)}


@router.post("/api/forum/posts/{post_id}/remove")
def remove_post(post_id: str, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT author_id,topic_id FROM forum_posts WHERE id=%s AND status='visible'", (post_id,))
        p = cur.fetchone()
        if not p:
            raise HTTPException(404, "not found")
        if str(p["author_id"]) != str(user["id"]) and user["role"] != "admin":
            raise HTTPException(403, "not allowed")
        cur.execute("UPDATE forum_posts SET status='removed' WHERE id=%s", (post_id,))
        cur.execute("UPDATE forum_topics SET reply_count=GREATEST(reply_count-1,0) WHERE id=%s", (p["topic_id"],))
    return {"ok": True}


@router.post("/api/forum/topics/{topic_id}/remove")
def remove_topic(topic_id: str, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT author_id FROM forum_topics WHERE id=%s AND status='open'", (topic_id,))
        t = cur.fetchone()
        if not t:
            raise HTTPException(404, "not found")
        if str(t["author_id"]) != str(user["id"]) and user["role"] != "admin":
            raise HTTPException(403, "not allowed")
        cur.execute("UPDATE forum_topics SET status='removed' WHERE id=%s", (topic_id,))
    return {"ok": True}
