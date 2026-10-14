"""Two-stage retrieval.

Stage A (scales): hybrid search over the chunk index, filtered by the caller's principals,
reranked, grouped into at most SCALE_CANDIDATE_DOCS candidate documents.

Stage B: the tree-search agent reads those candidates page by page, with its
evidence gate and citations. Because the candidate set is small and already authorised, none of the
agent's per-document operations has to touch the other millions of documents.

`page_search_hook` is the seam that also lets the agent's own search_content tool see the hybrid
index (rag_core/postgres_store.py).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import text

from app.scale import acl as acl_mod
from app.scale import index as ix
from app.scale.db import engine
from app.scale.models import Embedder, Reranker, embed_queries, get_embedder, get_reranker
from app.scale.settings import ScaleSettings, get_scale_settings
from app.scale.text import terms as analyze_terms

log = logging.getLogger(__name__)


@lru_cache
def _embedder() -> Embedder:
    return get_embedder()


@lru_cache
def _reranker() -> Reranker | None:
    return get_reranker()


def reset_model_caches() -> None:
    _embedder.cache_clear()
    _reranker.cache_clear()


@dataclass
class Candidate:
    document_id: str
    doc_id: str
    name: str
    path: str
    best_score: float
    pages: list[int]


def _page_texts(keys: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """(rag doc id, page) -> page text, one query for the whole batch."""
    if not keys:
        return {}
    docs = sorted({k[0] for k in keys})
    pages = sorted({k[1] for k in keys})
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT doc_id, page_number, text FROM document_pages "
                "WHERE doc_id = ANY(:docs) AND page_number = ANY(:pages)"
            ),
            {"docs": docs, "pages": pages},
        ).all()
    wanted = set(keys)
    return {(r[0], r[1]): r[2] for r in rows if (r[0], r[1]) in wanted}


def hydrate(hits: list[ix.ChunkHit]) -> list[ix.ChunkHit]:
    """Fill in chunk text for hits whose text was not stored in the index."""
    missing = [h for h in hits if not h.text]
    if not missing:
        return hits
    pages = _page_texts([(h.doc_id, h.page) for h in missing])
    for h in missing:
        page = pages.get((h.doc_id, h.page), "")
        h.text = page[h.start: h.end] if h.end > h.start else page
    return hits


def _idf_weights(query_terms: list[str], term_sets: list[set[str]]) -> dict[str, float]:
    """How much each question word is worth, judged from the candidates themselves: a word that every
    candidate contains says nothing about which one is right; a word only one contains says a lot."""
    n = len(term_sets)
    out: dict[str, float] = {}
    for t in query_terms:
        df = sum(1 for ts in term_sets if t in ts)
        out[t] = math.log(1.0 + (n - df + 0.5) / (df + 0.5)) if n >= 5 else 1.0
    return out


def coverage_scores(query: str, hits: list[ix.ChunkHit], cfg: ScaleSettings) -> list[float]:
    """Share of the question that a chunk covers, in [0, 1], without any model.

    Weighted by how informative each word is among the candidates, with a bonus for adjacent question words that
    are also adjacent in the chunk (a phrase), and for words found in the chunk's title / folder / heading. This is
    the cheap 'does the text actually say what was asked' check that vectors are bad at.
    """
    an = ix.analyzer_for(cfg, query=True)
    q_all = [t for t in analyze_terms(query, an, query=True) if "\u241f" not in t]
    q_words = list(dict.fromkeys(q_all))
    if not q_words:
        return [0.0] * len(hits)
    index_an = ix.analyzer_for(cfg)
    body_terms = [analyze_terms(h.text, index_an) for h in hits]
    term_sets = [set(t) for t in body_terms]
    w = _idf_weights(q_words, term_sets)
    pairs = list(dict.fromkeys(zip(q_all, q_all[1:])))
    pair_w = {p: max(w.get(p[0], 1.0), w.get(p[1], 1.0)) for p in pairs}
    uni_total = sum(w.values()) or 1.0
    pair_total = sum(pair_w.values())
    out: list[float] = []
    for hit, ts, seq in zip(hits, term_sets, body_terms):
        head = set(analyze_terms(f"{hit.name} {hit.path}", index_an))
        uni = sum(w[t] * (1.0 if t in ts else (0.5 if t in head else 0.0)) for t in q_words)
        if pairs:
            adjacent = {(a, b) for a, b in zip(seq, seq[1:])}
            bi = sum(pair_w[p] for p in pairs if p in adjacent)
            score = (0.4 * uni / uni_total + 0.6 * bi / pair_total) if pair_total else uni / uni_total
        else:
            score = uni / uni_total
        out.append(min(1.0, score))
    return out


def rank_documents(hits: list[ix.ChunkHit]) -> list[str]:
    """Document ids in the order of their best chunk. (Summing the evidence of several chunks per document was
    measured and was neutral at best and badly wrong at worst - long documents win by sheer chunk count - so the
    best chunk decides.)"""
    return list(dict.fromkeys(h.document_id for h in hits))


def search_chunks(
    query: str,
    *,
    principals: set[str] | None,
    authorized_doc_ids: list[str] | None = None,
    document_ids: list[str] | None = None,
    limit: int | None = None,
    rerank: bool = True,
    allow_unfiltered: bool = False,
    cfg: ScaleSettings | None = None,
) -> list[ix.ChunkHit]:
    cfg = cfg or get_scale_settings()
    limit = limit or cfg.final_k
    pool = max(limit, cfg.rerank_candidates if rerank else limit)
    hits = _retrieve(query, principals, authorized_doc_ids, document_ids, pool, allow_unfiltered, cfg)
    if not hits:
        return []
    hits = hydrate(hits)
    reranker = _reranker() if rerank and _worth_reranking(query, cfg) else None
    if reranker is not None and len(hits) > 1:
        # Only the best `rerank_top` fused chunks go to the model (cost is linear in it); the rest keep their order
        # behind them. A remote reranker is given the whole pool, as before.
        scored = hits if cfg.rerank_url else hits[: max(cfg.rerank_top, 2)]
        ranked = reranker.rerank(query, [h.text for h in scored], top_n=len(scored))
        if ranked:
            hits = _apply_rerank(scored, ranked, cfg) + hits[len(scored):]
    return hits[:limit]


def _retrieve(query, principals, authorized_doc_ids, document_ids, pool, allow_unfiltered, cfg) -> list[ix.ChunkHit]:
    """The hybrid search, plus (when switched on and a model is available) the question's key terms on the keyword side and
    the question translated into other languages. Failure of the model only ever means the plain question is searched."""
    plan = None
    if cfg.query_keywords or cfg.query_translate.strip():
        from app.scale import enrich

        plan = enrich.plan_query(query, cfg)
    sparse_text = f"{query} {' '.join(plan.keywords)}" if plan and plan.keywords else query
    common = dict(principals=principals, authorized_doc_ids=authorized_doc_ids, document_ids=document_ids, limit=pool,
                  allow_unfiltered=allow_unfiltered, cfg=cfg)
    hits = ix.search(embed_queries(_embedder(), [query])[0], sparse_text, **common)
    hits = _with_legacy(hits, query, sparse_text, common, pool, cfg)
    if plan and plan.translations:
        best = {h.id: h for h in hits}
        for translated in plan.translations:
            for h in ix.search(embed_queries(_embedder(), [translated])[0], translated, **common):
                if h.id not in best or h.score > best[h.id].score:
                    best[h.id] = h
        hits = sorted(best.values(), key=lambda h: h.score, reverse=True)[:pool]
    return hits


def _with_legacy(hits: list[ix.ChunkHit], query: str, sparse_text: str, common: dict, pool: int, cfg: ScaleSettings) -> list[ix.ChunkHit]:
    """While a new embedding model is still being built, the previous collection is searched too (with the model that built it),
    so a model change never turns into "nothing found". A legacy hit counts only for a document not yet rebuilt; the same
    access filter applies. Anything wrong with the old index means only that it is skipped."""
    from app.scale import migration

    try:
        legacy = migration.legacy_configs(cfg)
        if not legacy:
            return hits
        extra: list[ix.ChunkHit] = []
        for _, legacy_cfg in legacy:
            vector = embed_queries(get_embedder(legacy_cfg), [query])[0]
            extra.extend(ix.search(vector, sparse_text, **{**common, "cfg": legacy_cfg}))
        if not extra:
            return hits
        done = migration.migrated_ids(sorted({h.document_id for h in extra}), cfg)
        extra = [h for h in extra if h.document_id not in done]
        merged = {h.id: h for h in hits}
        for h in extra:
            if h.id not in merged or h.score > merged[h.id].score:
                merged[h.id] = h
        return sorted(merged.values(), key=lambda h: h.score, reverse=True)[:pool]
    except Exception:  # noqa: BLE001
        log.warning("the previous index could not be searched; using the current one only", exc_info=True)
        return hits


def _worth_reranking(query: str, cfg: ScaleSettings) -> bool:
    """Is this the kind of question a cross-encoder helps with? (Full sentences, not keyword strings or code lookups.)"""
    if cfg.rerank_gate == "off" or (cfg.rerank_gate == "auto" and cfg.rerank_url):
        return True
    if len(query.split()) < cfg.rerank_min_words:
        return False
    if cfg.rerank_skip_identifiers and question_identifiers(query):
        return False
    return True


def combine_scores(fused: list[float], model: list[float], blend: float) -> list[float]:
    """Merge the retrieval score and the cross-encoder score of the same chunks into one (higher = better):
    (1-b) * fused + b * model, each min-max scaled over these chunks."""
    if not fused:
        return []

    def scaled(values: list[float]) -> list[float]:
        lo, hi = min(values), max(values)
        span = hi - lo
        return [(v - lo) / span if span > 1e-12 else 0.0 for v in values]

    f, m = scaled(fused), scaled(model)
    return [(1.0 - blend) * f[i] + blend * m[i] for i in range(len(fused))]


def _apply_rerank(scored: list[ix.ChunkHit], ranked: list[tuple[int, float]], cfg: ScaleSettings) -> list[ix.ChunkHit]:
    """Re-order `scored` by the reranker's verdict, blended with the retrieval score (SCALE_RERANK_BLEND). A remote
    reranker that returned only its top_n leaves the unmentioned chunks behind the mentioned ones, in fused order."""
    model = {i: s for i, s in ranked if 0 <= i < len(scored)}
    if len(model) < len(scored):
        order = [i for i, _ in sorted(model.items(), key=lambda p: p[1], reverse=True)]
        rest = [i for i in range(len(scored)) if i not in model]
        out = []
        for i in order + rest:
            hit = scored[i]
            if i in model:
                hit.score = model[i]
            out.append(hit)
        return out
    fused = [h.score for h in scored]
    merged = combine_scores(fused, [model[i] for i in range(len(scored))], cfg.rerank_blend)
    order = sorted(range(len(scored)), key=lambda i: merged[i], reverse=True)
    out = []
    for i in order:
        scored[i].score = merged[i]
        out.append(scored[i])
    return out


def question_identifiers(query: str) -> list[str]:
    """Codes in the question that mix letters and digits (INC2576651, CTR-2025-2186-TD, B27). Years, versions and bare
    numbers are not included: they appear in thousands of unrelated chunks and prove nothing."""
    return [t for t in ix.identifier_tokens(query) if len(t) >= 3 and any(c.isalpha() for c in t) and any(c.isdigit() for c in t)]


def identifier_found(query: str, hits: list[ix.ChunkHit]) -> bool:
    """Does a code the question asks about occur in one of the best chunks (or its file name / path)? Then the
    question is about something that exists, whatever the other scores say."""
    ids = question_identifiers(query)
    if not ids or not hits:
        return False
    haystack = " ".join(f"{h.name} {h.path} {h.text}" for h in hits[:20]).casefold()
    return any(i in haystack for i in ids)


def relevance_signals(query: str, hits: list[ix.ChunkHit], cfg: ScaleSettings, with_coverage: bool = True) -> tuple[float | None, float, float]:
    """The three numbers the "nothing relevant" rule looks at, for the best chunks of a search:
    (top cosine or None when the fusion keeps no raw scores, top BM25, share of the question the top chunk covers).
    A code from the question found in the results is conclusive: all three are infinite, so no threshold turns it away."""
    cos = max((h.dense_score for h in hits if h.dense_score is not None), default=None)
    if cos is not None and identifier_found(query, hits):
        return math.inf, math.inf, math.inf
    lex = max((h.sparse_score for h in hits if h.sparse_score is not None), default=0.0)
    cov = coverage_scores(query, hits[:20], cfg)[0] if (with_coverage and hits) else 0.0
    return cos, lex, cov


def is_off_topic(cos: float | None, lex: float, cov: float | None, cfg: ScaleSettings) -> bool:
    """True when every signal that is switched on (threshold > 0) is weak. A signal that is off never votes."""
    if cos is None:
        return False                                  # fusion kept no raw scores: nothing to judge
    weak = []
    if cfg.min_confidence > 0:
        weak.append(cos < cfg.min_confidence)
    if cfg.min_lexical > 0:
        weak.append(lex < cfg.min_lexical)
    if cfg.min_coverage > 0 and cov is not None:
        weak.append(cov < cfg.min_coverage)
    return bool(weak) and all(weak)


def candidates_from_hits(hits: list[ix.ChunkHit], cfg: ScaleSettings | None = None, query: str | None = None) -> list[Candidate]:
    """Chunk hits -> documents, best first (before the Postgres permission re-check). `query` is only needed for the
    coverage signal of the "nothing relevant" rule (SCALE_MIN_COVERAGE)."""
    cfg = cfg or get_scale_settings()
    if (cfg.min_confidence > 0 or cfg.min_lexical > 0 or cfg.min_coverage > 0) and hits:
        if query:
            cos, lex, cov = relevance_signals(query, hits, cfg, with_coverage=cfg.min_coverage > 0)
        else:                                          # no question text to measure coverage or identifiers against
            cos = max((h.dense_score for h in hits if h.dense_score is not None), default=None)
            lex = max((h.sparse_score for h in hits if h.sparse_score is not None), default=0.0)
            cov = None
        if is_off_topic(cos, lex, cov, cfg):
            return []
    by_doc: dict[str, Candidate] = {}
    for h in hits:
        if h.document_id not in by_doc:
            by_doc[h.document_id] = Candidate(h.document_id, h.doc_id, h.name, h.path, h.score, [])
        pages = by_doc[h.document_id].pages
        if h.page not in pages:
            pages.append(h.page)
    order = rank_documents(hits)
    if cfg.family_cap > 0 and len(order) > cfg.family_cap:
        from app.scale import families

        best: dict[str, ix.ChunkHit] = {}
        for h in hits:
            if h.text and h.document_id not in best:           # hits are in rank order: the first one is the document's best
                best[h.document_id] = h
        order = families.demote_copies(order, {d: h.text for d, h in best.items()}, cap=cfg.family_cap,
                                       threshold=cfg.family_similarity)
    return [by_doc[d] for d in order]


def candidate_documents(
    query: str, principals: set[str] | None, cfg: ScaleSettings | None = None,
    within: list[str] | None = None,
) -> list[Candidate]:
    """Stage A: which documents should the agent read for this question?

    Authorisation is checked twice: the index query is filtered by `principals`, and the resulting
    document ids are re-checked against document_access (a stale index payload can drop a document
    here, never add one).
    """
    cfg = cfg or get_scale_settings()
    bypass = principals is None  # only SCALE_ADMIN_BYPASS_ACL passes None; see scale/scope.py
    hits = search_chunks(
        query, principals=principals, document_ids=within, limit=cfg.candidate_chunks, rerank=True,
        allow_unfiltered=bypass, cfg=cfg,
    )
    candidates = candidates_from_hits(hits, cfg, query)
    order = [c.document_id for c in candidates]
    by_doc = {c.document_id: c for c in candidates}
    allowed = set(order) if bypass else acl_mod.visible_document_ids(order, principals)
    return [by_doc[d] for d in order if d in allowed][: cfg.candidate_docs]


# ── the seam used by rag_core/postgres_store.py::search_pages ──


def page_search_hook(query: str, limit: int, doc_ids) -> list[dict]:
    """Hybrid hits in the shape search_pages returns: [{doc_id, name, page, text}] (page text).

    `doc_ids` is the agent's allowlist (rag doc ids). With no allowlist this returns nothing:
    the hook never searches the whole corpus on its own.
    """
    if doc_ids is None:
        return []
    allowed = [str(d) for d in doc_ids]
    if not allowed:
        return []
    try:
        hits = search_chunks(query, principals=None, authorized_doc_ids=allowed, limit=max(limit, 1), rerank=False)
    except Exception:  # noqa: BLE001 -- the keyword search must keep working if the index is down
        log.warning("hybrid page search failed; using keyword results only", exc_info=True)
        return []
    seen: set[tuple[str, int]] = set()
    unique = []
    for h in hits:
        key = (h.doc_id, h.page)
        if key not in seen:
            seen.add(key)
            unique.append(h)
    texts = _page_texts(list(seen))
    from app.scale import chunk_edit

    texts = chunk_edit.overlay_pages(texts)
    return [
        {"doc_id": h.doc_id, "name": h.name, "page": h.page, "text": texts.get((h.doc_id, h.page), h.text)}
        for h in unique
    ]


def merge_page_results(
    keyword: list[dict], mode: str, hybrid: list[dict], limit: int, cfg: ScaleSettings | None = None
) -> tuple[list[dict], str]:
    """Combine the Postgres keyword hits with the hybrid hits.

    union   (default) keyword hits keep their place; hybrid-only hits are appended up to `limit`.
    hybrid  hybrid ranking only (keyword hits that the hybrid search also found are kept, in hybrid order).
    """
    cfg = cfg or get_scale_settings()
    if not hybrid:
        return keyword, mode
    if cfg.merge_mode == "hybrid":
        return hybrid[:limit], "hybrid"
    seen = {(r["doc_id"], r["page"]) for r in keyword}
    merged = list(keyword)
    for r in hybrid:
        if len(merged) >= limit:
            break
        if (r["doc_id"], r["page"]) not in seen:
            merged.append(r)
            seen.add((r["doc_id"], r["page"]))
    return merged[:limit], (mode if keyword else "semantic")
