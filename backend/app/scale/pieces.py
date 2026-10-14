"""One searchable piece of a document, however it came to exist.

An ordinary chunk, a chunk a person corrected and a chunk a person added by hand are all the same thing to the
index: some text, the context it belongs to and optional search terms. This module turns a list of them into points
(embedding and writing them), so the ingest pipeline and the chunk editor share one code path and cannot drift apart.

What is embedded and what is keyword-indexed is derived here and nowhere else:
  dense   [context +] text [+ keywords and questions: always the ones a person wrote, the model's only with SCALE_ENRICH_DENSE]
  sparse  [context +] text + every keyword and question
The stored/shown text is always `text` alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.scale import acl as acl_mod
from app.scale import index as ix
from app.scale.models import get_embedder
from app.scale.settings import ScaleSettings, get_scale_settings


@dataclass
class Piece:
    page: int
    index: int
    text: str
    context: str = ""
    start: int = 0
    end: int = 0
    kind: str = ""                         # "" | "edited" | "manual"
    keywords: list[str] = field(default_factory=list)          # written by the enrichment model
    questions: list[str] = field(default_factory=list)
    user_keywords: list[str] = field(default_factory=list)     # written by a person in the chunk editor
    user_questions: list[str] = field(default_factory=list)
    available: bool = True

    # -- derived text -------------------------------------------------------------------------------------------
    def _terms(self, *, dense: bool, cfg: ScaleSettings) -> list[str]:
        out = list(self.user_keywords) + list(self.user_questions)
        if not dense or cfg.enrich_dense:
            out += list(self.keywords) + list(self.questions)
        return out

    def _compose(self, with_context: bool, extra: list[str]) -> str:
        body = f"{self.context}\n{self.text}" if (with_context and self.context) else self.text
        return body + ("\n" + "\n".join(extra) if extra else "")

    def embed_text(self, cfg: ScaleSettings) -> str:
        return self._compose(cfg.ctx_dense, self._terms(dense=True, cfg=cfg))

    def sparse_text(self, cfg: ScaleSettings) -> str:
        return self._compose(cfg.ctx_sparse, self._terms(dense=False, cfg=cfg))

    def payload_extra(self) -> dict:
        extra: dict = {}
        if self.kind:
            extra["kind"] = self.kind
            extra["text"] = self.text
        for key, value in (("kw_llm", self.keywords), ("q_llm", self.questions),
                           ("kw_user", self.user_keywords), ("q_user", self.user_questions)):
            if value:
                extra[key] = list(value)
        if not self.available:
            extra["available"] = False
        return extra


class EmbeddingError(RuntimeError):
    """The embedder returned something that must not be stored. Raised so the job fails and is retried, instead of
    writing chunks that can never be found."""


def _check_vectors(vectors, expected: int, cfg: ScaleSettings) -> None:
    if len(vectors) != expected:
        raise EmbeddingError(f"the embedder returned {len(vectors)} vectors for {expected} texts")
    for v in vectors:
        if len(v) != cfg.embed_dim:
            raise EmbeddingError(f"the embedder returned a {len(v)}-dimensional vector; SCALE_EMBED_DIM is {cfg.embed_dim}")
        norm = math.fsum(x * x for x in v)
        # (the offline test embedder legitimately gives a zero vector for text with no latin letters or digits)
        if not math.isfinite(norm) or (norm <= 1e-12 and cfg.embed_backend != "hashing"):
            raise EmbeddingError("the embedder returned an empty or non-finite vector")


def index_pieces(
    document_id: str, rag_doc_id: str, name: str, path: str, source: str, acl: list[str], pieces: list[Piece],
    cfg: ScaleSettings | None = None,
) -> int:
    """Embed and write `pieces` (the caller has removed what they replace). Returns how many points were written."""
    if not pieces:
        return 0
    cfg = cfg or get_scale_settings()
    embedder = get_embedder()
    vectors: list[list[float]] = []
    step = max(1, cfg.embed_batch)
    for start in range(0, len(pieces), step):
        batch = pieces[start: start + step]
        got = embedder.embed([p.embed_text(cfg) for p in batch])
        _check_vectors(got, len(batch), cfg)
        vectors.extend(got)
    normalized = acl_mod.normalize_acl(acl)
    indexed = [
        ix.IndexedChunk(
            document_id=document_id, doc_id=rag_doc_id, name=name, path=path, source=source,
            page=p.page, chunk=p.index, start=p.start, end=p.end, acl=normalized, dense=v,
            sparse_text=p.sparse_text(cfg), text=p.text, extra=p.payload_extra(),
        )
        for p, v in zip(pieces, vectors)
    ]
    return ix.upsert_chunks(indexed, cfg)
