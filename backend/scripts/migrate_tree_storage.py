"""One-time data migration: copy every document's doc.json/tree.json/pages.json off the
.rag-data volume and into the document_trees table (see rag_core/postgres_store.py).

Idempotent -- safe to re-run; each document is an upsert (ON CONFLICT DO UPDATE), so running
this twice just rewrites the same rows. The .rag-data files are left in place, untouched.

Run after `alembic upgrade head`:
    docker compose --env-file .env.dev -f docker-compose.dev.yml exec backend python -m scripts.migrate_tree_storage
"""

import json
import sys
from pathlib import Path

from app.core.config import get_settings
from app.rag_core.postgres_store import PostgresDocStore


def _read_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    root = Path(get_settings().rag_storage_path) / "docs"
    if not root.is_dir():
        print(f"No tree storage directory at {root} -- nothing to migrate.")
        return 0

    store = PostgresDocStore()
    migrated = 0
    skipped: list[str] = []

    for doc_dir in sorted(root.iterdir()):
        if not doc_dir.is_dir():
            continue
        doc_id = doc_dir.name
        try:
            meta = _read_json(doc_dir / "doc.json")
            tree = _read_json(doc_dir / "tree.json")
            pages = _read_json(doc_dir / "pages.json")
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append(f"{doc_id}: {exc}")
            continue

        store.save_document(doc_id, meta, tree, pages)
        migrated += 1
        print(f"  migrated {doc_id}  ({meta.get('name', '?')})")

    print(f"\nMigrated {migrated} document(s).")
    if skipped:
        print(f"Skipped {len(skipped)}:")
        for line in skipped:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
