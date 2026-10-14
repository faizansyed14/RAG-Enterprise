"""Embedding and reranking clients.

Both are deliberately thin and vendor-neutral so the same code runs against a hosted API today and
a self-hosted GPU server (vLLM / text-embeddings-inference / Ollama) when the client's data may not
leave their network. Swapping is configuration, not code.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from typing import Protocol, Sequence

import httpx

from app.scale.settings import ScaleSettings, get_scale_settings

log = logging.getLogger(__name__)


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    pass


class OpenAICompatEmbedder:
    """POST {base_url}/embeddings with the OpenAI request shape. Batches, retries, and refuses to
    return vectors whose size differs from the configured dimension (a silent mismatch would corrupt
    the index)."""

    def __init__(self, cfg: ScaleSettings | None = None) -> None:
        cfg = cfg or get_scale_settings()
        self._cfg = cfg
        self.dim = cfg.embed_dim
        self.name = cfg.embed_model
        from openai import OpenAI

        self._client = OpenAI(
            base_url=cfg.embed_base_url, api_key=cfg.embed_api_key or "none", timeout=cfg.embed_timeout_s,
            max_retries=0,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, self._cfg.embed_doc_prefix)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, self._cfg.embed_query_prefix)

    def _embed(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        out: list[list[float]] = []
        batch = max(1, self._cfg.embed_batch)
        for start in range(0, len(texts), batch):
            part = [prefix + t if t.strip() else " " for t in texts[start: start + batch]]
            out.extend(self._embed_batch(part))
        return out

    def _embed_batch(self, part: list[str]) -> list[list[float]]:
        last: Exception | None = None
        for attempt in range(4):
            try:
                response = self._client.embeddings.create(model=self._cfg.embed_model, input=part)
                vectors = [item.embedding for item in sorted(response.data, key=lambda d: d.index)]
                if len(vectors) != len(part):
                    raise EmbeddingError(f"asked for {len(part)} embeddings, got {len(vectors)}")
                for vector in vectors:
                    if len(vector) != self.dim:
                        raise EmbeddingError(
                            f"embedding has {len(vector)} dimensions but SCALE_EMBED_DIM={self.dim}; "
                            "fix the setting (or the model) before indexing anything"
                        )
                return vectors
            except EmbeddingError:
                raise
            except Exception as exc:  # noqa: BLE001 -- network / rate limit: retry with backoff
                last = exc
                time.sleep(min(2 ** attempt, 8))
        raise EmbeddingError(f"embedding request failed after retries: {last}")


class HashingEmbedder:
    """Deterministic, offline, NOT semantic: a signed hashed bag of words. It exists so the whole
    pipeline (chunking, indexing, ACL filtering, fusion, queue, connectors) can be tested on a
    machine with no model server. Never use it for real retrieval quality claims."""

    name = "hashing-test-embedder"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


class OnnxEmbedder:
    """A local ONNX sentence-embedding model (MiniLM, BGE, E5, GTE ... any encoder exported with a
    `last_hidden_state` or pooled output) run by onnxruntime on CPU or GPU. Meant for air-gapped
    clients and for tests with a real model. Needs `pip install onnxruntime tokenizers`.

    SCALE_ONNX_MODEL_DIR must hold `tokenizer.json` and `onnx/model.onnx` (or `model.onnx`).
    Inputs are sorted by length before batching so short texts are not padded to the longest one.
    """

    def __init__(self, cfg: ScaleSettings | None = None) -> None:
        cfg = cfg or get_scale_settings()
        import os

        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        root = cfg.onnx_model_dir
        if not root:
            raise EmbeddingError("SCALE_ONNX_MODEL_DIR is not set")
        model_path = next((p for p in (os.path.join(root, "onnx", "model.onnx"), os.path.join(root, "model.onnx")) if os.path.exists(p)), None)
        tok_path = os.path.join(root, "tokenizer.json")
        if model_path is None or not os.path.exists(tok_path):
            raise EmbeddingError(f"{root} must contain tokenizer.json and onnx/model.onnx")
        self._tok = Tokenizer.from_file(tok_path)
        self._tok.enable_truncation(cfg.onnx_max_tokens)
        self._tok.enable_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = cfg.onnx_threads
        self._session = ort.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._pooling = cfg.onnx_pooling
        self._batch = max(1, cfg.embed_batch)
        self._doc_prefix, self._query_prefix = cfg.embed_doc_prefix, cfg.embed_query_prefix
        self.dim = cfg.embed_dim
        self.name = cfg.embed_model
        probe = self._run(["probe"])
        if probe.shape[1] != self.dim:
            raise EmbeddingError(f"the ONNX model produces {probe.shape[1]}-dim vectors but SCALE_EMBED_DIM={self.dim}")

    def _run(self, texts: list[str]):
        np = self._np
        enc = self._tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        out = self._session.run(None, feeds)[0]
        if out.ndim == 3:
            if self._pooling == "cls":
                out = out[:, 0, :]
            else:
                m = mask[..., None].astype(np.float32)
                out = (out * m).sum(1) / np.maximum(m.sum(1), 1e-9)
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)

    def _embed(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        if not texts:
            return []
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        result: list[list[float]] = [[]] * len(texts)
        for start in range(0, len(order), self._batch):
            idx = order[start: start + self._batch]
            vecs = self._run([prefix + (texts[i] if texts[i].strip() else " ") for i in idx])
            for i, v in zip(idx, vecs):
                result[i] = v.tolist()
        return result

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, self._doc_prefix)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, self._query_prefix)


class LRU:
    """A small thread-safe least-recently-used map (the caches below are shared by the API's worker threads)."""

    def __init__(self, size: int) -> None:
        import collections
        import threading

        self.size, self._d, self._lock = max(0, size), collections.OrderedDict(), threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                self.hits += 1
                return self._d[key]
            self.misses += 1
            return None

    def put(self, key, value) -> None:
        if self.size <= 0:
            return
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)


def embed_queries(embedder: Embedder, texts: Sequence[str]) -> list[list[float]]:
    """Queries and passages use different prefixes with some models (E5, BGE-instruct). Embedders that
    do not distinguish them simply embed. The same question (a retry, a regenerated answer, a follow-up that repeats the
    search) is embedded once: the vectors are kept per embedder in a small LRU (SCALE_QUERY_CACHE_SIZE)."""
    fn = getattr(embedder, "embed_queries", None) or embedder.embed
    cache = getattr(embedder, "_query_cache", None)
    if cache is None:
        cache = LRU(get_scale_settings().query_cache_size)
        try:
            embedder._query_cache = cache  # type: ignore[attr-defined]
        except AttributeError:
            return fn(texts)
    out: list[list[float] | None] = [cache.get(t) for t in texts]
    missing = [i for i, v in enumerate(out) if v is None]
    if missing:
        fresh = fn([texts[i] for i in missing])
        for i, vec in zip(missing, fresh):
            out[i] = vec
            cache.put(texts[i], vec)
    return out  # type: ignore[return-value]


_EMBEDDERS: dict[str, Embedder] = {}
_EMBEDDERS_LOCK = threading.Lock()


def get_embedder(cfg: ScaleSettings | None = None) -> Embedder:
    """One embedder per complete embedding configuration, shared by every thread in the process. Loading an ONNX model
    (tokenizer + session) per document made ingestion pay model start-up on every file."""
    cfg = cfg or get_scale_settings()
    key = cfg.model_dump_json()
    found = _EMBEDDERS.get(key)
    if found is not None:
        return found
    with _EMBEDDERS_LOCK:
        found = _EMBEDDERS.get(key)
        if found is None:
            if cfg.embed_backend == "hashing":
                found = HashingEmbedder(cfg.embed_dim)
            elif cfg.embed_backend == "onnx":
                found = OnnxEmbedder(cfg)
            else:
                found = OpenAICompatEmbedder(cfg)
            if len(_EMBEDDERS) >= 4:                    # a changed configuration replaces the old one; do not collect models
                _EMBEDDERS.clear()
            _EMBEDDERS[key] = found
        return found


def clear_embedder_cache() -> None:
    with _EMBEDDERS_LOCK:
        _EMBEDDERS.clear()


class Reranker(Protocol):
    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[tuple[int, float]] | None: ...


class HttpReranker:
    """Cross-encoder reranking over HTTP. Returns [(index_into_documents, score)] best first, or None
    on any failure -- retrieval must degrade to the fused order, never fail the question."""

    def __init__(self, cfg: ScaleSettings | None = None) -> None:
        self._cfg = cfg or get_scale_settings()

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[tuple[int, float]] | None:
        cfg = self._cfg
        if not documents:
            return []
        headers = {"Content-Type": "application/json"}
        if cfg.rerank_api_key:
            headers["Authorization"] = f"Bearer {cfg.rerank_api_key}"
        if cfg.rerank_style == "tei":
            body = {"query": query, "texts": list(documents), "truncate": True}
        else:
            body = {"model": cfg.rerank_model, "query": query, "documents": list(documents), "top_n": top_n}
        try:
            response = httpx.post(cfg.rerank_url, json=body, headers=headers, timeout=cfg.rerank_timeout_s)
            response.raise_for_status()
            data = response.json()
            items = data if isinstance(data, list) else (data.get("results") or data.get("data") or [])
            scored = [
                (int(item["index"]), float(item.get("relevance_score", item.get("score", 0.0))))
                for item in items
            ]
        except Exception:  # noqa: BLE001
            log.warning("reranker unavailable; keeping fused order", exc_info=True)
            return None
        scored = [(i, s) for i, s in scored if 0 <= i < len(documents)]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_n]


class OnnxReranker:
    """A local cross-encoder (ms-marco-MiniLM-L-6, BGE-reranker, jina-reranker ... anything exported as a sequence
    classifier with one logit per (query, passage) pair) run by onnxruntime. It reads the question and the passage
    TOGETHER, which a bi-encoder embedding cannot, so it separates near-duplicate passages that differ in one
    detail. Needs `pip install onnxruntime tokenizers`; SCALE_RERANK_ONNX_DIR holds tokenizer.json and
    onnx/model.onnx. Returns None on any failure, like HttpReranker: retrieval degrades to the fused order.
    """

    def __init__(self, cfg: ScaleSettings | None = None) -> None:
        cfg = cfg or get_scale_settings()
        import os

        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        root = cfg.rerank_onnx_dir
        if not root:
            raise EmbeddingError("SCALE_RERANK_ONNX_DIR is not set")
        model_path = next((p for p in (os.path.join(root, "onnx", "model.onnx"), os.path.join(root, "model.onnx")) if os.path.exists(p)), None)
        tok_path = os.path.join(root, "tokenizer.json")
        if model_path is None or not os.path.exists(tok_path):
            raise EmbeddingError(f"{root} must contain tokenizer.json and onnx/model.onnx")
        self._np = np
        self._tok = Tokenizer.from_file(tok_path)
        self._tok.enable_truncation(cfg.rerank_onnx_max_tokens)
        self._tok.enable_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = cfg.rerank_onnx_threads
        self._session = ort.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._batch = max(1, cfg.rerank_onnx_batch)
        self._cache = LRU(cfg.rerank_cache_size)      # (question, passage) -> logit: a repeated question costs nothing

    @staticmethod
    def _key(query: str, document: str) -> bytes:
        return hashlib.blake2b(f"{query}\x00{document}".encode("utf-8", "replace"), digest_size=12).digest()

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Raw logits (higher = more relevant), in the order of `documents`."""
        np = self._np
        if not documents:
            return []
        out = [0.0] * len(documents)
        keys = [self._key(query, d) for d in documents]
        todo = []
        for i, k in enumerate(keys):
            cached = self._cache.get(k)
            if cached is None:
                todo.append(i)
            else:
                out[i] = cached
        # Similar lengths together, so short passages are not padded to the longest one.
        order = sorted(todo, key=lambda i: len(documents[i]))
        for start in range(0, len(order), self._batch):
            idx = order[start: start + self._batch]
            enc = self._tok.encode_batch([(query, documents[i] if documents[i].strip() else " ") for i in idx])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            if "token_type_ids" in self._inputs:
                feeds["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
            logits = self._session.run(None, feeds)[0]
            logits = logits.reshape(len(idx), -1)[:, -1]          # one logit per pair (the last column if there are two classes)
            for i, value in zip(idx, logits):
                out[i] = float(value)
                self._cache.put(keys[i], out[i])
        return out

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[tuple[int, float]] | None:
        try:
            scores = self.score(query, documents)
        except Exception:  # noqa: BLE001
            log.warning("local reranker failed; keeping fused order", exc_info=True)
            return None
        ranked = sorted(enumerate(scores), key=lambda pair: pair[1], reverse=True)
        return ranked[:top_n]


_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning(message)


def get_reranker(cfg: ScaleSettings | None = None) -> Reranker | None:
    cfg = cfg or get_scale_settings()
    if cfg.rerank_url:
        return HttpReranker(cfg)
    if cfg.rerank_mode == "onnx":
        try:
            return OnnxReranker(cfg)
        except Exception as exc:  # noqa: BLE001 -- search must work without the reranker: it only improves the order
            _warn_once("reranker", f"the local reranker is not available ({exc}); searching without it. "
                                   "Run scripts/get-models.sh --reranker, or set SCALE_RERANK_MODE=none to silence this.")
    return None
