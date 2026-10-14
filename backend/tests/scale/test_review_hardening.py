"""Fixes from an outside review: one embedder per configuration, an index name that reflects what a vector means,
and retirement of finished queue jobs."""

from sqlalchemy import text

from app.scale import index as ix, models, queue
from app.scale.db import engine
from app.scale.settings import ScaleSettings, get_scale_settings


def test_the_embedder_is_built_once_per_configuration(monkeypatch):
    models.clear_embedder_cache()
    cfg = get_scale_settings()
    assert models.get_embedder(cfg) is models.get_embedder(cfg)
    other = ScaleSettings(**{**cfg.model_dump(), "embed_dim": cfg.embed_dim + 8})
    assert models.get_embedder(other) is not models.get_embedder(cfg)


def test_pooling_and_prefixes_change_the_collection_name_only_when_they_differ_from_the_defaults():
    base = get_scale_settings()
    same = ScaleSettings(**base.model_dump())
    assert ix.collection_name(base) == ix.collection_name(same)
    dump = base.model_dump()
    assert ix.collection_name(ScaleSettings(**{**dump, "embed_backend": "onnx", "onnx_pooling": "mean"})) != \
        ix.collection_name(ScaleSettings(**{**dump, "embed_backend": "onnx", "onnx_pooling": "cls"}))
    assert ix.collection_name(ScaleSettings(**{**dump, "embed_query_prefix": "query: "})) != ix.collection_name(base)
    assert ix.collection_name(ScaleSettings(**{**dump, "embed_doc_prefix": "passage: "})) != \
        ix.collection_name(ScaleSettings(**{**dump, "embed_query_prefix": "query: "}))


def _age(status: str, days: float, n: int) -> list[int]:
    ids = []
    for i in range(n):
        job = queue.enqueue("t", {"i": f"{status}{i}"})
        ids.append(job)
    with engine().begin() as conn:
        conn.execute(
            text("UPDATE scale_jobs SET status = :s, updated_at = now() - make_interval(secs => :a) WHERE job_id = ANY(:ids)"),
            {"s": status, "a": days * 86400, "ids": ids},
        )
    return ids


def test_old_finished_jobs_are_deleted_and_live_ones_never_are():
    old_done, new_done = _age("done", 30, 3), _age("done", 1, 2)
    old_dead, mid_dead = _age("dead", 90, 2), _age("dead", 20, 2)
    queued = [queue.enqueue("t", {"q": 1})]
    running = _age("running", 400, 1)
    gone = queue.prune(done_days=7, dead_days=60)
    assert gone == {"done": 3, "dead": 2}
    with engine().connect() as conn:
        left = {r[0] for r in conn.execute(text("SELECT job_id FROM scale_jobs"))}
    assert not left & set(old_done + old_dead)
    assert set(new_done + mid_dead + queued + running) <= left


def test_pruning_works_in_bounded_batches_and_zero_means_keep():
    _age("done", 30, 5)
    assert queue.prune(done_days=7, dead_days=0, batch=2)["done"] == 2
    assert queue.prune(done_days=0, dead_days=0) == {"done": 0, "dead": 0}


def test_cluster_layout_is_passed_to_a_new_collection_only_when_set():
    from app.scale.index import _layout_args

    base = get_scale_settings()
    assert _layout_args(base) == {}
    dump = base.model_dump()
    tuned = ScaleSettings(**{**dump, "qdrant_shards": 12, "qdrant_replication_factor": 2, "qdrant_write_consistency": 2,
                            "qdrant_on_disk_payload": True})
    assert _layout_args(tuned) == {"shard_number": 12, "replication_factor": 2, "write_consistency_factor": 2, "on_disk_payload": True}


def test_the_capacity_planner_does_its_arithmetic():
    from scripts.scale_capacity import plan

    p = plan(text_gb=11.0, dim=1000, chunk_bytes=1100.0, replicas=2, docs=86400.0, docs_per_second=1.0)
    assert p["chunks_million"] == 10.0                                    # 11 GB / 1,100 B
    # int8 vector 1000 B + 128 B of graph links + 600 B sparse + 450 B payload = 2,178 B per chunk, twice
    assert p["ram_bytes_per_chunk"] == 2178 and p["qdrant_ram_gb_total"] == round(10e6 * 2178 * 2 / 1e9, 1)
    assert p["first_ingest_days"] == 1.0
    lean = plan(text_gb=11.0, dim=1000, sparse_on_disk=True, payload_on_disk=True)
    assert lean["ram_bytes_per_chunk"] == 1128 < p["ram_bytes_per_chunk"]
