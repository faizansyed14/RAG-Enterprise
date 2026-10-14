import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.apache.lucene.analysis.Analyzer;
import org.apache.lucene.analysis.ar.ArabicAnalyzer;
import org.apache.lucene.analysis.en.EnglishAnalyzer;
import org.apache.lucene.analysis.standard.StandardAnalyzer;
import org.apache.lucene.analysis.CharArraySet;
import org.apache.lucene.analysis.TokenStream;
import org.apache.lucene.analysis.tokenattributes.CharTermAttribute;
import org.apache.lucene.document.*;
import org.apache.lucene.index.*;
import org.apache.lucene.search.*;
import org.apache.lucene.search.similarities.BM25Similarity;
import org.apache.lucene.store.*;

/** Reference lexical search with Lucene's BM25 (the scoring engine inside Elasticsearch/OpenSearch).
 *  index: java LuceneRef index <chunks.jsonl-as-tsv> <indexDir> <analyzer>
 *  query: java LuceneRef query <indexDir> <analyzer> <queries.tsv> <out.tsv> <topK> <titleBoost> [filterGroups,comma]
 *  chunks tsv: id \t group \t path \t title \t body   (tabs/newlines replaced by spaces)
 *  queries tsv: qid \t query
 *  output tsv: qid \t rank \t path \t score
 */
public class LuceneRef {
  static Analyzer analyzer(String name) {
    switch (name) {
      case "english": return new EnglishAnalyzer();
      case "arabic": return new ArabicAnalyzer();
      case "standard-nostop": return new StandardAnalyzer(CharArraySet.EMPTY_SET);
      default: return new StandardAnalyzer();
    }
  }
  public static void main(String[] a) throws Exception {
    if (a[0].equals("index")) index(a); else query(a);
  }
  static void index(String[] a) throws Exception {
    Analyzer an = analyzer(a[3]);
    IndexWriterConfig cfg = new IndexWriterConfig(an);
    cfg.setSimilarity(new BM25Similarity(1.2f, 0.75f));
    cfg.setRAMBufferSizeMB(256);
    long t0 = System.currentTimeMillis(); int n = 0;
    try (Directory dir = FSDirectory.open(Paths.get(a[2])); IndexWriter w = new IndexWriter(dir, cfg);
         BufferedReader r = Files.newBufferedReader(Paths.get(a[1]), StandardCharsets.UTF_8)) {
      String line;
      while ((line = r.readLine()) != null) {
        String[] p = line.split("\t", 5);
        if (p.length < 5) continue;
        Document d = new Document();
        d.add(new StringField("id", p[0], Field.Store.YES));
        d.add(new StringField("group", p[1], Field.Store.NO));
        d.add(new StoredField("path", p[2]));
        d.add(new TextField("title", p[3], Field.Store.NO));
        d.add(new TextField("body", p[4], Field.Store.NO));
        w.addDocument(d); n++;
      }
      w.commit();
    }
    System.out.println("indexed " + n + " chunks in " + (System.currentTimeMillis() - t0) + " ms");
  }
  static boolean FUZZY = false;
  static Query term(String field, String t) {
    if (!FUZZY || t.length() < 3) return new TermQuery(new Term(field, t));
    // Elasticsearch "fuzziness: AUTO": 1 edit for 3-5 characters, 2 edits above; prefix_length 0, max_expansions 50
    return new FuzzyQuery(new Term(field, t), t.length() <= 5 ? 1 : 2, 0, 50, true);
  }
  static Query parse(Analyzer an, String field, String text, float boost) throws IOException {
    BooleanQuery.Builder b = new BooleanQuery.Builder();
    try (TokenStream ts = an.tokenStream(field, text)) {
      CharTermAttribute c = ts.addAttribute(CharTermAttribute.class);
      ts.reset();
      Set<String> seen = new LinkedHashSet<>();
      while (ts.incrementToken()) seen.add(c.toString());
      ts.end();
      for (String t : seen) b.add(term(field, t), BooleanClause.Occur.SHOULD);
    }
    Query q = b.build();
    return boost == 1f ? q : new BoostQuery(q, boost);
  }
  static void query(String[] a) throws Exception {
    Analyzer an = analyzer(a[2]);
    int topK = Integer.parseInt(a[5]);
    float titleBoost = Float.parseFloat(a[6]);
    Set<String> groups = a.length > 7 && !a[7].equals("-") ? new HashSet<>(Arrays.asList(a[7].split(","))) : null;
    FUZZY = a.length > 8 && a[8].equals("fuzzy");
    List<String> qlines = Files.readAllLines(Paths.get(a[3]), StandardCharsets.UTF_8);
    try (Directory dir = FSDirectory.open(Paths.get(a[1])); DirectoryReader rd = DirectoryReader.open(dir);
         PrintWriter out = new PrintWriter(Files.newBufferedWriter(Paths.get(a[4]), StandardCharsets.UTF_8))) {
      IndexSearcher s = new IndexSearcher(rd);
      s.setSimilarity(new BM25Similarity(1.2f, 0.75f));
      long t0 = System.nanoTime();
      for (String line : qlines) {
        String[] p = line.split("\t", 2);
        BooleanQuery.Builder b = new BooleanQuery.Builder();
        b.add(parse(an, "body", p[1], 1f), BooleanClause.Occur.SHOULD);
        if (titleBoost > 0f) b.add(parse(an, "title", p[1], titleBoost), BooleanClause.Occur.SHOULD);
        BooleanQuery.Builder outer = new BooleanQuery.Builder();
        outer.add(b.build(), BooleanClause.Occur.MUST);
        if (groups != null) {
          BooleanQuery.Builder g = new BooleanQuery.Builder();
          for (String x : groups) g.add(new TermQuery(new Term("group", x)), BooleanClause.Occur.SHOULD);
          outer.add(g.build(), BooleanClause.Occur.FILTER);
        }
        TopDocs td = s.search(outer.build(), topK);
        int rank = 1;
        for (ScoreDoc sd : td.scoreDocs) {
          Document d = s.storedFields().document(sd.doc);
          out.println(p[0] + "\t" + rank++ + "\t" + d.get("path") + "\t" + sd.score);
        }
      }
      System.out.println("ran " + qlines.size() + " queries in " + (System.nanoTime() - t0) / 1_000_000 + " ms");
    }
  }
}
