"""
Community layer: a member directory, friend requests (the `connections` graph),
and 1:1 direct messages. DMs are limited to accepted connections. Real-time is
done by client polling (no WebSockets) — light enough for the 1 GB box.
"""
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, constr
from psycopg2.extras import Json

from core import db, audit, client_ip, current_user
from shares import validate_share, hydrate_share

router = APIRouter()


# ── friend graph helpers ────────────────────────────────────────────────────────
def _conn_map(cur, uid):
    """{other_id: {'status':..., 'dir':'out'|'in'}} for all of the user's connections."""
    cur.execute("SELECT requester_id,addressee_id,status FROM connections "
                "WHERE requester_id=%s OR addressee_id=%s", (uid, uid))
    out = {}
    for r in cur.fetchall():
        if str(r["requester_id"]) == str(uid):
            out[str(r["addressee_id"])] = {"status": r["status"], "dir": "out"}
        else:
            out[str(r["requester_id"])] = {"status": r["status"], "dir": "in"}
    return out


def _rel(info):
    if not info: return "none"
    if info["status"] == "accepted": return "friends"
    if info["status"] == "pending":  return "pending_out" if info["dir"] == "out" else "pending_in"
    return "none"


def _are_friends(cur, a, b):
    cur.execute("SELECT 1 FROM connections WHERE status='accepted' AND "
                "((requester_id=%s AND addressee_id=%s) OR (requester_id=%s AND addressee_id=%s))",
                (a, b, b, a))
    return cur.fetchone() is not None


# ── member directory ─────────────────────────────────────────────────────────────
@router.get("/api/members")
def members(q: Optional[str] = None, user=Depends(current_user)):
    sql = ("SELECT u.id,u.full_name,u.specialty,u.role,u.verification_status,o.name AS org "
           "FROM users u LEFT JOIN organizations o ON o.id=u.org_id "
           "WHERE u.is_active AND u.id<>%s")
    params = [str(user["id"])]
    if q:
        sql += " AND (u.full_name ILIKE %s OR u.specialty ILIKE %s OR o.name ILIKE %s)"
        like = f"%{q}%"; params += [like, like, like]
    sql += " ORDER BY u.full_name LIMIT 60"
    with db() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cmap = _conn_map(cur, user["id"])
    return {"members": [
        {"id": str(r["id"]), "full_name": r["full_name"], "specialty": r["specialty"], "org": r["org"],
         "verified": r["verification_status"] == "verified",
         "relation": _rel(cmap.get(str(r["id"])))} for r in rows]}


# ── friend requests ──────────────────────────────────────────────────────────────
class UserRef(BaseModel):
    user_id: str


@router.post("/api/friends/request")
def friend_request(body: UserRef, request: Request, user=Depends(current_user)):
    if body.user_id == str(user["id"]):
        raise HTTPException(400, "cannot connect with yourself")
    with db() as cur:
        cur.execute("SELECT 1 FROM users WHERE id=%s AND is_active", (body.user_id,))
        if not cur.fetchone():
            raise HTTPException(404, "member not found")
        cur.execute("SELECT id,requester_id,addressee_id,status FROM connections "
                    "WHERE (requester_id=%s AND addressee_id=%s) OR (requester_id=%s AND addressee_id=%s)",
                    (user["id"], body.user_id, body.user_id, user["id"]))
        ex = cur.fetchone()
        if ex:
            if ex["status"] == "accepted":
                return {"ok": True, "relation": "friends"}
            if ex["status"] == "pending":
                if str(ex["addressee_id"]) == str(user["id"]):  # they already asked me -> accept
                    cur.execute("UPDATE connections SET status='accepted', responded_at=now() WHERE id=%s", (ex["id"],))
                    return {"ok": True, "relation": "friends"}
                return {"ok": True, "relation": "pending_out"}
            # declined/blocked -> re-open as a fresh request from me
            cur.execute("UPDATE connections SET requester_id=%s,addressee_id=%s,status='pending',"
                        "created_at=now(),responded_at=NULL WHERE id=%s", (user["id"], body.user_id, ex["id"]))
            return {"ok": True, "relation": "pending_out"}
        cur.execute("INSERT INTO connections(requester_id,addressee_id) VALUES (%s,%s)", (user["id"], body.user_id))
        audit(cur, user["id"], "friend_request", "user", body.user_id, None, client_ip(request))
    return {"ok": True, "relation": "pending_out"}


@router.post("/api/friends/accept")
def friend_accept(body: UserRef, user=Depends(current_user)):
    with db() as cur:
        cur.execute("UPDATE connections SET status='accepted', responded_at=now() "
                    "WHERE addressee_id=%s AND requester_id=%s AND status='pending'", (user["id"], body.user_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "no pending request from this member")
    return {"ok": True, "relation": "friends"}


@router.post("/api/friends/decline")
def friend_decline(body: UserRef, user=Depends(current_user)):
    with db() as cur:
        cur.execute("UPDATE connections SET status='declined', responded_at=now() "
                    "WHERE addressee_id=%s AND requester_id=%s AND status='pending'", (user["id"], body.user_id))
    return {"ok": True}


@router.post("/api/friends/remove")
def friend_remove(body: UserRef, user=Depends(current_user)):
    with db() as cur:
        cur.execute("DELETE FROM connections WHERE status='accepted' AND "
                    "((requester_id=%s AND addressee_id=%s) OR (requester_id=%s AND addressee_id=%s))",
                    (user["id"], body.user_id, body.user_id, user["id"]))
    return {"ok": True}


@router.get("/api/friends")
def list_friends(user=Depends(current_user)):
    u = str(user["id"])
    with db() as cur:
        cur.execute("SELECT status,requester_id,addressee_id, "
                    "CASE WHEN requester_id=%(u)s THEN addressee_id ELSE requester_id END AS other_id "
                    "FROM connections WHERE (requester_id=%(u)s OR addressee_id=%(u)s) "
                    "AND status IN ('accepted','pending')", {"u": u})
        rows = cur.fetchall()
        ids = [str(r["other_id"]) for r in rows]
        people = {}
        if ids:
            cur.execute("SELECT u.id,u.full_name,u.specialty,o.name AS org FROM users u "
                        "LEFT JOIN organizations o ON o.id=u.org_id WHERE u.id = ANY(%s)", (ids,))
            for x in cur.fetchall():
                people[str(x["id"])] = {"id": str(x["id"]), "full_name": x["full_name"],
                                        "specialty": x["specialty"], "org": x["org"]}
    friends, incoming, outgoing = [], [], []
    for r in rows:
        person = people.get(str(r["other_id"]), {"id": str(r["other_id"]), "full_name": "—"})
        if r["status"] == "accepted":              friends.append(person)
        elif str(r["requester_id"]) == u:          outgoing.append(person)
        else:                                       incoming.append(person)
    return {"friends": friends, "incoming": incoming, "outgoing": outgoing}


# ── direct messages ──────────────────────────────────────────────────────────────
class DMIn(BaseModel):
    body:       Optional[constr(max_length=8000)] = None
    share_kind: Optional[str] = None
    share_ref:  Optional[dict] = None


@router.get("/api/messages")
def conversations(user=Depends(current_user)):
    """Conversation list: last message + unread count per partner."""
    u = str(user["id"])
    with db() as cur:
        cur.execute("""
          SELECT DISTINCT ON (other_id) other_id, body, share_kind, created_at AS last_at FROM (
            SELECT *, CASE WHEN sender_id=%(u)s THEN recipient_id ELSE sender_id END AS other_id
            FROM direct_messages WHERE sender_id=%(u)s OR recipient_id=%(u)s) x
          ORDER BY other_id, created_at DESC""", {"u": u})
        last = cur.fetchall()
        cur.execute("SELECT sender_id AS other_id, count(*) AS n FROM direct_messages "
                    "WHERE recipient_id=%(u)s AND read_at IS NULL GROUP BY sender_id", {"u": u})
        unread = {str(r["other_id"]): r["n"] for r in cur.fetchall()}
        ids = [str(r["other_id"]) for r in last]
        names = {}
        if ids:
            cur.execute("SELECT id,full_name,specialty FROM users WHERE id = ANY(%s)", (ids,))
            for x in cur.fetchall():
                names[str(x["id"])] = {"full_name": x["full_name"], "specialty": x["specialty"]}
    convos = []
    for r in last:
        oid = str(r["other_id"]); nm = names.get(oid, {"full_name": "—", "specialty": None})
        body = r["body"]
        preview = (body[:60] + "…") if body and len(body) > 60 else (body or ("📎 shared a " + (r["share_kind"] or "item")))
        convos.append({"user_id": oid, "full_name": nm["full_name"], "specialty": nm["specialty"],
                       "last": preview, "last_at": r["last_at"].isoformat(), "unread": unread.get(oid, 0)})
    convos.sort(key=lambda c: c["last_at"], reverse=True)
    return {"conversations": convos}


@router.get("/api/messages/{other_id}")
def thread(other_id: str, user=Depends(current_user)):
    with db() as cur:
        cur.execute("SELECT id,full_name,specialty FROM users WHERE id=%s AND is_active", (other_id,))
        other = cur.fetchone()
        if not other:
            raise HTTPException(404, "member not found")
        cur.execute("""SELECT id,sender_id,body,share_kind,share_ref,created_at
                       FROM direct_messages
                       WHERE (sender_id=%(u)s AND recipient_id=%(o)s)
                          OR (sender_id=%(o)s AND recipient_id=%(u)s)
                       ORDER BY created_at LIMIT 200""", {"u": str(user["id"]), "o": other_id})
        msgs = [{"id": str(m["id"]), "mine": str(m["sender_id"]) == str(user["id"]), "body": m["body"],
                 "created_at": m["created_at"].isoformat(),
                 "share": hydrate_share(cur, m["share_kind"], m["share_ref"], user)} for m in cur.fetchall()]
        cur.execute("UPDATE direct_messages SET read_at=now() "
                    "WHERE recipient_id=%s AND sender_id=%s AND read_at IS NULL", (user["id"], other_id))
        are_friends = _are_friends(cur, user["id"], other_id)
    return {"other": {"id": other_id, "full_name": other["full_name"], "specialty": other["specialty"]},
            "are_friends": are_friends, "messages": msgs}


@router.post("/api/messages/{other_id}")
def send_message(other_id: str, body: DMIn, request: Request, user=Depends(current_user)):
    if other_id == str(user["id"]):
        raise HTTPException(400, "cannot message yourself")
    if not body.body and not body.share_kind:
        raise HTTPException(400, "empty message")
    with db() as cur:
        cur.execute("SELECT 1 FROM users WHERE id=%s AND is_active", (other_id,))
        if not cur.fetchone():
            raise HTTPException(404, "member not found")
        if not _are_friends(cur, user["id"], other_id):
            raise HTTPException(403, "you can only message your connections — send a request first")
        kind, ref = validate_share(cur, user, body.share_kind, body.share_ref)
        cur.execute("INSERT INTO direct_messages(sender_id,recipient_id,body,share_kind,share_ref) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING id,created_at",
                    (user["id"], other_id, body.body, kind, Json(ref) if ref else None))
        m = cur.fetchone()
        audit(cur, user["id"], "dm_send", "user", other_id, None, client_ip(request))
    return {"ok": True, "id": str(m["id"]), "created_at": m["created_at"].isoformat()}
