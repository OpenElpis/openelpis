"""
LLM client for Groq — stdlib urllib only (no new dependency, matching mailer.py).

Used by the Library: on-demand article translation and natural-language ("ask the
copilot") search. It is INERT until GROQ_API_KEY is set in /etc/openelpis.env — every
caller must treat GROQ_ENABLED is False as "feature not available yet" and degrade
gracefully (the real RAG copilot will reuse this same client later).
"""
import os, json, urllib.request, urllib.error

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_ENABLED = bool(GROQ_API_KEY)
_URL = "https://api.groq.com/openai/v1/chat/completions"


def groq_chat(messages, *, model=None, max_tokens=2048, temperature=0.2, timeout=90):
    """Return the assistant message text, or None if Groq is disabled / the call fails.
    Never raises — callers fall back to a non-AI path."""
    if not GROQ_ENABLED:
        return None
    payload = json.dumps({
        "model": model or GROQ_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(_URL, data=payload, headers={
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError,
            TimeoutError, OSError):
        return None
