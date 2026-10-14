# Chunk editor

Search quality problems are usually a few specific chunks: an OCR slip in a part number, a mangled table row, a disclaimer that
matches every question, an answer that exists only in somebody's head. The chunk editor lets an administrator look at what the
index holds for a document and fix those, without re-uploading anything.

## What you can do

* **Correct a chunk's text.** It is re-embedded and re-indexed. "Restore original" undoes it.
* **Add keywords and questions** to a chunk (words people would search with that the text does not contain). They are indexed on
  the keyword side and embedded with the chunk.
* **Switch a chunk off** (boilerplate repeated on every page). It stops being found; nothing is deleted and it can be switched on again.
* **Add a chunk by hand** (an FAQ answer, a note) and delete it again. It belongs to the document and carries the document's access rules.
* **Try a search** and see the winning chunks with their scores (meaning score, word score, final score, kind).

Where: the **Chunks** page in the sidebar (administrators only), or the API:

```
GET    /api/scale/documents/{id}/chunks?offset=&limit=&q=&kind=&unavailable=     any user who may read the document
GET    /api/scale/documents/{id}/chunks/{n}
PATCH  /api/scale/documents/{id}/chunks/{n}      {text, keywords, questions, available}     admin
POST   /api/scale/documents/{id}/chunks          {text, page, keywords, questions}          admin
DELETE /api/scale/documents/{id}/chunks/{n}      hand-written chunks only                   admin
GET    /api/scale/documents/{id}/chunk-history   who changed what                           admin
GET    /api/scale/retrieval-test?q=&k=&rerank=   a search with every number shown
```

The frontend page is `frontend/src/components/admin/ChunkEditorView.tsx` (the **Chunks** entry in the sidebar, administrators only).
It is type-checked and builds; it has not been clicked through in a browser against a library with real edits, so look at it once
before showing the client.

## What an edit changes, and what it does not

* It changes **what search finds** and the **text of the hit** that search shows.
* It does **not** change the file, its stored pages, the page preview or the page citations. The agent's full-page read still
  shows the original page. (With `SCALE_CHUNK_EDIT_OVERLAY=true`, the default, the agent's keyword-search tool does show the
  corrected text.)
* Every change is recorded in `chunk_edit_log` (who, when, before and after).

## What happens when the file changes

Documents are content-addressed, so a changed file is a new document. The editor follows each edit by a hash of the chunk's
**original** text and carries it over:

* the chunk text is unchanged, even if it moved: the edit is re-applied;
* the chunk text changed: the edit is kept but marked **orphaned**, is not applied (it would be editing text that is gone), and
  the page lists it so a person can redo it;
* hand-written chunks are re-applied to the new version.

## Access control

Chunk lists and chunk reads check that the caller may read the document. Edits require the admin role. A hand-written chunk gets
the document's access list like any other chunk and follows it when the list changes. Note that an administrator can read the
text of any chunk of a document they open; that is the same as being able to open the document.

## Tests

`tests/scale/test_chunk_edit.py`: edit, restore, switch off, add and delete, carry-over on a changed file, orphaning, access,
history, and that a switched-off chunk is not found. These prove the mechanics. Whether editing chunks improves answers is
entirely up to what is edited; no accuracy gain is claimed.
