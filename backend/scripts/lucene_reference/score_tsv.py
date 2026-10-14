"""Score a LuceneRef result file with the pack's own scoring.

  python -m scripts.lucene_reference.score_tsv golden.json res.tsv [x]     (a third argument adds the split by style / source)
"""
import json, sys, collections
if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
    print(__doc__)
    sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
sys.path.insert(0, ".")
from scripts.scale_realeval import rank_of, score_ranks, split_by
gold = {g["id"]: g for g in json.load(open(sys.argv[1]))}
ranked = collections.defaultdict(list)
for line in open(sys.argv[2]):
    qid, rank, path, score = line.rstrip("\n").split("\t")
    if path not in ranked[qid]: ranked[qid].append(path)       # chunk -> document, best chunk first
rows = []
for qid, g in gold.items():
    r = rank_of(ranked.get(qid, [])[:40], g["expect_paths"])
    rows.append({"rank": r, "style": g["style"], "source": g["source"]})
s = score_ranks([r["rank"] for r in rows])
print(sys.argv[2].split("/")[-1].ljust(28), {k: s[k] for k in ("hit@1","hit@5","hit@10","hit@40","mrr")})
if len(sys.argv) > 3:
    print("   by style ", {k: (v["hit@1"], v["hit@10"]) for k, v in split_by(rows, "style").items()})
    print("   by source", {k: (v["hit@1"], v["hit@10"]) for k, v in split_by(rows, "source").items()})
