# Entity resolution (`synth_resolve`) — design

The make-or-break stage of the Phase 4 synthesis layer (see `../PLAN.md`). Given the candidate
mentions `synth_entities` extracted from one source, decide for each whether it refers to an
**existing** entity (link), is **new** (create a page), or is **ambiguous** (escalate).

**Framing that drives every decision below:** you will never tune the thresholds perfectly, so the
real goal is to make every wrong decision *cheap to reverse*. Resolution is two halves — *deciding*
(most of this doc) and *recovering* (`merge`/`split` as first-class ops). Get recovery right and the
decision half is allowed to be imperfect.

## Inputs / outputs

Input — per candidate mention from `synth_entities`:
`{surface_form, type, definition_sentence?, context_chunks[], co_mentions[], salience}`.

Output — per candidate: a decision **MATCH**(existing `entity_id`) · **NEW**(create) ·
**DEFER**(escalate), plus the scores and signals that drove it, logged for audit.

Two failure modes it exists to prevent:
- **Fragmentation (over-split):** "E0 cipher" / "E0 encryption" / "the E0 stream cipher" → three pages.
- **Conflation (over-merge):** "LE" (Bluetooth Low Energy radio) and an unrelated "LE" collapse into
  one page; "Link Manager" and "Link Layer" get merged.

## Pipeline

### (0) Normalize → `norm_key`
NFKC, lowercase, collapse whitespace; strip generic qualifiers ("the … procedure", "… process") **but
keep them as aliases**. Detect `Foo Bar (FB)` definitional patterns and populate a bidirectional
acronym map. High-value for specs, which literally define their acronyms.

### (1) Block — never compare against all entities
Generate a small candidate set per mention via cheap keys, all gated to **compatible types**:
- exact `norm_key` or alias hit (strong);
- acronym ↔ expansion hit;
- embedding kNN — embed name+definition, top-10 against an `entities` name-embedding vector table (a new
  small LanceDB table);
- token-set Jaccard / trigram overlap for fuzzy surfaces.

### (2) Score each (mention, candidate) pair → `s ∈ [0,1]`
Starting weights (tune against the gold set — see below):

| Signal | Why it matters | ~weight |
|--------|----------------|---------|
| **Type agreement** | hard gate: incompatible type → `s=0` | gate |
| Alias / acronym exact hit | near-certain same | 0.35 |
| Name lexical sim (edit / Jaccard) | catches surface variants | 0.15 |
| Name embedding cosine | semantic near-synonyms | 0.20 |
| **Context embedding cosine** | mention's chunks vs the entity page's definition — *the polysemy disambiguator* | 0.20 |
| **Co-mention overlap** | Jaccard of this mention's neighbors vs the entity's existing `[[links]]` | 0.10 |

Context cosine + co-mention overlap are what separate two "LE"s: the BLE one co-occurs with
*advertising / GATT / connection-interval*; the other doesn't. Start hand-set; graduate to a
logistic-regression scorer once the review queue has produced labels.

### (3) Decide with a two-threshold band
Let `s*` = best score, `δ` = margin over 2nd-best:
- `s* ≥ τ_high (0.85)` **and** `δ ≥ 0.15` → **MATCH** (auto-link)
- `s* < τ_low (0.45)` → **NEW**
- otherwise (middle band, *or* two existing entities both above `τ_high` with small margin) → **DEFER**

The band is the point: confident merges only when high *and* well-separated; confident-new only when
clearly novel; the murky middle is **escalated, never guessed**.

### (3b) Adjudicate DEFERs with the LLM before bothering a human
Hand the chat LLM the mention (name + definition + context snippet) and the top-2/3 candidate page
summaries; ask a structured `same | different | can't-tell` with a forced citation of the distinguishing
evidence. Only `can't-tell` falls through to the human `entity_review` queue. Targeted call on the
ambiguous slice only — not every pair — runs under the existing GPU lease in the concurrency=1 synth
queue. Tiering: cheap signals → LLM on the ambiguous slice → human on the genuinely hard slice.

### (4) Apply & record
- **MATCH** → insert `mentions` row; add surface to `entities.aliases` if new.
- **NEW** → create `entities` + `wiki_pages` + stub page; embed the name for future blocking.
- **DEFER-unresolved** → provisionally create as NEW but tag `#unresolved` so `wiki-lint` surfaces it and
  a later merge is one cheap op.
- Every decision logs `{score, signals, decision}` — auditable, and becomes training data for the
  learned scorer.

## The recovery half (what makes imperfect `τ` safe)

- **Merge(a→b):** repoint `mentions` / `claims` / `claim_sources` a→b, replace a's page with an Obsidian
  redirect/alias, one git commit, idempotent. Triggered by `doctalk wiki-merge` or surfaced by
  `wiki-lint`'s near-duplicate detector.
- **Split(entity, criterion):** create the new entity, move the claims whose `claim_sources` match the
  criterion. Harder → human-confirmed.
- `entity_merges` (from_id, into_id, reason, committed_sha) keeps it reversible and auditable.

## Two domain-specific wins for this corpus

1. **Seed from the glossary first.** Specs like `docs/Core_v6.0.pdf` have an "Acronyms and
   Abbreviations" / definitions section. Parse it *before* synthesizing prose and pre-seed `entities`
   with authoritative name+acronym pairs. Resolution precision jumps because the canonical forms already
   exist as match targets.
2. **Photos resolve, never create.** A photo matches its VLM description / OCR / geo against existing
   entity pages by embedding similarity; above threshold → attach as evidence/media; below → attach to
   nothing. Photos can't mint entities — keeps them supporting, per the locked decision in `../PLAN.md`.

## Idempotency caveat (stated honestly)

`synth_resolve` is **not** a pure function of the source — it depends on the current `entities` state, so
it is order-dependent. Handle by: running serialized in the concurrency=1 synth queue; keying the `jobs`
row on `content_hash + 'synth_resolve' + model_version + embed_version` so re-drop skips and an
embed-model upgrade re-blocks; and making re-runs produce **merges** (reversible) rather than clobbering
pages.

## Tuning (you can't tune `τ` blind)

Hand-label ~200 mention/entity pairs as same/different → a gold set. Report MATCH precision/recall across
`τ`; pick `τ_high` for high precision (bad merges are worse than fragments), `τ_low` for high
new-recall. Headline metrics:
- **fragmentation rate** — pages per true entity → want ~1;
- **conflation rate** — pages drawing claims from ≥2 true entities → want 0.

The review queue's human verdicts feed the gold set continuously.

## Schema deltas (on top of the Phase 4 tables in `../PLAN.md`)

- `entities` += `norm_key`, `acronyms` JSON, `glossary_defined` bool, `name_embedding_id`, `status`
  (active | unresolved | merged_into).
- `entity_review` queue: mention payload, candidate ids + scores, llm_verdict, human_verdict, state.
- `entity_merges`: from_id, into_id, reason, committed_sha.
- `mentions` += `score`, `decision`, `signals` JSON.

## Verify

- Glossary-seeded run: acronyms from the spec's definitions section resolve to a single canonical page
  (no "L2CAP" vs "Logical Link Control and Adaptation Protocol" split).
- Polysemy: two same-surface mentions in different contexts (e.g. distinct "LE" senses) do **not** merge.
- Ambiguous mention → lands in `entity_review`, not auto-merged; LLM adjudication resolves the easy ones.
- A wrong merge is reversible: `wiki-merge`/split repoints mentions+claims and commits, with no orphaned
  `claim_sources` (confirmed by `wiki-audit`).
- Fragmentation/conflation rates computed against the gold set stay within target as the corpus grows.
