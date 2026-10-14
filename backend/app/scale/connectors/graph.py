"""Generic Microsoft Graph document-library source: you give it the drive ids yourself.

Most people want `sharepoint` (site addresses) or `onedrive` (people) instead; both do the lookups for you. This kind stays
for installations that already use it and for drives you want to name directly. The scanning, the read-only guard and the
permission handling are in graph_common.py.
"""

from __future__ import annotations

from app.scale.connectors.graph_common import (  # noqa: F401  (re-exported: other modules and tests import them from here)
    GRAPH, Drive, GraphDriveConnector, GraphError, ReadOnlyViolation, assert_read_only, permissions_to_acl,
)


class GraphConnector(GraphDriveConnector):
    kind = "graph"
