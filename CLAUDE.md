# CLAUDE.md — doctalk

Orientation for any session working in this repo. Keep it lean; expand the **Commands** section once
Phase 0–1 land real code (re-run `/init` then to capture build/test invocations). The
**Wiki conventions** section is load-bearing for Phase 4 — it is the synthesis-layer *schema* in the
sense of the LLM-wiki pattern (`llm-wiki.md`): it's what makes a session a disciplined wiki maintainer
rather than a generic chatbot.

## What this is

**doctalk** — a fully-local "drop files → wiki + chat your data" knowledge base. You drop heterogeneous
files (large PDFs, docx, images) into a watched dir; it builds a navigable wiki, supports hybrid
metadata+semantic search, and answers chat questions with citations. **Documents are primary; photos
play a supporting role.** Greenfield; importable package is `doctalk`. Full design lives in `PLAN.md`.

- `PLAN.md` — the architecture and phased build plan. Read this first.
- `docs/entity-resolution.md` — spec for `synth_resolve`, the make-or-break Phase 4 sub-stage.
- `llm-wiki.md` — the originating vision (Karpathy's LLM-wiki pattern). Phase 4 exists to honor it.

## Core invariants (carried from bluey's design discipline)

- **One source of truth.** MySQL rows + the blake3 `content_hash` are authoritative. The wiki nav nodes,
  gallery, search, and chat are **independent consumers** that read it and join only by stable key.
- **`db/repo.py` is the ONLY metadata writer.** Everything else reads. Don't write metadata elsewhere.
- **Never process processed data.** Re-derive from the source + truth store, not from another consumer's
  output.
- **LanceDB is a derived index, never authoritative.** `rebuild-index` regenerates it from MySQL.
- **Ingest is a resumable, idempotent DAG.** Each stage writes a `jobs` row keyed by
  `input_hash = blake3(content_hash + stage + model_version + params)`; a `done` row → skip. Identical
  re-drop is a no-op; a model upgrade re-runs only the affected stage + downstream.

## Hard constraints

- **Fully local compute.** Local VLM, OCR, embeddings, chat LLM. **No external APIs.**
- **8 GB VRAM wall** (RTX 3070 Ti Laptop). Cannot hold VLM + chat + CLIP + embeddings resident at once.
  GPU-bound stages acquire a **GPU lease** (one model resident; `OLLAMA_MAX_LOADED_MODELS=1`); every
  model has a CPU/low-VRAM fallback. Heavy VLM work runs as an offline batch, separate from chat serving.
- **MySQL** for metadata (chosen for the multi-user roadmap). SQLAlchemy 2.0 + Alembic for migrations.
- The LLM **never writes raw SQL** — queries go through the typed filter AST → parameterized SQLAlchemy.

## Exception to "everything but MySQL is derived" — the Phase 4 wiki

The synthesis wiki (`wiki/`) is a **second source of truth**, durable via **git** (one commit per
ingested source), because it embodies accumulated reasoning + human edits and is *not* cheaply
regenerable. It stays auditable against MySQL because every claim carries a `claim_sources` provenance
link down to chunks. `rebuild-index` never touches `wiki/`; only the last-resort `wiki-bootstrap` does
(lossy). See `PLAN.md` → "Synthesis layer".

## Commands

**Env (Phase 0).** `conda create -y -n doctalk python=3.12 pip && conda activate doctalk` then
`pip install -e ".[dev]"` from the repo root. (Phase 1+ also needs Ollama with
`OLLAMA_MAX_LOADED_MODELS=1`, MySQL, and Redis up.)

**Truth store.** Config is env-driven with the `DOCTALK_` prefix; `DOCTALK_DB_URL` selects the
database. Production = MySQL (the default URL); set `DOCTALK_DB_URL="sqlite:///$PWD/dev.db"` to run
without a MySQL server (also what the test suite uses). `alembic upgrade head` creates/migrates the
schema (single source of the URL: `alembic/env.py` reads `config.get_settings()`).

**Now (Phase 0):**
- `doctalk initdb` — create tables from the models (dev shortcut; prefer `alembic upgrade head`).
- `doctalk ingest <file>` — hash → upsert the `files` row → run the resumable DAG; re-drop is a no-op.
- `doctalk stats` — file count + job counts by status.
- `pytest -q` — Phase 0 verification (DAG idempotency + crash/resume), runs on a temp SQLite db.

**Wiki maintenance (Phase 4):** `doctalk wiki-lint [--fix]` · `wiki-audit` · `wiki-merge` ·
`wiki-prune [--dry-run]` (drop noise entities that predate the extraction gate — reversible).

**Planned:** `doctalk wiki-bootstrap` (Phase 4, last-resort).

**Lint/type:** `ruff check .` · `mypy src` (dev extras). After Phase 1 lands real stages, re-run
`/init` to refresh this section.

## Wiki conventions (Phase 4) — the synthesis schema

> Stub. Fill in as the synthesis layer is built; this section is the contract every session follows when
> maintaining the wiki, so keep it precise and current.

- **Two distinct "wikis", don't conflate them:** Phase-1 `wiki_build` emits deterministic **navigation
  nodes** (one per chapter/figure/image, regenerable from MySQL). The Phase-4 synthesis layer emits
  **authored prose pages** (entities/concepts/topics) that are *rewritten* as sources arrive and live in
  the `wiki/` git repo.
- **Layout:** `wiki/{index.md, log.md, overview.md, entities/, concepts/, topics/, queries/}`.
- **`index.md`** — content catalog, updated every ingest. **`log.md`** — append-only, one line per event
  prefixed `## [YYYY-MM-DD] <op> | <title>` so it stays grep-parseable.
- **`overview.md` is revised, never regenerated.** Each ingest feeds the LLM the *previous* overview +
  a digest of the new source and asks for an editorial revision (`synth.overview`) — the per-ingest
  git diff shows the thesis evolving. The one page whose prior text is an input to its next version.
- **Topic pages synthesize above the entity level.** The `synth_topics` stage writes one prose page per
  entity-rich top-level chapter (the outline gives clustering for free), authored ONLY from member
  entities' claims and wikilinked to them (`## Drawn from`) — provenance chains through entity pages
  down to chunks. Slugs are file-stem-prefixed; LLM calls capped per source, skips reported.
- **Links:** `[[wikilink]]` style (Obsidian-browsable). Maintain inbound links; orphans are a lint smell.
- **Provenance is mandatory:** every claim links to its source chunk(s) via `claim_sources`. No
  unsupported claims (lint flags them).
- **Contradictions are flagged, not silently overwritten** — cite both the old and new source.
- **Query answers compound:** a good synthesis is filed back to `wiki/queries/<slug>.md` and linked from
  the index, not left in chat history.
- **Documents drive synthesis; photos support.** A relevant photo attaches to an existing entity page as
  evidence; bulk photos never spawn their own synthesis pages.
- **Data values are not entities.** Numeric/hex literals, measurements, and document self-references
  ("0x0009", "350 ms", "Section 2.3") never get pages: `synth.gate.is_pageworthy` rejects them at
  extraction, a salience gate drops one-window one-claim candidates from large sweeps, and
  `wiki-prune` removes pre-gate noise retroactively (reversible: `status='pruned'`, claims kept).
- **Entity resolution** (the `[[same entity]]` decisions) follows `docs/entity-resolution.md`:
  normalize → block → score → two-threshold band (auto-match / new / defer), LLM-adjudicate the
  ambiguous slice, human only on `can't-tell`. Merges/splits are reversible (`entity_merges`); prefer
  fragmentation over conflation when uncertain.

## House style

- Match the surrounding code's idioms, naming, and comment density.
- Prefer the project's stable keys (`content_hash`, ids) for joins across consumers.
- When in doubt about a design decision, check `PLAN.md`'s "Decisions locked with the user" before
  introducing a new direction.
