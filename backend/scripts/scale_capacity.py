"""Capacity planner: from your corpus, how many chunks, how much memory and disk, and how long the first ingest takes.

  python -m scripts.scale_capacity --text-gb 60 --dim 1024 --replicas 2 --docs-per-second 12

Arithmetic from the formulas in docs/scale/CAPACITY.md, NOT a measurement. Replace every input with a measured one
(count files by type, extract a sample, run scripts/scale_bench.py on your hardware) before buying anything.
"""

from __future__ import annotations

import argparse
import json

KB = 1000.0
GB = 1000.0 ** 3


def plan(*, text_gb: float, dim: int = 384, chunk_bytes: float = 1100.0, replicas: int = 1, quantize_int8: bool = True,
         sparse_on_disk: bool = False, payload_on_disk: bool = False, hnsw_m: int = 16, sparse_bytes: float = 600.0,
         payload_bytes: float = 450.0, docs: float | None = None, docs_per_second: float | None = None,
         node_ram_gb: float = 128.0, ram_headroom: float = 0.6) -> dict:
    """`text_gb` is the EXTRACTED text, not the size of the files: a terabyte of scanned images may hold 10 GB of text."""
    chunks = text_gb * GB / chunk_bytes
    links = 2 * hnsw_m * 4.0                                   # layer-0 links, 4 bytes each
    ram_per_chunk = (dim if quantize_int8 else dim * 4.0) + links
    ram_per_chunk += 0 if sparse_on_disk else sparse_bytes
    ram_per_chunk += 0 if payload_on_disk else payload_bytes
    disk_per_chunk = dim * 4.0 + sparse_bytes + payload_bytes + links       # originals stay on disk either way
    ram = chunks * ram_per_chunk * replicas
    disk = chunks * disk_per_chunk * replicas
    usable = node_ram_gb * GB * ram_headroom
    out = {
        "chunks_million": round(chunks / 1e6, 1),
        "qdrant_ram_gb_total": round(ram / GB, 1),
        "qdrant_disk_gb_total": round(disk / GB, 1),
        "qdrant_nodes_for_ram": max(1, int(-(-ram // usable))),
        "ram_bytes_per_chunk": round(ram_per_chunk),
        "postgres_disk_gb_estimate": round(text_gb * 3.0, 1),   # page text + section trees + indexes: about 3x the text
        "assumptions": {"chunk_bytes": chunk_bytes, "dim": dim, "replicas": replicas, "int8_vectors_in_ram": quantize_int8,
                        "sparse_on_disk": sparse_on_disk, "payload_on_disk": payload_on_disk, "node_ram_gb": node_ram_gb,
                        "ram_headroom": ram_headroom},
    }
    if docs and docs_per_second:
        out["first_ingest_days"] = round(docs / docs_per_second / 86400.0, 1)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--text-gb", type=float, required=True, help="extracted text, in GB (not the size of the files)")
    p.add_argument("--dim", type=int, default=384)
    p.add_argument("--chunk-bytes", type=float, default=1100.0, help="average bytes of text per chunk (160 words is about 1,100)")
    p.add_argument("--replicas", type=int, default=1, help="copies of every shard")
    p.add_argument("--no-quantize", action="store_true", help="keep float32 vectors in RAM")
    p.add_argument("--sparse-on-disk", action="store_true")
    p.add_argument("--payload-on-disk", action="store_true")
    p.add_argument("--docs", type=float, help="number of documents (for the ingest time)")
    p.add_argument("--docs-per-second", type=float, help="your MEASURED ingest rate across all workers")
    p.add_argument("--node-ram-gb", type=float, default=128.0)
    a = p.parse_args(argv)
    print(json.dumps(plan(text_gb=a.text_gb, dim=a.dim, chunk_bytes=a.chunk_bytes, replicas=a.replicas, quantize_int8=not a.no_quantize,
                          sparse_on_disk=a.sparse_on_disk, payload_on_disk=a.payload_on_disk, docs=a.docs,
                          docs_per_second=a.docs_per_second, node_ram_gb=a.node_ram_gb), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
