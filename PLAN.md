# Local Multimodal Knowledge Base ("drop files → wiki + chat your data")

## Context

The user wants a system where you **drop heterogeneous files into a watched directory** and it
**builds a navigable wiki and lets you search + chat with your data**. The corpus is *not* audio —
it's large documents (PDFs up to ~100 MB, e.g. the 3816-page 27 MB Bluetooth Core spec in
`docs/Core_v6.0.pdf`, with an embedded TOC, internal hyperlinks, tables, figures), Word docs,
floorplans, and **a very large number of photos/images**. Docs must be chaptered with links between
chapters; figures and every image get their own wiki page; chat answers must cite sources; and search
must support **hybrid queries** mixing structured metadata with semantic content, e.g.
*"all pictures of dogs, larger than 100 kb, in png"* and *"forests between Jan 1–30 from Canada."*

**This is a NEW greenfield project, not part of bluey-ox-walker.** bluey is a Bluetooth-SDR
interception tool (SoapySDR, libbtbb, AVX2 C extensions, E0 crypto, conda `blue` env) with zero code
overlap — confirmed by exploration: no wiki/RAG/chat/report code exists in it, and it stores metadata
in JSON sidecars, not a database. The only thing worth carrying over is bluey's **design discipline**
("one source of truth, independent consumers that join only by a stable key; never process processed
data"). New project at **`/data/python/doc-talk`** (importable package **`doctalk`**).

### Decisions locked with the user
- **Fully local compute** — local VLM, OCR, text + image embeddings, chat LLM. No external APIs.
- **Describe every image** with the local VLM (not just representatives). → ingest is a **resumable
  background batch**, not instant.
- **Web app** interface (multi-user future).
- **MySQL** for metadata (user chose this for the multi-user roadmap, over SQLite).
- Docs↔images relationship is **mixed/unsure** → build cross-linking but drive it by semantic
  similarity + shared metadata, don't force it.
- **Documents are the primary corpus; photos play a supporting role** — images enrich document/entity
  pages as evidence rather than driving their own synthesis. (Earlier framing over-weighted photos.)

### Hard constraint discovered: GPU = RTX 3070 Ti Laptop, **8 GB VRAM** (20 cores, 31 GB RAM)
You cannot hold VLM + chat LLM + CLIP + embeddings resident at once. The design **serializes GPU
residency** (one model hot at a time via a GPU-lease mutex; Ollama `OLLAMA_MAX_LOADED_MODELS=1`),
runs heavy VLM work as an offline batch separate from chat serving, and provides a CPU-degrade
fallback for every model. Describe-every-image over tens of thousands of photos is a multi-hour
one-time batch per unique image — acceptable because the content-hash cache makes it pay-once.

## Architecture

**Core invariant (from bluey):** MySQL rows + content-hash are the **source of truth**; the wiki,
gallery, search, and chat are independent consumers that read it. The **vector store (LanceDB) is a
derived index, never authoritative** — a `rebuild-index` CLI can regenerate it from MySQL.

### Stack
| Role | Primary | CPU/low-VRAM fallback |
|------|---------|------------------------|
| PDF structure / tables / figures | PyMuPDF (`get_toc`, `get_links`, per-page text+images) + Docling for tables | PyMuPDF only on giant PDFs |
| docx | python-docx / Docling docx | mammoth→HTML |
| OCR (+ floorplan layout) | PaddleOCR / PP-Structure (GPU) | Tesseract |
| VLM (describe every image) | Qwen2-VL-7B via Ollama (high-value docs/floorplans) | **moondream2** (~1.8 GB, default for photo bulk) |
| Text embeddings | bge-large-en-v1.5 | bge-small |
| Image embeddings (text→image search) | open_clip ViT-H-14 | ViT-B-32 |
| Reranker | bge-reranker-v2-m3 | skip |
| Chat LLM | Llama-3.1-8B via Ollama | llama3.2:3b |
| Synthesis / entity extraction (Phase 4) | Llama-3.1-8B via Ollama (shares chat model + GPU lease) | llama3.2:3b |
| Compounding wiki store (Phase 4) | markdown **git repo** (`wiki/`, Obsidian-browsable) | — |
| Vector store (derived) | **LanceDB** (embedded, ANN + metadata prefilter) | sqlite-vec |
| Reverse geocode (GPS→country, offline) | `reverse_geocoder` | — |
| EXIF | Pillow + exifread | — |
| Watcher | watchdog (debounced, write-complete) | periodic scan backstop |
| Backend / ORM / migrations | FastAPI + SQLAlchemy 2.0 + Alembic | — |
| Job queue | RQ + Redis (concurrency=1 for GPU stages) | in-process |
| Frontend | React + Vite + TS (virtualized gallery, filter builder, chat) | Jinja for Phase 1 |

### Repo layout (abridged)
```
/data/python/doc-talk/
  CLAUDE.md  README.md  pyproject.toml  alembic/
  docs/{architecture,schema,pitfalls,models}.md
  src/doctalk/
    config.py                 # single source: paths, model names, VRAM budget, watched dirs
    hashing.py                # blake3 content-hash (idempotency key)
    db/{models,session,repo}.py   # repo.py = ONLY metadata writer
    watch/watcher.py
    ingest/dag.py             # resumable DAG runner + job-ledger cache
    ingest/stages/{identify,pdf_structure,docx_structure,image_extract,ocr,
                   vlm_describe,exif_geo,embed_text,embed_image,
                   link_internal,link_semantic,cluster,wiki_build}.py
    models/{vlm,ocr,embed,rerank,chat,gpu_lease}.py   # gpu_lease = VRAM mutex
    vector/store.py           # LanceDB (text + image tables), derived index
    query/{planner,hybrid,chat}.py
    synth/{entities,resolve,integrate,promote,lint}.py  # LLM-authored compounding wiki (Phase 4)
    api/app.py + routes/{files,wiki,gallery,search,chat,jobs}.py
    cli/                      # reingest, rebuild-index, wiki-lint, wiki-audit, wiki-bootstrap, stats
  wiki/  # Phase 4 git repo of markdown: index.md log.md overview.md entities/ concepts/ topics/ queries/
  web/   tests/fixtures/      # BT spec + sample images (gitignored)
```

### MySQL schema (see `docs/schema.md` for full DDL)
Tables: **files** (content_hash UNIQUE = re-drop is a no-op; indexes on `(format,byte_size)`,
`mime`), **chapters** (outline tree: parent_id/level/ord/page range, `source` =
outline|heading_detect), **figures** (figure|table, bbox, table_html, → image_id), **images**
(vlm_description, ocr_text, EXIF datetime, gps + `geo_country`/`geo_place`, is_floorplan, cluster_id,
embedding_id; indexes `(geo_country,exif_datetime)`, `(format,byte_size)`, FULLTEXT on
description+OCR), **chunks** (retrieval unit for chat, carries file/chapter/page for citations),
**wiki_nodes** (one per chapter/figure/image/file), **links** (kind =
internal_pdf|semantic|shared_geo|shared_time|figure_ref, score), **jobs** (resumability ledger;
UNIQUE `(content_hash, stage, input_hash)`).
LanceDB: `text_chunks{vector, file_id, chapter_id, page}` and `images{vector, file_id, geo_country,
exif_ts, format, byte_size}` — filterable scalars mirrored for ANN-with-prefilter.

**Synthesis-layer tables (Phase 4).** **entities** (canonical `name`, `type`, `aliases` JSON,
`wiki_path`, `embedding_id` for resolution; UNIQUE `(name,type)`), **wiki_pages** (`path`, `title`,
`kind` = entity|concept|topic|overview|query, `entity_id?`, `source_count`, `last_synth_at`,
`md_hash` — the markdown **body lives on disk** in the git repo, this row is just the index/catalog),
**claims** (`wiki_page_id`, `text`, `status` = active|contradicted|superseded, `confidence`),
**claim_sources** (`claim_id` → `chunk_id`/`file_id` — the provenance link that makes the wiki
auditable against truth), **mentions** (source `file_id`/`chunk_id` → `entity_id`, so a re-synth knows
which pages a source touches). The synthesis `jobs` row is keyed like every other stage
(`content_hash + 'synth' + model_version`) so a re-drop skips synthesis and a model upgrade re-runs it.

### Ingest = resumable idempotent DAG
Idempotency key = blake3 `content_hash`. Each stage writes a `jobs` row keyed by
`input_hash = blake3(content_hash + stage + model_version + params)`. Before running, check for a
`done` job with that `input_hash`: identical re-drop → all stages skipped; model upgrade → only the
affected stage + downstream re-run; crash → re-enqueue non-`done` stages. **GPU-bound stages
(vlm_describe, paddle-OCR, embed_*) acquire a GPU lease** so only one model is VRAM-resident
(concurrency=1); CPU stages run at N. Backpressure: small/active docs jump the queue, the photo
backlog drains in the background with an ETA.

### Hybrid query + chat
`planner.py` parses a query into (a) a **typed filter AST** → parameterized SQLAlchemy *(LLM never
writes raw SQL)* for `format`, `byte_size>`, date ranges, `geo_country`; and (b) a **semantic plan** —
CLIP text-tower for "dogs"/"forests" → image vectors, or bge for doc questions → chunk vectors.
`hybrid.py` pushes the filter into LanceDB as a prefilter, runs ANN within the filtered set, reranks,
joins back to MySQL. **Chat (RAG):** retrieve → rerank → assemble context with inline provenance tags
`{file, chapter, page}` → Llama-3.1-8B answers citing those tags → citations render as wiki-node
links. Cross-linking: exact PDF internal links + semantic-similarity links across docs/images.

### Synthesis layer — the compounding wiki (Phase 4)
Everything above builds the **library** (cataloged, indexed, searchable). The synthesis layer adds the
**scholar**: an LLM-authored, *compounding* wiki of markdown pages that integrates each new source into a
persistent, interlinked body of knowledge — the actual thesis of the LLM-wiki pattern (*knowledge
compiled once and kept current, not re-derived on every query*). **Don't confuse it with Phase-1
`wiki_build`**, which emits deterministic **navigation nodes** (one per chapter/figure/image);
the synthesis layer emits **authored prose pages** (entities, concepts, topics) that get *rewritten* as
sources arrive. Documents drive it; a relevant photo attaches to an existing page as evidence, and bulk
photos never spawn their own synthesis pages.

**A second source of truth, durable via git.** This deliberately breaks the "everything but MySQL is
derived" invariant: the wiki embodies accumulated reasoning and human edits, so it is **not** cheaply
regenerable like LanceDB. It lives as a **git repo of markdown** (`wiki/`, browsable in Obsidian),
**one commit per ingested source** so the knowledge has a version history. Every claim carries
provenance down to MySQL chunks (`claim_sources`), so the wiki stays *auditable* against truth even
though it isn't regenerated from it. `rebuild-index` still rebuilds LanceDB and never touches the wiki;
a separate, last-resort `wiki-bootstrap` can cold-build pages from MySQL (lossy — it discards human
edits and accumulated framing).

**Incremental integration (the core op).** When a source finishes extraction, a background **synthesis
pass** runs the chat LLM under the existing GPU lease, **concurrency=1** (it mutates shared pages):
1. `synth_entities` — read the source's summary + chunks, emit a schema-validated list of entities +
   claims (LLM forced to structured output; never freehand SQL/markdown here).
2. `synth_resolve` — match each entity against the `entities` table (fuzzy + embedding); decide
   new-vs-existing so the same entity never gets two pages; ambiguous matches defer to a review queue.
3. `synth_integrate` — for each affected page, read the *current page + the new chunks* and **rewrite
   the page to integrate** the new information, add `[[wikilinks]]`, and **flag contradictions** against
   prior `claims` (citing both sources). Then update `index.md` (content catalog) and append `log.md`
   (`## [YYYY-MM-DD] ingest | <title>` — grep-parseable), and **commit to git**.
A single source touches ~10–15 pages, exactly as the pattern describes. Synthesis drains in the
background like the photo backlog (shared GPU lease, no new resident model → the 8 GB wall is unchanged).

**Query answers compound.** Cross-document chat is answered **from the wiki pages first** (read
`index.md` → drill into pages), falling back to chunk-RAG only to fill gaps — the synthesized wiki, not
raw retrieval, is the primary substrate. Good answers are offered back via `synth/promote`: a synthesis
you asked for is filed as `wiki/queries/<slug>.md`, linked from the index, so explorations accumulate
instead of vanishing into chat history.

**Lint.** `doctalk wiki-lint` (periodic, not per-ingest) is an agentic health-check: unresolved
contradictions, stale/superseded claims, **orphan pages** (no inbound `[[links]]`), entities mentioned
≥N times with no page, missing cross-refs, and **unsupported claims** (no `claim_sources`). It reports,
suggests new questions/sources to chase, and auto-fixes the mechanical items. `doctalk wiki-audit`
checks every claim's cited chunk still exists (catches wiki↔truth drift).

## Phased build plan

- **Phase 0 — skeleton & truth store.** pyproject, config, Alembic/MySQL up, `db/models.py`,
  `hashing.py`, `jobs` ledger, `dag.py` with idempotency check.
  *Verify:* run DAG twice on one file → 2nd run all-`skipped`; kill mid-run → resumes.
- **Phase 1 — prototype on `docs/Core_v6.0.pdf` + one photo folder (the proof).**
  PyMuPDF outline→chapter tree, internal links resolve, Docling tables/figures, per-chapter chunks;
  images get EXIF+geo, CLIP embed, moondream description, OCR; LanceDB text+image tables;
  `hybrid.py` answers the two canonical image queries; minimal UI (browse chapters + gallery + one
  search box + one chat box). *Verify:* BT-spec outline is navigable; a real cross-reference resolves;
  "section on E0 encryption" chat answer cites a real page/chapter; "dogs >100 kb png" and "forests
  Canada Jan 1–30" return correct images; re-drop = no reprocessing.
- **Phase 2 — generalize & harden.** docx; heading-detection fallback for outline-less PDFs;
  floorplan sub-type (PP-Structure); semantic cross-linking + image clustering/dedup; reranker;
  Qwen2-VL for high-value images; GPU-lease tuning; backlog backpressure + ETA.
  *Verify:* mixed corpus ingests without OOM; a photo semantically links to a relevant doc figure;
  near-duplicate photos collapse into clusters.
- **Phase 3 — multi-user web app.** React frontend (virtualized gallery, filter builder, wiki
  browser, chat with citation cards), users/auth, per-user views, job dashboard, admin CLIs.
  *Verify:* two users browse concurrently; chat citations deep-link to wiki nodes.
- **Phase 4 — the compounding wiki (synthesis layer; closes the gap with the LLM-wiki vision).**
  `wiki/` git repo + page conventions; `entities`/`wiki_pages`/`claims`/`claim_sources`/`mentions`
  tables; the `synth_entities`→`synth_resolve`→`synth_integrate` background pass (chat LLM, GPU-lease,
  **entity resolution — the make-or-break sub-stage — is spec'd in `docs/entity-resolution.md`**;
  concurrency=1, one git commit per source); contradiction flagging with provenance; `index.md` +
  `log.md` maintenance; wiki-first chat with answer-file-back to `wiki/queries/`; `doctalk wiki-lint`
  and `wiki-audit`. *Verify:* ingest source A → entity/concept pages created, index+log updated, one
  git commit; ingest source B that contradicts A → the page shows a flagged contradiction citing both,
  log records it; re-drop A → synthesis skipped (ledger), no spurious commit; a question answerable only
  by synthesizing ≥3 docs is answered **from wiki pages** (not raw chunks) with page citations, and
  filing it creates a linked `wiki/queries/` page; `wiki-lint` surfaces a real orphan + a missing-page
  suggestion + an unsupported claim; a relevant photo attaches to an entity page as supporting evidence
  while bulk photos spawn no pages; `wiki/` opens in Obsidian with a sane graph view.

## Key risks
- **8 GB VRAM wall** — mitigated by the GPU-lease mutex, one Ollama model loaded at a time, and the
  small-model fallback path (moondream / bge-small / ViT-B). Document the VRAM budget in
  `docs/models.md`.
- **Describe-every-image throughput** — dominant cost; pay-once via the hash cache, surface ETA,
  cluster *after* describing (navigation, not a cost gate).
- **Giant-PDF memory** — stream per-page; PyMuPDF-only structure on huge PDFs, Docling reserved for
  table-rich pages.
- **Floorplan/table accuracy** — keep raw OCR text alongside VLM descriptions so search has a fallback
  signal.
- **Vector/metadata drift** — MySQL wins; `rebuild-index` CLI regenerates LanceDB from truth.
- **Watchdog on large/slow/network copies** — write-completion detection + periodic reconciliation
  scan backstop.
- **Synthesis hallucination / drift (Phase 4)** — every claim must carry a `claim_sources` provenance
  link; `wiki-lint` flags unsupported claims and `wiki-audit` checks cited chunks still exist, so the
  wiki stays auditable against MySQL even though it isn't regenerated from it.
- **Synthesis mutates shared state non-deterministically (Phase 4)** — the synthesis queue is
  concurrency=1 and transactional: one git commit per source means every pass is diffable and
  revertable; the `jobs` ledger prevents redundant re-synthesis on re-drop.
- **Entity-resolution errors (Phase 4)** — duplicate or wrongly-merged pages: fuzzy + embedding match
  with a confidence threshold, ambiguous matches deferred to a review queue, and `wiki-lint` detects
  near-duplicate pages.

## Verification (end-to-end)
1. `doctalk ingest <dir>` twice → second run is fully cached/skipped (job ledger proves idempotency).
2. Ingest `docs/Core_v6.0.pdf` → chapter tree matches the PDF's real outline; an internal hyperlink
   resolves to the right chapter; a table renders.
3. Drop a photo folder with EXIF/GPS → the two canonical hybrid queries return correct, filtered,
   semantically-ranked images.
4. Chat: ask a question answerable from the spec → answer cites page + chapter and the citation links
   to the wiki node; ask something not in corpus → it declines rather than fabricates.
5. Monitor `nvidia-smi` during a mixed ingest → never more than one large model resident; no OOM.
6. `doctalk rebuild-index` → LanceDB regenerated from MySQL; queries still pass.
7. **(Phase 4)** Ingest two sources where the second contradicts the first → the affected wiki page shows
   a flagged, provenance-linked contradiction; `git log wiki/` shows one commit per source.
8. **(Phase 4)** Ask a question answerable only by synthesizing ≥3 documents → the answer is built from
   wiki pages (index→pages), cites them, and can be filed back as a `wiki/queries/` page linked from
   `index.md`; ask again later → the filed page is reused, not re-derived.
9. **(Phase 4)** `doctalk wiki-lint` on a partially-built wiki → reports orphans, missing pages, and
   unsupported claims; the mechanical fixes apply cleanly. `wiki-audit` finds no dangling claim sources.
