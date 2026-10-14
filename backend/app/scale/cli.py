"""Operator CLI:  python -m app.scale.cli <command>

  connect add  --kind KIND --name NAME --config config.json     (kinds: python -m app.scale.cli connect kinds)
  connect list
  connect test --kind KIND --config config.json     sign in and show what a scan would see (SharePoint, OneDrive); changes nothing
  scan         --connection NAME            queue a scan now
  principals add --user USERNAME --principal group:<entra-group-id>
  status                                    items by state (skipped / failed reasons included), jobs, index size
  jobs requeue-dead
  index-existing                            queue chunk-indexing for documents uploaded before the scale layer / via the UI
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from app.scale import acl as acl_mod
from app.scale import index as ix
from app.scale import queue
from app.scale.connectors import available_kinds
from app.scale.db import engine
from app.scale.sync import create_connection


def _connection_id(name: str) -> str:
    with engine().connect() as conn:
        row = conn.execute(text("SELECT connection_id FROM source_connections WHERE name = :n"), {"n": name}).first()
    if row is None:
        raise SystemExit(f"no connection named {name!r}")
    return str(row[0])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scale")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    csub = c.add_subparsers(dest="sub", required=True)
    add = csub.add_parser("add")
    add.add_argument("--kind", required=True, choices=available_kinds())
    add.add_argument("--name", required=True)
    add.add_argument("--config", required=True, help="path to a JSON file (no secrets: name env vars instead)")
    tst = csub.add_parser("test")
    tst.add_argument("--kind", required=True, choices=available_kinds())
    tst.add_argument("--config", required=True, help="path to a JSON file (no secrets: name env vars instead)")
    csub.add_parser("list")
    csub.add_parser("kinds")
    s = sub.add_parser("scan")
    s.add_argument("--connection", required=True)
    pr = sub.add_parser("principals")
    prsub = pr.add_subparsers(dest="sub", required=True)
    pa = prsub.add_parser("add")
    pa.add_argument("--user", required=True)
    pa.add_argument("--principal", required=True, action="append")
    sub.add_parser("status")
    sub.add_parser("index-existing")
    rc = sub.add_parser("reconcile", help="compare the documents with the chunk index and re-embed whatever is missing or stale")
    rc.add_argument("--dry-run", action="store_true", help="report what would be repaired, change nothing")
    rc.add_argument("--force", action="store_true", help="compare every document now, not only those due")
    rc.add_argument("--orphans", action="store_true", help="also delete chunks whose document no longer exists")
    mg = sub.add_parser("migration", help="a changed embedding model: how far the rebuild is, and deleting the old index when it is done")
    mgsub = mg.add_subparsers(dest="sub", required=True)
    mgsub.add_parser("status")
    rt = mgsub.add_parser("retire", help="delete the previous index (refused while documents are still on it)")
    rt.add_argument("--force", action="store_true")
    j = sub.add_parser("jobs")
    jsub = j.add_subparsers(dest="sub", required=True)
    jsub.add_parser("requeue-dead")
    args = p.parse_args(argv)

    if args.cmd == "connect" and args.sub == "add":
        with open(args.config) as fh:
            config = json.load(fh)
        print(create_connection(args.kind, args.name, config))
    elif args.cmd == "connect" and args.sub == "test":
        from app.scale.connectors import build_connector

        with open(args.config) as fh:
            connector = build_connector(args.kind, json.load(fh))
        if not hasattr(connector, "test"):
            raise SystemExit(f"{args.kind} has no connection test yet")
        print(json.dumps(connector.test(), indent=2))
    elif args.cmd == "connect" and args.sub == "kinds":
        print("\n".join(available_kinds()))
    elif args.cmd == "connect" and args.sub == "list":
        with engine().connect() as conn:
            for r in conn.execute(text("SELECT connection_id, kind, name, enabled FROM source_connections ORDER BY name")):
                print(*r, sep="\t")
    elif args.cmd == "scan":
        cid = _connection_id(args.connection)
        print("queued" if queue.enqueue("scan", {"connection_id": cid}, dedupe_key=f"scan:{cid}") else "already queued")
    elif args.cmd == "principals" and args.sub == "add":
        with engine().begin() as conn:
            row = conn.execute(
                text("SELECT user_id FROM users WHERE lower(username) = lower(:u)"), {"u": args.user}
            ).first()
            if row is None:
                raise SystemExit(f"no user {args.user!r}")
            for principal in args.principal:
                conn.execute(
                    text(
                        "INSERT INTO user_principals (user_id, principal, source) VALUES (:u, :p, 'cli') "
                        "ON CONFLICT (user_id, principal) DO UPDATE SET synced_at = now()"
                    ),
                    {"u": row[0], "p": acl_mod.normalize_principal(principal)},
                )
        print("ok")
    elif args.cmd == "status":
        with engine().connect() as conn:
            print("source items by state:")
            for r in conn.execute(text("SELECT state, count(*) FROM source_items WHERE NOT deleted GROUP BY 1 ORDER BY 1")):
                print(f"  {r[0]:<10} {r[1]}")
            print("skipped / failed reasons (top 15):")
            for r in conn.execute(
                text(
                    "SELECT state, left(coalesce(error, ''), 60) AS why, count(*) FROM source_items "
                    "WHERE state IN ('skipped', 'failed') AND NOT deleted GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15"
                )
            ):
                print(f"  {r[0]:<8} {r[2]:>8}  {r[1]}")
            print("documents by tier:")
            for r in conn.execute(text("SELECT tier, count(*) FROM documents GROUP BY 1")):
                print(f"  {r[0]:<6} {r[1]}")
        print("jobs:", json.dumps(queue.stats()))
        age = queue.oldest_queued_age_seconds()
        print("oldest queued job age (s):", None if age is None else round(age))
        print("chunks in index (approx):", ix.count_chunks())
        from app.scale import reconcile

        print("index health:", json.dumps(reconcile.summary(), default=str))
    elif args.cmd == "index-existing":
        from app.scale import pipeline

        total = 0
        while True:
            n = pipeline.enqueue_unindexed(limit=5000)
            total += n
            if n < 5000:
                break
        print(f"queued {total} documents for chunk-indexing; run the worker to process them")
    elif args.cmd == "reconcile":
        from app.scale import reconcile

        result = reconcile.run(dry_run=args.dry_run, force=args.force, orphans=True if args.orphans else None, budget_s=3600.0)
        print(json.dumps(result, indent=2))
        if not args.dry_run and result["requeued"]:
            print(f"queued {result['requeued']} documents for re-embedding; run the worker to process them")
    elif args.cmd == "migration":
        from app.scale import migration

        if args.sub == "status":
            print(json.dumps(migration.status(), indent=2))
        else:
            try:
                print("deleted:", migration.retire(force=args.force) or "nothing")
            except RuntimeError as exc:
                print(f"refused: {exc}")
                return 1
    elif args.cmd == "jobs" and args.sub == "requeue-dead":
        print("requeued", queue.requeue_dead())
    return 0


if __name__ == "__main__":
    sys.exit(main())
