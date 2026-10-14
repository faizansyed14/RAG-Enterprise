"""Self-healing index: delete vectors, drop the collection, change the model, leave stray chunks -- the reconcile job must
notice each one and bring the chunks back from Postgres, without touching sources and without ever widening access."""

import uuid

import pytest
from sqlalchemy import text

from app.scale import chunk_edit, index as ix, pipeline, reconcile, search, worker
from app.scale.db import engine
from app.scale.pieces import EmbeddingError, _check_vectors
from app.scale.settings import get_scale_settings
from qdrant_client import models as qm

from .helpers import doc, drain, ingest
from .test_legacy_documents import PREFIX, clean_legacy, make_legacy_doc  # noqa: F401  (autouse fixture)


def _para(i: int) -> str:
    topics = "payment insurance termination confidentiality warranty delivery liability audit renewal arbitration".split()
    return " ".join([f"Section {i} covers {topics[i % len(topics)]} terms for the supplier agreement."] + [f"clause{i}x{j}" for j in range(26)])


BODY = "\n\n".join(_para(i) for i in range(8)) + "\n"


@pytest.fixture(autouse=True)
def env(monkeypatch, object_store):
    monkeypatch.setenv("SCALE_CHUNK_WORDS", "30")
    monkeypatch.setenv("SCALE_CHUNK_OVERLAP_WORDS", "0")
    monkeypatch.setenv("SCALE_MIN_CHUNK_WORDS", "3")
    monkeypatch.setenv("SCALE_RECONCILE_GRACE_MINUTES", "0")
    get_scale_settings.cache_clear()


def count(document_id: str) -> int:
    return len(ix.scroll_document(document_id))


def queued(kind: str | None = None) -> int:
    with engine().connect() as conn:
        return conn.execute(text("SELECT count(*) FROM scale_jobs WHERE status = 'queued' AND (:k IS NULL OR kind = :k)"), {"k": kind}).scalar_one()


def row(document_id: str) -> dict:
    with engine().connect() as conn:
        return dict(conn.execute(
            text("SELECT chunks_indexed_at, chunks_expected, index_signature, reconciled_at FROM documents WHERE document_id = :d"),
            {"d": document_id}).mappings().one())


@pytest.fixture(autouse=True)
def clean_state():
    with engine().begin() as conn:
        conn.execute(text("DELETE FROM scale_state"))
    reconcile._SHORT.clear()
    yield


@pytest.fixture
def lite(tmp_path):
    ingest(tmp_path, {"public/terms.txt": BODY})
    d = doc("terms.txt")["document_id"]
    assert count(d) >= 6
    return d


def heal() -> dict:
    result = reconcile.run(force=True)
    drain()
    return result


def test_indexing_records_what_it_wrote(lite):
    r = row(lite)
    assert r["chunks_expected"] == count(lite) > 0
    assert r["index_signature"] == reconcile.index_signature()


def test_a_healthy_index_is_left_alone(lite):
    result = reconcile.run(force=True)
    assert result["verdicts"] == {"ok": 1}
    assert result["requeued"] == 0
    assert queued() == 0


def test_deleted_chunks_are_regenerated(lite):
    before = count(lite)
    ix.delete_points(lite, [1, 3, 4])
    assert count(lite) == before - 3
    result = heal()
    assert result["verdicts"] == {"partial": 1}
    assert count(lite) == before
    assert reconcile.run(force=True)["verdicts"] == {"ok": 1}


def test_a_document_with_no_chunks_at_all_comes_back(lite):
    ix.delete_document_chunks(lite)
    assert count(lite) == 0
    assert heal()["verdicts"] == {"missing": 1}
    assert count(lite) > 0
    assert doc("terms.txt")["document_id"] in {c.document_id for c in search.candidate_documents("supplier arbitration", {"*"})}


def test_the_whole_collection_dropped_is_rebuilt_for_every_kind_of_document(lite):
    legacy = make_legacy_doc("a.txt", "Vacation policy: employees accrue two days of leave per month.")
    pipeline.enqueue_unindexed()
    drain()
    assert count(legacy["document_id"]) > 0
    ix.get_client().delete_collection(ix.collection_name())
    ix.reset_client_cache()
    result = heal()
    assert result["collection_missing"] is True
    assert result["verdicts"] == {"missing": 2}
    assert count(lite) > 0 and count(legacy["document_id"]) > 0
    assert PREFIX + "a.txt" in [c.name for c in search.candidate_documents("vacation leave per month", {"*"})]


def test_a_chunk_that_lost_only_its_vector_is_regenerated(lite):
    client, name = ix.get_client(), ix.collection_name()
    client.delete_vectors(name, points=[ix.point_id(lite, 2)], vectors=["dense"])
    assert lite in ix.documents_without_vector([lite])
    assert heal()["verdicts"] == {"no_vector": 1}
    assert ix.documents_without_vector([lite]) == set()


def test_leftover_chunks_of_an_interrupted_rebuild_are_removed(lite):
    before = count(lite)
    point = ix.get_points(lite, [0], with_vectors=True)[0]
    ix.get_client().upsert(ix.collection_name(), points=[qm.PointStruct(
        id=ix.point_id(lite, 900), vector={"dense": point.vector["dense"]}, payload={**point.payload, "chunk": 900})])
    assert count(lite) == before + 1
    assert heal()["verdicts"] == {"extra": 1}
    assert count(lite) == before and 900 not in ix.chunk_numbers(lite)


def test_a_changed_embedding_model_fills_the_new_collection_by_itself(lite, monkeypatch):
    old = ix.collection_name()
    monkeypatch.setenv("SCALE_EMBED_MODEL", "another-" + uuid.uuid4().hex[:6])
    get_scale_settings.cache_clear()
    new = ix.collection_name()
    assert new != old and count(lite) == 0
    assert heal()["verdicts"] == {"signature": 1}
    assert count(lite) > 0
    assert row(lite)["index_signature"] == reconcile.index_signature()
    assert ix.get_client().count(old).count > 0            # the old collection is left as it was


def test_changed_chunk_size_rebuilds_with_the_new_size(lite, monkeypatch):
    before = count(lite)
    monkeypatch.setenv("SCALE_CHUNK_WORDS", "60")
    get_scale_settings.cache_clear()
    assert heal()["verdicts"] == {"signature": 1}
    assert 0 < count(lite) < before
    assert row(lite)["chunks_expected"] == count(lite)


def test_orphan_chunks_of_a_missing_document_are_deleted(lite):
    ghost = str(uuid.uuid4())
    point = ix.get_points(lite, [0], with_vectors=True)[0]
    ix.get_client().upsert(ix.collection_name(), points=[qm.PointStruct(
        id=ix.point_id(ghost, 0), vector={"dense": point.vector["dense"]}, payload={**point.payload, "document_id": ghost})])
    assert count(ghost) == 1
    assert reconcile.sweep_orphans(dry_run=True) == 1 and count(ghost) == 1
    assert reconcile.sweep_orphans() == 1
    assert count(ghost) == 0 and count(lite) > 0


def test_a_dry_run_changes_nothing(lite):
    ix.delete_points(lite, [0, 1])
    stamp = row(lite)
    result = reconcile.run(force=True, dry_run=True)
    assert result["verdicts"] == {"partial": 1} and result["requeued"] == 0
    assert row(lite) == stamp and queued() == 0


def test_hand_written_chunks_are_not_mistaken_for_damage(lite):
    made = chunk_edit.add_chunk(lite, "Special clause: the penalty rate is eleven percent.", page=1)
    assert reconcile.run(force=True)["verdicts"] == {"ok": 1}
    chunk_edit.delete_chunk(lite, made["chunk"])
    assert reconcile.run(force=True)["verdicts"] == {"ok": 1}
    chunk_edit.add_chunk(lite, "Another hand written note about arbitration seats.", page=1)
    ix.delete_document_chunks(lite)
    assert heal()["verdicts"] == {"missing": 1}
    assert any("arbitration seats" in c["text"] for c in chunk_edit.list_chunks(lite, limit=100)["chunks"])   # comes back too


def test_a_failed_rebuild_keeps_the_old_chunks_searchable(lite, monkeypatch):
    before = ix.chunk_numbers(lite)

    class Broken:
        def embed(self, texts):
            raise RuntimeError("embedding service down")

    monkeypatch.setattr("app.scale.pieces.get_embedder", lambda: Broken())
    with pytest.raises(RuntimeError):
        pipeline.index_document(lite)
    assert ix.chunk_numbers(lite) == before


def test_a_rebuild_never_leaves_the_document_without_chunks(lite, monkeypatch):
    seen = []
    real = ix.upsert_chunks

    def spy(chunks, cfg=None, wait=True):
        seen.append(count(lite))               # what is searchable at the moment the new chunks are being written
        return real(chunks, cfg, wait)

    monkeypatch.setattr(ix, "upsert_chunks", spy)
    pipeline.index_document(lite)
    assert seen and min(seen) > 0


def test_embedder_output_is_checked_before_it_is_stored():
    cfg = get_scale_settings()
    good = [[0.1] * cfg.embed_dim]
    _check_vectors(good, 1, cfg)
    with pytest.raises(EmbeddingError):
        _check_vectors(good, 2, cfg)                                           # lost a vector
    with pytest.raises(EmbeddingError):
        _check_vectors([[0.1] * (cfg.embed_dim + 1)], 1, cfg)                  # wrong model / dimension
    with pytest.raises(EmbeddingError):
        _check_vectors([[float("nan")] * cfg.embed_dim], 1, cfg)


def test_the_judgement_table():
    j = reconcile.judge
    s = "sig"
    assert j(5, 5, 0, s, s, False) == "ok"
    assert j(6, 5, 1, s, s, False) == "ok"          # a hand-written chunk
    assert j(0, 5, 0, s, s, False) == "missing"
    assert j(0, 0, 0, s, s, False) == "ok"          # a document with no text has no chunks
    assert j(3, 5, 0, s, s, False) == "partial"
    assert j(7, 5, 0, s, s, False) == "extra"
    assert j(5, 5, 0, "old", s, False) == "signature"
    assert j(5, 5, 0, s, s, True) == "no_vector"
    assert j(4, None, 0, None, s, False) == "adopt"  # indexed before the job existed: trust what is there
    assert j(0, None, 0, None, s, False) == "missing"


def test_documents_indexed_before_this_job_existed_are_adopted_not_rebuilt(lite):
    with engine().begin() as conn:
        conn.execute(text("UPDATE documents SET chunks_expected = NULL, index_signature = NULL, reconciled_at = NULL WHERE document_id = :d"), {"d": lite})
    result = reconcile.run(force=True)
    assert result["verdicts"] == {"adopt": 1} and result["requeued"] == 0
    assert row(lite)["chunks_expected"] == count(lite)


def test_the_scheduler_queues_a_reconcile_job_only_when_there_is_something_to_compare(lite):
    worker.tick_schedule()
    assert queued("reconcile") == 1                        # never compared yet: due
    worker.tick_schedule()
    assert queued("reconcile") == 1                        # not queued twice
    drain()
    ix.delete_points(lite, [0, 1])
    with engine().begin() as conn:
        conn.execute(text("UPDATE documents SET reconciled_at = now() - interval '2 days' WHERE document_id = :d"), {"d": lite})
    worker.tick_schedule()
    drain()
    drain()                                                # the reconcile job queues the re-embedding, the next pass runs it
    assert {0, 1} <= ix.chunk_numbers(lite)


def test_the_requeue_cap_keeps_the_queue_bounded(lite, monkeypatch):
    monkeypatch.setenv("SCALE_RECONCILE_REQUEUE_CAP", "1")
    get_scale_settings.cache_clear()
    make_legacy_doc("b.txt", "Second legacy document about safety training.")
    make_legacy_doc("c.txt", "Third legacy document about fire drills.")
    pipeline.enqueue_unindexed()
    drain()
    ix.get_client().delete_collection(ix.collection_name())
    ix.reset_client_cache()
    first = reconcile.run(force=True)
    again = reconcile.run(force=True)
    assert first["requeued"] >= 1 and again.get("verdicts", {}).get("deferred_backlog") == 1
    drain()                                                 # the backlog is worked off ...
    assert reconcile.run(force=True)["verdicts"] == {"ok": 3}   # ... and every document is back


def test_a_wiped_index_is_noticed_without_waiting_for_each_documents_turn(lite):
    """The documents were all compared minutes ago, so none is 'due'; the whole-index size check is what notices."""
    reconcile._SHORT.clear()
    assert reconcile.run()["verdicts"] == {}                  # healthy: nothing due, nothing to do
    ix.get_client().delete_collection(ix.collection_name())
    ix.reset_client_cache()
    reconcile._SHORT.clear()
    assert reconcile.has_work() is True
    result = reconcile.run()                                  # not forced
    assert result["verdicts"] == {"missing": 1}
    drain()
    assert count(lite) > 0
    reconcile._SHORT.clear()
    assert reconcile.index_looks_short() is False


def test_the_size_check_does_not_fire_on_a_healthy_index(lite):
    reconcile._SHORT.clear()
    assert reconcile.index_looks_short(fresh=True) is False


def test_the_orphan_sweep_covers_the_whole_index_across_runs_and_remembers_where_it_stopped(lite, monkeypatch):
    ghosts = [str(uuid.uuid4()) for _ in range(5)]
    point = ix.get_points(lite, [0], with_vectors=True)[0]
    ix.get_client().upsert(ix.collection_name(), points=[
        qm.PointStruct(id=ix.point_id(g, 0), vector={"dense": point.vector["dense"]}, payload={**point.payload, "document_id": g})
        for g in ghosts])
    total = ix.count_chunks()
    monkeypatch.setenv("SCALE_RECONCILE_ORPHAN_SCAN_POINTS", "2")          # two chunks per run: many runs to cover the index
    get_scale_settings.cache_clear()
    removed, runs = 0, 0
    while runs < 100:
        runs += 1
        removed += reconcile.sweep_orphans()
        if not reconcile._get_state("orphans_cursor"):
            break
    assert removed == 5 and all(count(g) == 0 for g in ghosts) and count(lite) > 0
    assert runs > 1 and runs >= (total + 1) // 2 - 1                         # it really was split into bounded runs
    assert reconcile._get_state("orphans_last")                              # and a finished pass is recorded
