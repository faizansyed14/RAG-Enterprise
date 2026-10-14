# Lucene reference: "what would Elasticsearch / OpenSearch give on the same chunks?"

Elasticsearch and OpenSearch score keyword search with Lucene BM25. This small program runs the same scoring on the
same chunks and questions as the pack's own index, so "do we need Elasticsearch for accuracy?" is a measurement on
YOUR documents, not an opinion. No server is needed; it is one `java` command.

```bash
# 1. the exact chunks the pack indexes (same chunker, same context prefix)
python -m scripts.lucene_reference.export_chunks  SHARE manifest.jsonl docs.txt chunks.tsv
# 2. Lucene jars: any Lucene >= 9 on the classpath (Anserini's fat jar works: https://github.com/castorini/anserini)
javac -cp lucene.jar LuceneRef.java
java  -cp .:lucene.jar LuceneRef index chunks.tsv idx english            # analyzers: english | standard | arabic
# 3. ask the same questions (queries.tsv = "id<TAB>question"); the last arguments: top-K, title boost, ACL groups, [fuzzy]
java  -cp .:lucene.jar LuceneRef query idx english queries.tsv res.tsv 120 0 group:web,group:legal
java  -cp .:lucene.jar LuceneRef query idx english queries_typo.tsv res_fuzzy.tsv 120 0 group:web,group:legal fuzzy
# 4. score with the same function the pack uses
python -m scripts.lucene_reference.score_tsv golden.json res.tsv x
```

`fuzzy` reproduces Elasticsearch `fuzziness: AUTO` (1 edit for 3-5 characters, 2 above). Use
`python -m scripts.scale_make_typos golden.json golden_typo.json queries_typo.tsv` for the misspelled questions.
