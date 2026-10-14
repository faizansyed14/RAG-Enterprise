import uuid

import pytest
from sqlalchemy import text

from app.scale import acl
from app.scale.db import engine


def test_normalisation():
    assert acl.normalize_principal("Group:ABC-123") == "group:abc-123"
    assert acl.normalize_principal("sid:s-1-5-21-1") == "sid:S-1-5-21-1"
    assert acl.normalize_principal("*") == "*"
    assert acl.normalize_acl(["user:A@x.com", "user:a@x.com", "group:G"]) == ["group:g", "user:a@x.com"]
    with pytest.raises(ValueError):
        acl.normalize_principal("nobody")
    with pytest.raises(ValueError):
        acl.normalize_principal("role:admin")


def test_principals_for_user_include_mapped_groups(make_pg_user):
    uid, name = make_pg_user("Alice@Corp.com")
    with engine().begin() as conn:
        conn.execute(text("INSERT INTO user_principals (user_id, principal) VALUES (:u, 'group:legal')"), {"u": uid})
    p = acl.principals_for_user(uid, "Alice@Corp.com")
    assert {"*", f"user:{uid}", "user:alice@corp.com", "group:legal"} <= p


def _doc(conn, name="x.pdf"):
    d = uuid.uuid4()
    conn.execute(
        text("INSERT INTO documents (document_id, filename, doc_type, content_hash, storage_key, status, tier) "
             "VALUES (:d, :n, 'pdf', :h, 'k', 'indexed', 'lite')"),
        {"d": d, "n": name, "h": uuid.uuid4().hex},
    )
    return d


def _item(conn, conn_id, doc, ext, acl_list, deleted=False):
    conn.execute(
        text("INSERT INTO source_items (connection_id, external_id, path, name, version, acl, document_id, deleted) "
             "VALUES (:c, :e, :e, :e, 'v', :a, :d, :x)"),
        {"c": conn_id, "e": ext, "a": acl_list, "d": doc, "x": deleted},
    )


@pytest.fixture
def connection():
    with engine().begin() as conn:
        cid = conn.execute(
            text("INSERT INTO source_connections (kind, name) VALUES ('local', :n) RETURNING connection_id"),
            {"n": f"t-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()
    return cid


def test_document_reached_from_two_places_is_readable_by_the_union(connection):
    with engine().begin() as conn:
        d = _doc(conn)
        _item(conn, connection, d, "finance/a.pdf", ["group:finance"])
        _item(conn, connection, d, "legal/a.pdf", ["group:legal"])
        union = acl.recompute_document_access(conn, d)
    assert union == ["group:finance", "group:legal"]
    assert acl.can_read(d, {"group:legal"})
    assert acl.can_read(d, {"group:finance"})
    assert not acl.can_read(d, {"group:hr"})
    with engine().begin() as conn:
        conn.execute(text("DELETE FROM documents WHERE document_id = :d"), {"d": d})


def test_deleting_the_only_source_item_makes_the_document_unreadable(connection):
    with engine().begin() as conn:
        d = _doc(conn)
        _item(conn, connection, d, "a.pdf", ["group:legal"])
        acl.recompute_document_access(conn, d)
    assert acl.can_read(d, {"group:legal"})
    with engine().begin() as conn:
        conn.execute(text("UPDATE source_items SET deleted = true WHERE document_id = :d"), {"d": d})
        acl.recompute_document_access(conn, d)
    assert not acl.can_read(d, {"group:legal", "*"})
    with engine().begin() as conn:
        conn.execute(text("DELETE FROM documents WHERE document_id = :d"), {"d": d})


def test_empty_acl_means_nobody(connection):
    with engine().begin() as conn:
        d = _doc(conn)
        _item(conn, connection, d, "a.pdf", [])
        acl.recompute_document_access(conn, d)
    assert not acl.can_read(d, {"*", "group:legal"})
    with engine().begin() as conn:
        conn.execute(text("DELETE FROM documents WHERE document_id = :d"), {"d": d})


def test_manual_uploads_without_source_items_keep_their_star(connection):
    with engine().begin() as conn:
        d = _doc(conn)
        conn.execute(text("INSERT INTO document_access VALUES (:d, '*')"), {"d": d})
        assert acl.recompute_document_access(conn, d) == []   # no source items: untouched
    assert acl.can_read(d, {"*"})
    with engine().begin() as conn:
        conn.execute(text("DELETE FROM documents WHERE document_id = :d"), {"d": d})


def test_a_user_over_the_principal_cap_keeps_a_deterministic_set_and_it_is_logged(make_pg_user, monkeypatch, caplog):
    from app.scale.settings import get_scale_settings

    monkeypatch.setenv("SCALE_MAX_PRINCIPALS", "3")
    get_scale_settings.cache_clear()
    uid, name = make_pg_user()
    with engine().begin() as conn:
        for g in ("e", "b", "d", "a", "c"):
            conn.execute(text("INSERT INTO user_principals (user_id, principal) VALUES (:u, :p)"), {"u": uid, "p": f"group:{g}"})
    with caplog.at_level("WARNING"):
        got = acl.principals_for_user(uid, name)
    kept = {p for p in got if p.startswith("group:")}
    assert kept == {"group:a", "group:b", "group:c"}              # the first three, sorted: never a random three
    assert any("principals" in r.message for r in caplog.records)
