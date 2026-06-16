"""
LLM client for Groq — stdlib urllib only (no new dependency, matching mailer.py).

Used by the copilot (citation-grounded Q&A), the Library article translator, and the
Library natural-language search. Inert until GROQ_API_KEY is set in /etc/openelpis.env.

Errors are typed so callers can react: GroqRateLimited (free tier ~25 req/min → tell the
user to wait) vs GroqUnavailable (disabled / bad key / network / 5xx).
"""
import os, json, urllib.request, urllib.error

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")        # quality: copilot answers
# High-throughput model (much higher free-tier tokens/min) for bulk/simple jobs:
# article translation, keyword extraction, NL→filter parsing. Avoids 429s on long translations.
GROQ_FAST_MODEL = os.environ.get("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
GROQ_ENABLED = bool(GROQ_API_KEY)
_URL = "https://api.groq.com/openai/v1/chat/completions"
# A real User-Agent is REQUIRED: api.groq.com sits behind Cloudflare, which blocks the default
# "Python-urllib/x" agent with HTTP 403 "error code: 1010". (Learned the hard way.)
_UA = "OpenElpis/1.0 (+https://openelpis.com; hello@openelpis.com)"


class GroqError(Exception):
    pass


class GroqRateLimited(GroqError):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        super().__init__("rate_limited")


class GroqUnavailable(GroqError):
    pass


def groq_chat(messages, *, model=None, max_tokens=2048, temperature=0.2, timeout=90):
    """Return the assistant message text.
    Raises GroqRateLimited on HTTP 429, GroqUnavailable when disabled / on any other failure."""
    if not GROQ_ENABLED:
        raise GroqUnavailable("groq not configured")
    payload = json.dumps({
        "model": model or GROQ_MODEL, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode()
    req = urllib.request.Request(_URL, data=payload, headers={
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return (data["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            ra = e.headers.get("retry-after") if e.headers else None
            raise GroqRateLimited(int(ra) if (ra and str(ra).isdigit()) else None)
        raise GroqUnavailable(f"groq http {e.code}")
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError, OSError) as e:
        raise GroqUnavailable(str(e))
