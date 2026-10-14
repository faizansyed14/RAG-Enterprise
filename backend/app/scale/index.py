"""The hybrid chunk index (Qdrant): dense vectors + sparse BM25-style vectors + an ACL payload.

Why this shape
  * Dense vectors find paraphrases ("termination for convenience" ~ "ending the contract early").
  * Sparse vectors find exact tokens ("SUB-084", "B27", a clause number). Qdrant applies IDF to the
    sparse side server-side (Modifier.IDF), so the stored value is the BM25 term-frequency part.
  * Both are queried with the same ACL filter and combined by weighted min-max score fusion, which keeps the raw
    cosine and BM25 for the "nothing relevant" check.
  * Every chunk carries `acl`, and the filter is attached to BOTH prefetch legs. Filtering only the
    fused result would still let forbidden chunks consume the candidate slots.
  * A permission change rewrites one payload field per document (set_payload); nothing is re-embedded.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Sequence

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.scale.settings import ScaleSettings, get_scale_settings
from app.scale.text import Analyzer, terms

log = logging.getLogger(__name__)

_NAMESPACE = uuid.UUID("b9d1b6f4-6a53-4a77-9b1e-3d3c9d1f2a10")
_TOKEN = re.compile(r"\w+(?:[-./]\w+)*", re.UNICODE)
_PART_SPLIT = re.compile(r"[-./_]")
_K1, _B = 1.2, 0.75


@dataclass
class IndexedChunk:
    document_id: str
    doc_id: str            # the page-tree id ('doc-...') that the agent tools use
    name: str              # document name exactly as stored in document_trees.meta.name
    path: str
    source: str            # connection id ('' for manual uploads)
    page: int
    chunk: int
    start: int
    end: int
    acl: list[str]
    dense: list[float]
    sparse_text: str       # text used for the sparse vector (context + chunk)
    text: str = ""         # stored only when SCALE_STORE_CHUNK_TEXT=true
    extra: dict = field(default_factory=dict)


@dataclass
class ChunkHit:
    id: str
    score: float
    document_id: str
    doc_id: str
    name: str
    path: str
    page: int
    chunk: int
    start: int
    end: int
    text: str = ""
    dense_score: float | None = None    # raw cosine
    sparse_score: float | None = None
    kind: str = ""                      # "" an extracted chunk | "edited" | "manual" (chunk editor)


# ── sparse encoding ──


def analyzer_for(cfg: ScaleSettings | None = None, *, query: bool = False) -> Analyzer:
    cfg = cfg or get_scale_settings()
    return Analyzer(
        stem=tuple(x.strip() for x in cfg.stem_langs.split(",") if x.strip()),
        stopwords_in_queries=bool(query and cfg.query_stopwords),
    )


def tokenize(text: str, cfg: ScaleSettings | None = None) -> list[str]:
    """The terms that are indexed for `text` (see app/scale/text.py). A compound such as 'sub-084' yields
    the whole token AND its parts so an exact id and a loose mention both match; Arabic and CJK are handled."""
    return terms(text, analyzer_for(cfg))


def _token_id(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(), "little")


def sparse_document(text: str, cfg: ScaleSettings | None = None, avgdl: float | None = None) -> qm.SparseVector:
    """BM25's term-frequency half. Qdrant multiplies by IDF at query time (Modifier.IDF)."""
    cfg = cfg or get_scale_settings()
    counts: dict[int, int] = {}
    tokens = tokenize(text, cfg)
    for token in tokens:
        idx = _token_id(token)
        counts[idx] = counts.get(idx, 0) + 1
    length = max(len(tokens), 1)
    norm = _K1 * (1 - _B + _B * length / (avgdl or cfg.sparse_avgdl))
    indices = sorted(counts)
    values = [counts[i] * (_K1 + 1) / (counts[i] + norm) for i in indices]
    return qm.SparseVector(indices=indices, values=values)


def identifier_tokens(text: str) -> list[str]:
    """Tokens that look like identifiers: they contain a digit and are not a bare small number.
    'SUB-084', 'REF-0000123', 'B27', '2023-0412', 'INV12345' qualify; '30', 'days', 'FY' do not."""
    out: list[str] = []
    for raw in _TOKEN.findall(text.casefold()):
        if any(ch.isdigit() for ch in raw) and (len(raw) >= 3 or not raw.isdigit()) and not (raw.isdigit() and len(raw) < 4):
            out.append(raw)
            if _PART_SPLIT.search(raw):
                out.extend(p for p in _PART_SPLIT.split(raw) if len(p) >= 4 and any(c.isdigit() for c in p))
    return list(dict.fromkeys(out))


def sparse_query(text: str, cfg: ScaleSettings | None = None) -> qm.SparseVector:
    cfg = cfg or get_scale_settings()
    indices = sorted({_token_id(t) for t in terms(text, analyzer_for(cfg, query=True), query=True)})
    return qm.SparseVector(indices=indices, values=[1.0] * len(indices))


# ── client / collection ──


def _sanitize(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


# Bump when what is STORED per chunk changes shape (chunking, the context prefix, payload fields): the collection name
# carries it, so chunks written by an older layout are never mixed with newer ones in the same collection.
INDEX_FORMAT = 2


def _vector_meaning_signature(cfg: ScaleSettings) -> str:
    """Settings that change what a vector means without changing the model's name: pooling and the query / passage prefixes.
    Added only when they differ from the defaults, so an index built before this existed keeps its collection name."""
    parts = []
    if cfg.embed_backend == "onnx" and cfg.onnx_pooling != "cls":
        parts.append(f"pool-{cfg.onnx_pooling}")
    if cfg.embed_query_prefix or cfg.embed_doc_prefix:
        parts.append("pfx-" + hashlib.blake2b(f"{cfg.embed_query_prefix}\x00{cfg.embed_doc_prefix}".encode(), digest_size=3).hexdigest())
    return "".join(f"|{p}" for p in parts)


def collection_name(cfg: ScaleSettings | None = None) -> str:
    """One collection per (embedding model, text analysis, index format). Anything that changes which terms are
    indexed is part of the name, so changing it can never silently search an index built differently."""
    cfg = cfg or get_scale_settings()
    base = f"{cfg.collection}__{_sanitize(cfg.embed_model)}"
    signature = (analyzer_for(cfg).signature()
                 + f"|a{cfg.sparse_avgdl:g}|c{int(cfg.ctx_dense)}{int(cfg.ctx_sparse)}|f{INDEX_FORMAT}"
                 + _vector_meaning_signature(cfg))
    return f"{base}__a{hashlib.blake2b(signature.encode(), digest_size=3).hexdigest()}"


@lru_cache
def _client_for(location: str, url: str, api_key: str | None) -> QdrantClient:
    if location:
        return QdrantClient(location=location)
    return QdrantClient(url=url, api_key=api_key, timeout=60)


def get_client(cfg: ScaleSettings | None = None) -> QdrantClient:
    from app.core.config import get_settings

    cfg = cfg or get_scale_settings()
    app = get_settings()
    return _client_for(cfg.qdrant_location, app.qdrant_url, app.qdrant_api_key)


_ENSURED: set[tuple] = set()   # collections this process has already verified (saves 2 HTTP calls per upsert)


def reset_client_cache() -> None:
    _COUNTS.clear()
    _client_for.cache_clear()
    _ENSURED.clear()


def ensure_collection(dim: int | None = None, cfg: ScaleSettings | None = None) -> str:
    from qdrant_client.http.exceptions import UnexpectedResponse

    cfg = cfg or get_scale_settings()
    dim = dim or cfg.embed_dim
    client = get_client(cfg)
    name = collection_name(cfg)
    key = (cfg.qdrant_location, name, dim)
    if key in _ENSURED:
        return name
    if client.collection_exists(name):
        info = client.get_collection(name)
        existing = info.config.params.vectors["dense"].size  # type: ignore[index]
        if existing != dim:
            raise RuntimeError(
                f"collection {name} stores {existing}-dim vectors but the embedder produces {dim}; "
                "use a new SCALE_COLLECTION name and re-index"
            )
        _ensure_payload_indexes(client, name)
        _ENSURED.add(key)
        _note_current(cfg)
        return name
    quant = (
        qm.ScalarQuantization(scalar=qm.ScalarQuantizationConfig(type=qm.ScalarType.INT8, always_ram=True))
        if cfg.quantize_int8 else None
    )
    try:
        client.create_collection(
            collection_name=name,
            vectors_config={
                "dense": qm.VectorParams(
                    size=dim, distance=qm.Distance.COSINE, on_disk=cfg.on_disk_vectors,
                    hnsw_config=qm.HnswConfigDiff(m=cfg.hnsw_m, ef_construct=cfg.hnsw_ef_construct),
                )
            },
            sparse_vectors_config={
                "sparse": qm.SparseVectorParams(modifier=qm.Modifier.IDF, index=qm.SparseIndexParams(on_disk=cfg.sparse_on_disk)),
            },
            quantization_config=quant,
            **_layout_args(cfg),
        )
    except (UnexpectedResponse, ValueError) as exc:
        # Several workers meet an empty index at the same moment; whoever loses the race finds it created.
        if not client.collection_exists(name):
            raise
        log.info("collection %s was created by another worker (%s)", name, type(exc).__name__)
    _ensure_payload_indexes(client, name)
    _ENSURED.add(key)
    _note_current(cfg)
    return name


def _layout_args(cfg: ScaleSettings) -> dict:
    """Shard / replica settings for a new collection, only those that were set (the embedded engine takes none of them)."""
    out: dict = {}
    if cfg.qdrant_shards > 0:
        out["shard_number"] = cfg.qdrant_shards
    if cfg.qdrant_replication_factor > 0:
        out["replication_factor"] = cfg.qdrant_replication_factor
    if cfg.qdrant_write_consistency > 0:
        out["write_consistency_factor"] = cfg.qdrant_write_consistency
    if cfg.qdrant_on_disk_payload:
        out["on_disk_payload"] = True
    return out


def _note_current(cfg: ScaleSettings) -> None:
    """Record this collection as the current build (see migration.py). Imported late: migration needs the database."""
    from app.scale import migration

    migration.note_current(cfg)


def _write_names(cfg: ScaleSettings) -> list[str]:
    """Where a permission change, rename, chunk edit or deletion goes: the current collection and any legacy one that is
    still being read, so an old index never keeps serving what was revoked."""
    from app.scale import migration

    return migration.all_names(cfg)


_PAYLOAD_INDEXES = (
    ("acl", qm.PayloadSchemaType.KEYWORD), ("document_id", qm.PayloadSchemaType.KEYWORD),
    ("doc_id", qm.PayloadSchemaType.KEYWORD), ("source", qm.PayloadSchemaType.KEYWORD),
    ("kind", qm.PayloadSchemaType.KEYWORD), ("available", qm.PayloadSchemaType.BOOL),
)


def _ensure_payload_indexes(client: QdrantClient, name: str) -> None:
    """Create any payload index the collection lacks (idempotent). A collection built before an index existed gets it
    the first time this process opens it; the filters work without the index too, only slower."""
    for field_name, schema in _PAYLOAD_INDEXES:
        try:
            client.create_payload_index(name, field_name, schema)
        except Exception:  # noqa: BLE001 -- an index that already exists, or an embedded engine that has no indexes
            log.debug("payload index %s on %s not created", field_name, name, exc_info=True)


# ── writes ──


def point_id(document_id: str, chunk: int) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{document_id}:{chunk}"))


def upsert_chunks(chunks: Sequence[IndexedChunk], cfg: ScaleSettings | None = None, wait: bool = True) -> int:
    if not chunks:
        return 0
    cfg = cfg or get_scale_settings()
    name = ensure_collection(len(chunks[0].dense), cfg)
    points = []
    for c in chunks:
        payload = {
            "document_id": c.document_id, "doc_id": c.doc_id, "name": c.name, "path": c.path,
            "source": c.source, "page": c.page, "chunk": c.chunk, "start": c.start, "end": c.end,
            "acl": c.acl, **c.extra,
        }
        if cfg.store_chunk_text:
            payload["text"] = c.text
        vector = {"dense": c.dense, "sparse": sparse_document(c.sparse_text, cfg)}
        points.append(qm.PointStruct(id=point_id(c.document_id, c.chunk), vector=vector, payload=payload))
    from qdrant_client.http.exceptions import UnexpectedResponse

    for start in range(0, len(points), 256):
        batch = points[start: start + 256]
        try:
            get_client(cfg).upsert(name, points=batch, wait=wait)
        except (UnexpectedResponse, ValueError) as exc:
            missing = getattr(exc, "status_code", None) == 404 or "not found" in str(exc).lower()   # server / embedded
            if not missing:
                raise
            _ENSURED.clear()                      # the collection was removed under us: recreate and retry once
            name = ensure_collection(len(chunks[0].dense), cfg)
            get_client(cfg).upsert(name, points=batch, wait=wait)
    return len(points)


# A chunk switched off in the chunk editor carries available=false. Chunks written before the editor existed have no such
# field and still match (a must_not on a missing field is true).
_DISABLED = qm.FieldCondition(key="available", match=qm.MatchValue(value=False))


def _doc_filter(document_id: str) -> qm.Filter:
    return qm.Filter(must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=str(document_id)))])


def delete_document_chunks(document_id: str, cfg: ScaleSettings | None = None) -> None:
    cfg = cfg or get_scale_settings()
    client = get_client(cfg)
    for name in _write_names(cfg):
        if client.collection_exists(name):
            client.delete(name, points_selector=qm.FilterSelector(filter=_doc_filter(document_id)), wait=True)


def chunk_numbers(document_id: str, cfg: ScaleSettings | None = None) -> set[int]:
    """The chunk numbers a document currently has in the index (payload only, no vectors)."""
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    if not client.collection_exists(name):
        return set()
    found: set[int] = set()
    offset = None
    while True:
        batch, offset = client.scroll(name, scroll_filter=_doc_filter(document_id), limit=1024, offset=offset,
                                      with_payload=["chunk"], with_vectors=False)
        found.update(int((p.payload or {}).get("chunk", 0)) for p in batch)
        if offset is None:
            return found


def set_document_acl(document_id: str, acl: Iterable[str], cfg: ScaleSettings | None = None) -> None:
    """Apply a permission change to every chunk of a document without re-embedding anything."""
    cfg = cfg or get_scale_settings()
    client = get_client(cfg)
    for name in _write_names(cfg):
        if client.collection_exists(name):
            client.set_payload(name, payload={"acl": sorted(set(acl))}, points=_doc_filter(document_id), wait=True)


def set_document_name(document_id: str, name: str, cfg: ScaleSettings | None = None) -> None:
    """Keep the display name in the chunk payload in step with the document's tree row."""
    cfg = cfg or get_scale_settings()
    client = get_client(cfg)
    for name_ in _write_names(cfg):
        if client.collection_exists(name_):
            client.set_payload(name_, payload={"name": name}, points=_doc_filter(document_id), wait=True)


def scroll_document(document_id: str, *, with_vectors: bool = False, cfg: ScaleSettings | None = None) -> list:
    """Every point of one document (chunks, edited and manual chunks, summaries), as qdrant records, in chunk order."""
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    if not client.collection_exists(name):
        return []
    points, offset = [], None
    while True:
        batch, offset = client.scroll(
            name, scroll_filter=_doc_filter(document_id), limit=512, offset=offset, with_payload=True,
            with_vectors=["dense"] if with_vectors else False,
        )
        points.extend(batch)
        if offset is None:
            break
    return sorted(points, key=lambda p: int((p.payload or {}).get("chunk", 0)))


def first_point(document_id: str, cfg: ScaleSettings | None = None):
    """Any one point of the document (they all share name, path, source and access), or None if it is not indexed."""
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    if not client.collection_exists(name):
        return None
    batch, _ = client.scroll(name, scroll_filter=_doc_filter(document_id), limit=1, with_payload=True, with_vectors=False)
    return batch[0] if batch else None


def get_points(document_id: str, chunks: Sequence[int], *, with_vectors: bool = False, cfg: ScaleSettings | None = None) -> list:
    """The points of the given chunk numbers of a document that exist (ids are derived, so this is a direct lookup)."""
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    if not chunks or not client.collection_exists(name):
        return []
    return client.retrieve(name, ids=[point_id(document_id, c) for c in chunks], with_payload=True,
                           with_vectors=["dense"] if with_vectors else False)


def fetch_visible(
    document_id_chunks: Sequence[tuple[str, int]], *, principals: set[str] | None, cfg: ScaleSettings | None = None,
) -> list[ChunkHit]:
    """Chunks named by (document_id, chunk), read back from the index with the SAME access filter and the same
    switched-off rule as a search. This is how the knowledge graph turns "document D, chunk 7" into a result without
    ever being able to return something the caller may not read. `principals=None` is the admin bypass."""
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    if not document_id_chunks or not client.collection_exists(name):
        return []
    if principals is not None and not principals:
        return []
    must: list[qm.Condition] = [qm.HasIdCondition(has_id=[point_id(d, c) for d, c in document_id_chunks])]
    if principals is not None:
        must.append(qm.FieldCondition(key="acl", match=qm.MatchAny(any=sorted(principals))))
    points, offset = [], None
    while True:
        batch, offset = client.scroll(
            name, scroll_filter=qm.Filter(must=must, must_not=[_DISABLED]), limit=256, offset=offset, with_payload=True,
        )
        points.extend(batch)
        if offset is None:
            break
    return [_hit(p, score=0.0) for p in points]


def delete_points(document_id: str, chunks: Sequence[int], cfg: ScaleSettings | None = None) -> None:
    cfg = cfg or get_scale_settings()
    client = get_client(cfg)
    for name in _write_names(cfg):
        if chunks and client.collection_exists(name):
            client.delete(name, points_selector=qm.PointIdsList(points=[point_id(document_id, c) for c in chunks]), wait=True)


def set_point_payload(document_id: str, chunk: int, payload: dict, cfg: ScaleSettings | None = None) -> None:
    cfg = cfg or get_scale_settings()
    client = get_client(cfg)
    for name in _write_names(cfg):
        if client.collection_exists(name):
            client.set_payload(name, payload=payload, points=[point_id(document_id, chunk)], wait=True)


def document_chunk_counts(document_ids: Sequence[str], cfg: ScaleSettings | None = None) -> dict[str, int] | None:
    """How many points each of these documents has in the index (a document with none is absent from the answer's
    non-zero values: the result has every requested id). None when the collection does not exist at all."""
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    ids = [str(d) for d in document_ids]
    if not client.collection_exists(name):
        return None
    flt = qm.Filter(must=[qm.FieldCondition(key="document_id", match=qm.MatchAny(any=ids))])
    counts = {d: 0 for d in ids}
    for hit in client.facet(name, key="document_id", facet_filter=flt, limit=len(ids) + 8, exact=True).hits:
        if str(hit.value) in counts:
            counts[str(hit.value)] = int(hit.count)
    return counts


def documents_without_vector(document_ids: Sequence[str], cfg: ScaleSettings | None = None) -> set[str]:
    """Documents that have at least one point whose dense vector is missing (a vector can be deleted on its own)."""
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    ids = [str(d) for d in document_ids]
    if not ids or not client.collection_exists(name):
        return set()
    flt = qm.Filter(
        must=[qm.FieldCondition(key="document_id", match=qm.MatchAny(any=ids))],
        must_not=[qm.HasVectorCondition(has_vector="dense")],
    )
    bad: set[str] = set()
    offset = None
    while True:
        batch, offset = client.scroll(name, scroll_filter=flt, limit=256, offset=offset, with_payload=["document_id"], with_vectors=False)
        bad.update(str((p.payload or {}).get("document_id")) for p in batch)
        if offset is None:
            return bad


def scan_document_ids(cursor: str | None, max_points: int, cfg: ScaleSettings | None = None) -> tuple[set[str], str | None]:
    """The distinct document ids found in the next `max_points` chunks of the index, and where to resume (None = reached the end).
    Walks every chunk in storage order, so a sweep of any size can be split into bounded runs and finishes."""
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    if not client.collection_exists(name):
        return set(), None
    found: set[str] = set()
    offset: object = cursor if cursor is None or not str(cursor).isdigit() else int(cursor)
    seen = 0
    while seen < max_points:
        batch, offset = client.scroll(name, limit=min(1024, max_points - seen), offset=offset, with_payload=["document_id"], with_vectors=False)
        found.update(str((p.payload or {}).get("document_id")) for p in batch)
        seen += len(batch)
        if offset is None:
            return found, None
    return found, str(offset)


def count_chunks(cfg: ScaleSettings | None = None) -> int:
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    return client.count(name, exact=False).count if client.collection_exists(name) else 0


# ── reads ──


def search(
    dense: list[float],
    query_text: str,
    *,
    principals: set[str] | None,
    authorized_doc_ids: Sequence[str] | None = None,
    document_ids: Sequence[str] | None = None,
    limit: int | None = None,
    allow_unfiltered: bool = False,
    cfg: ScaleSettings | None = None,
) -> list[ChunkHit]:
    """Hybrid search. The caller MUST supply `principals` (the ACL filter) or `authorized_doc_ids`
    (a set already checked against document_access). Supplying neither raises: an unfiltered search
    over the whole corpus is never a legitimate request -- except for the explicit, named
    `allow_unfiltered=True` used by SCALE_ADMIN_BYPASS_ACL and by evaluation baselines."""
    if principals is None and authorized_doc_ids is None and not allow_unfiltered:
        raise ValueError("search needs principals or authorized_doc_ids; refusing an unfiltered search")
    cfg = cfg or get_scale_settings()
    client, name = get_client(cfg), collection_name(cfg)
    if not client.collection_exists(name):
        return []
    must: list[qm.Condition] = []
    if principals is not None:
        if not principals:
            return []
        must.append(qm.FieldCondition(key="acl", match=qm.MatchAny(any=sorted(principals))))
    if authorized_doc_ids is not None:
        if not authorized_doc_ids:
            return []
        must.append(qm.FieldCondition(key="doc_id", match=qm.MatchAny(any=list(authorized_doc_ids))))
    if document_ids is not None:
        # A narrowing scope (the user's own selection). Never a substitute for `principals`.
        if not document_ids:
            return []
        must.append(qm.FieldCondition(key="document_id", match=qm.MatchAny(any=[str(d) for d in document_ids])))
    flt = qm.Filter(must=must, must_not=[_DISABLED])
    limit = limit or cfg.final_k
    return _search_weighted(client, name, dense, query_text, flt, limit, cfg)


def _hit(point, dense_score: float | None = None, sparse_score: float | None = None, score: float | None = None) -> ChunkHit:
    p = point.payload or {}
    return ChunkHit(
        id=str(point.id), score=float(point.score if score is None else score), document_id=p.get("document_id", ""),
        doc_id=p.get("doc_id", ""), name=p.get("name", ""), path=p.get("path", ""),
        page=int(p.get("page", 0)), chunk=int(p.get("chunk", 0)), start=int(p.get("start", 0)),
        end=int(p.get("end", 0)), text=p.get("text", ""), dense_score=dense_score, sparse_score=sparse_score,
        kind=p.get("kind", ""),
    )


def _minmax(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    span = hi - lo
    return {k: ((v - lo) / span if span > 1e-12 else 1.0) for k, v in values.items()}


_COUNTS: dict[str, tuple[float, int]] = {}          # filter -> (when, estimated number of chunks it lets through)


def _dense_search_params(client, name: str, flt: qm.Filter, cfg: ScaleSettings) -> qm.SearchParams | None:
    """Exact search when the permission filter lets only a modest number of chunks through.

    Measured at 500,000 chunks (docs/scale/BENCHMARKS.md, section 21): the graph search with a filter is fine for a user who
    sees a large share of the index (recall@10 0.99) and for one who sees a handful of documents (the engine scans them), but a
    user whose filter mixes a medium-sized public set with one group (about 13,000 chunks visible) got recall@10 of 0.49 and
    no setting of the graph search width fixed it. Scanning that many vectors exactly costs 4-15 ms. So below
    SCALE_EXACT_BELOW chunks the search is exact. The count is an estimate from the payload index, kept for a few seconds; a
    stale count can only change speed or exactness, never what a user may see (the filter itself is always applied)."""
    quant = (
        qm.QuantizationSearchParams(rescore=True, oversampling=cfg.quantize_oversampling) if cfg.quantize_int8 else None
    )
    if cfg.exact_below <= 0:
        return qm.SearchParams(quantization=quant) if quant else None
    key = f"{name}|{flt.model_dump_json()}"
    now = time.monotonic()
    hit = _COUNTS.get(key)
    if hit is None or now - hit[0] > cfg.exact_probe_ttl_s:
        try:
            n = int(client.count(name, count_filter=flt, exact=False).count)
        except Exception:  # noqa: BLE001 -- no estimate: leave the engine's own plan
            return qm.SearchParams(quantization=quant) if quant else None
        if len(_COUNTS) > 2048:
            _COUNTS.clear()
        _COUNTS[key] = hit = (now, n)
    if hit[1] <= cfg.exact_below:
        return qm.SearchParams(exact=True)
    return qm.SearchParams(quantization=quant) if quant else None


def _search_weighted(client, name, dense, query_text, flt, limit, cfg) -> list[ChunkHit]:
    """Score-based fusion: each list is min-max normalised and combined as w*dense + (1-w)*keyword. Unlike rank
    fusion this keeps how MUCH better a hit is, and it exposes the raw cosine, which is what an 'is there anything
    relevant at all?' threshold needs. Two round trips."""
    sparse_q = sparse_query(query_text, cfg)
    d = client.query_points(name, query=dense, using="dense", query_filter=flt, limit=cfg.dense_k, with_payload=True,
                            search_params=_dense_search_params(client, name, flt, cfg)).points
    s_pts = []
    if sparse_q.indices:
        s_pts = client.query_points(name, query=sparse_q, using="sparse", query_filter=flt, limit=cfg.sparse_k, with_payload=True).points
    points = {str(p.id): p for p in (*d, *s_pts)}
    dn = _minmax({str(p.id): float(p.score) for p in d})
    sn = _minmax({str(p.id): float(p.score) for p in s_pts})
    raw_d = {str(p.id): float(p.score) for p in d}
    raw_s = {str(p.id): float(p.score) for p in s_pts}
    w = cfg.fusion_dense_weight
    scored = [(w * dn.get(pid, 0.0) + (1.0 - w) * sn.get(pid, 0.0), pid) for pid in points]
    scored.sort(reverse=True)
    return [_hit(points[pid], raw_d.get(pid), raw_s.get(pid), score) for score, pid in scored[:limit]]
