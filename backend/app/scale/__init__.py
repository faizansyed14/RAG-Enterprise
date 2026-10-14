"""Scale core: everything that lets this system go from ~1,000 documents to millions.

The existing system answers by letting an agent navigate section trees and read pages. That is
accurate, and it stays. What does not scale is how the agent *finds* documents and how the system
*ingests* them. This package adds, without replacing anything:

  settings      one place for every SCALE_* knob
  acl           who may read what; the filter that is applied inside the index query
  chunking      structure-aware chunks with page numbers and a heading trail
  models        embedding and reranker clients (any OpenAI-compatible endpoint / any /rerank endpoint)
  index         Qdrant hybrid index: dense + sparse BM25 vectors, ACL payload, rank fusion
  search        two-stage retrieval: ACL-filtered hybrid search -> candidate documents for the agent
  queue         durable Postgres job queue (SKIP LOCKED, leases, retries, dead letters)
  pipeline      source item -> object store -> pages -> chunk index (the 'lite' tier)
  sync          connector scan -> source_items -> jobs (deletes and permission changes included)
  connectors    local folder, SMB share, Microsoft Graph (SharePoint / OneDrive)
  evaluation    golden-set runner: recall@k, MRR, access-control negative tests
  worker        `python -m app.scale.worker` -- the process that does the heavy work

This layer is always on: documents, chat and the admin screens all go through it.
"""
