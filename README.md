# doctalk

**Drop files → get a wiki you can search and chat with.** A fully-local knowledge base: drop
heterogeneous files (large PDFs, Word docs, images) into a watched directory and doctalk builds a
navigable, interlinked wiki, supports hybrid metadata + semantic search, and answers questions with
citations back to the source page. No external APIs — every model runs on your machine.

> **Status: planning / greenfield.** No code yet. The design is complete and the build is phased.
> Start with [`PLAN.md`](PLAN.md).

## Why it's different

Most "chat with your documents" tools are RAG: they retrieve raw chunks at query time and re-derive an
answer from scratch every question — nothing accumulates. doctalk also maintains a **compounding wiki**:
as each source is ingested, the system integrates it into persistent, interlinked pages — updating
entity and concept pages, flagging contradictions, keeping cross-references current. Knowledge is
compiled once and kept up to date, not rebuilt on every query. The idea comes from the LLM-wiki pattern
([`llm-wiki.md`](llm-wiki.md)); doctalk extends it to a large, multimodal, document-first corpus.

## What it does

- **Chapters big PDFs** (e.g. a 3,800-page spec) into a navigable outline with working internal links;
  extracts tables and figures.
- **Describes every image** with a local vision model; pulls EXIF/GPS, OCR text, and embeddings.
- **Hybrid search** mixing structured filters with semantic content — e.g. *"all pictures of dogs,
  larger than 100 KB, in PNG"* or *"forests between Jan 1–30 from Canada."*
- **Chat with citations** — answers cite the exact file, chapter, and page, and link to the wiki node.
- **A compounding wiki** — authored entity/concept pages that get richer with every source (Phase 4).

Documents are the primary corpus; photos play a supporting role.

## How it works (high level)

- **Source of truth:** MySQL rows keyed by a content hash. The wiki, gallery, search, and chat are
  independent consumers; the vector index (LanceDB) is derived and rebuildable.
- **Ingest:** a resumable, idempotent pipeline — re-dropping a file is a no-op; a model upgrade only
  re-runs the affected step.
- **Local + GPU-aware:** designed for an 8 GB GPU, so it keeps one model resident at a time and runs
  heavy vision work as a background batch.
- **The compounding wiki** lives as a git repo of markdown, browsable in Obsidian.

Full architecture, schema, and the phased build plan are in [`PLAN.md`](PLAN.md).

## Documentation

- [`PLAN.md`](PLAN.md) — architecture, stack, MySQL/LanceDB schema, phased build plan, risks.
- [`docs/entity-resolution.md`](docs/entity-resolution.md) — design for the wiki's entity-resolution stage.
- [`llm-wiki.md`](llm-wiki.md) — the originating LLM-wiki vision.
- [`CLAUDE.md`](CLAUDE.md) — conventions and invariants for contributors (human or LLM).

## Getting started

_Not yet runnable — to be written once Phase 0–1 land._ Will require a local MySQL, Redis, and Ollama
(`OLLAMA_MAX_LOADED_MODELS=1`), plus a Python environment for the `doctalk` package.

## License

TBD.
