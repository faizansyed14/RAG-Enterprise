"""Write the SAME chunks the Qdrant run indexes, for the Lucene reference run."""
import json, sys
from pathlib import Path
from app.scale.chunking import chunk_document
from app.scale.settings import get_scale_settings
cfg = get_scale_settings()
share = Path(sys.argv[1]); manifest = {}
for l in open(sys.argv[2]): m = json.loads(l); manifest[m["path"]] = m["group"]
keep = [l.strip() for l in open(sys.argv[3]) if l.strip()]
san = lambda s: " ".join(s.replace("\t", " ").replace("\r", " ").split())
n = 0
with open(sys.argv[4], "w") as out:
    for rel in keep:
        body = (share / rel).read_text("utf-8", errors="replace")
        for c in chunk_document([body], title=Path(rel).name, path=rel, cfg=cfg):
            out.write("\t".join([f"{rel}#{c.index}", manifest[rel], rel, san(c.context), san(c.embed_text)]) + "\n"); n += 1
print(n, "chunks")
