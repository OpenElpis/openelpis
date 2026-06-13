"""
"Shares" — attaching a material or a saved copilot answer to a forum topic/post
or a direct message, and rendering a preview of one for a given viewer.

A share is stored as two columns: share_kind ('material'|'answer') + share_ref
(jsonb, always {"id": "<uuid>"}). validate_share() guards what a user may attach;
hydrate_share() resolves a stored share into a display dict at read time (so titles
stay fresh and visibility is re-checked per viewer).
"""
from fastapi import HTTPException

VALID_KINDS = {"material", "answer"}


def validate_share(cur, user, kind, ref):
    """Validate a share the user is ATTACHING. Returns (kind, normalized_ref) or
    (None, None) when nothing is attached; raises HTTPException on a bad/forbidden share."""
    if not kind:
        return None, None
    if kind not in VALID_KINDS:
        raise HTTPException(400, "invalid share kind")
    sid = ref.get("id") if isinstance(ref, dict) else None
    if not sid:
        raise HTTPException(400, "share reference is missing an id")
    if kind == "material":
        cur.execute("SELECT uploaded_by,status FROM materials WHERE id=%s", (sid,))
        m = cur.fetchone()
        if not m:
            raise HTTPException(404, "material not found")
        if not (str(m["uploaded_by"]) == str(user["id"]) or m["status"] == "approved"
                or user["role"] == "admin"):
            raise HTTPException(403, "you can only share your own or approved materials")
    else:  # answer
        cur.execute("SELECT user_id FROM saved_answers WHERE id=%s", (sid,))
        a = cur.fetchone()
        if not a:
            raise HTTPException(404, "saved answer not found")
        if str(a["user_id"]) != str(user["id"]) and user["role"] != "admin":
            raise HTTPException(403, "you can only share your own saved answers")
    return kind, {"id": str(sid)}


def material_shared_with(cur, mid, viewer_id):
    """True if material `mid` was shared somewhere `viewer_id` can see it: any forum
    share (forum is visible to all members) or a DM to/from the viewer."""
    cur.execute("""SELECT 1 WHERE EXISTS (
        SELECT 1 FROM forum_posts  WHERE share_kind='material' AND share_ref->>'id'=%(m)s AND status='visible'
        UNION ALL SELECT 1 FROM forum_topics WHERE share_kind='material' AND share_ref->>'id'=%(m)s AND status='open'
        UNION ALL SELECT 1 FROM direct_messages WHERE share_kind='material' AND share_ref->>'id'=%(m)s
               AND (sender_id=%(v)s OR recipient_id=%(v)s))""",
        {"m": str(mid), "v": str(viewer_id)})
    return cur.fetchone() is not None


def hydrate_share(cur, kind, ref, viewer):
    """Resolve a stored share into a display dict for `viewer`, or None."""
    if not kind or not ref:
        return None
    sid = ref.get("id")
    if not sid:
        return None
    if kind == "material":
        cur.execute("SELECT id,title,original_filename,source_type,status,uploaded_by "
                    "FROM materials WHERE id=%s", (sid,))
        m = cur.fetchone()
        if not m:
            return {"kind": "material", "unavailable": True}
        can_open = (str(m["uploaded_by"]) == str(viewer["id"]) or m["status"] == "approved"
                    or viewer["role"] == "admin" or material_shared_with(cur, sid, viewer["id"]))
        return {"kind": "material", "id": str(m["id"]), "title": m["title"],
                "filename": m["original_filename"], "source_type": m["source_type"],
                "status": m["status"], "can_open": bool(can_open)}
    if kind == "answer":
        cur.execute("SELECT id,question,answer,sources FROM saved_answers WHERE id=%s", (sid,))
        a = cur.fetchone()
        if not a:
            return {"kind": "answer", "unavailable": True}
        return {"kind": "answer", "id": str(a["id"]), "question": a["question"],
                "answer": a["answer"], "sources": a["sources"] or []}
    return None
