"""
Local multilingual sentence embedder — fastembed / ONNX on CPU (no torch, no GPU).

384-dim, matching `material_chunks.embedding vector(384)`. The model is multilingual
(EN/TR/ES/DE/FR/IT/RU/NL + ~40 more) and *symmetric* (paraphrase-style), so queries and
passages are embedded the same way — a Turkish question matches English passages by meaning.

Used by:
  - the copilot for runtime QUERY embedding (semantic retrieval), and
  - the chunk+embed worker (deploy/embed-corpus.py) for PASSAGE embedding.

The model is lazy-loaded + cached, so importing this module is cheap; the (~120 MB) ONNX
weights load on first use. Degrades cleanly: if fastembed isn't installed, EMBED_OK is
False and callers fall back to keyword-only retrieval.
"""
from functools import lru_cache
from typing import List

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM  = 384

try:
    import fastembed  # noqa: F401
    EMBED_OK = True
except Exception:
    EMBED_OK = False


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(MODEL_NAME)


def embed_passages(texts: List[str]) -> List[List[float]]:
    """Embed document passages. Returns one 384-float list per input text (order preserved)."""
    texts = [t if (t and t.strip()) else " " for t in texts]
    if not texts:
        return []
    return [v.tolist() for v in _model().embed(texts)]


def embed_query(text: str) -> List[float]:
    """Embed a single search query → 384 floats."""
    return embed_passages([text or " "])[0]


def vec_literal(v) -> str:
    """Render an embedding as a pgvector text literal for a `%s::vector` parameter."""
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"
