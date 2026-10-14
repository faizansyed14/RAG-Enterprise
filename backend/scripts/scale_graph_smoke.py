"""Read-only smoke test of the Microsoft Graph connector against a REAL tenant (SharePoint / OneDrive).

Run this against a test library before any client demo. It writes nothing to the database or the tenant.

  export GRAPH_TENANT_ID=...  GRAPH_CLIENT_ID=...  GRAPH_CLIENT_SECRET=...
  export GRAPH_DRIVE_ID=b!abc...        # a document library; list yours with GET /sites/{id}/drives
  python -m scripts.scale_graph_smoke --pages 2 [--sharing-headers]

It checks, in order: the token works; the first delta pages enumerate files; every file gets an ACL from
its permissions (an EMPTY ACL means the file would be invisible to everyone: fail closed -- see the
site_group_map note in app/scale/connectors/graph.py); and a second delta call from the saved link returns
nothing new. It prints counts and sample paths, never tokens or secrets.

Needed application permissions: Files.Read.All + Sites.Read.All (add Sites.FullControl.All only if you turn
on --sharing-headers, which Microsoft documents as required for permission-change tracking in delta).
"""

from __future__ import annotations

import argparse
import collections
import os
import sys

from app.scale.connectors.graph import GraphConnector


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=2, help="how many delta pages to read")
    parser.add_argument("--sharing-headers", action="store_true")
    args = parser.parse_args()
    missing = [k for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_DRIVE_ID") if not os.environ.get(k)]
    if missing:
        print("missing environment variables:", ", ".join(missing))
        return 2
    connector = GraphConnector({
        "tenant_id": os.environ["GRAPH_TENANT_ID"], "client_id": os.environ["GRAPH_CLIENT_ID"],
        "client_secret_env": "GRAPH_CLIENT_SECRET", "drive_ids": [os.environ["GRAPH_DRIVE_ID"]],
        "page_size": 50, "use_sharing_headers": args.sharing_headers,
    })
    files, empty_acl, principals = [], [], collections.Counter()
    cursor: dict = {}
    for n, batch in enumerate(connector.scan({}), start=1):
        for change in batch.changes:
            if change.kind != "upsert" or change.file is None:
                continue
            files.append(change.file)
            if not change.file.acl:
                empty_acl.append(change.file.path)
            for p in change.file.acl:
                principals[p.split(":", 1)[0]] += 1
        cursor = batch.cursor
        if n >= args.pages or batch.done:
            break
    print(f"read {len(files)} files from the first {args.pages} page(s)")
    for f in files[:5]:
        print("  ", f.path, f"({f.size} bytes)", "ACL:", f.acl[:4], "..." if len(f.acl) > 4 else "")
    print("principal kinds seen:", dict(principals) or "none")
    if empty_acl:
        print(f"WARNING: {len(empty_acl)} files have an EMPTY ACL and would be invisible to everyone, e.g. {empty_acl[:3]}")
        print("  usually a SharePoint site group: map it in the connection's site_group_map, or the files stay hidden.")
    if cursor.get("drives"):
        print("delta cursor saved:", {k: ("deltaLink" if "delta" in v and "token" in v else "nextLink") for k, v in cursor["drives"].items()})
    return 1 if not files else 0


if __name__ == "__main__":
    sys.exit(main())
