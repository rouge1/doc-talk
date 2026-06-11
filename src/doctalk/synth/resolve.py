"""synth_resolve — decide, for each extracted candidate, MATCH / NEW / DEFER.

The make-or-break stage (``docs/entity-resolution.md``). Block against a small candidate set (never
all entities), score each (mention, candidate) pair on type-gated signals, then apply a
**two-threshold band**: auto-MATCH only when the best score is high *and* well-separated from the
runner-up; auto-NEW only when clearly novel; the murky middle DEFERs — LLM-adjudicated, then queued
for a human. Bad merges are worse than fragments, so the band errs toward not-merging, and the
recovery half (``repo.merge_entities`` / ``wiki-merge``) makes every wrong call cheap to reverse.

Embedding is indirected through ``_embed`` (monkeypatched in tests) so resolution logic is testable
without a model; a missing model degrades to exact/alias blocking rather than failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from doctalk.cluster.grouping import cosine
from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import Entity
from doctalk.synth.normalize import acronym_pair, norm_key
from doctalk.vector import store


@dataclass
class Resolution:
    entity: Entity
    decision: str            # MATCH | NEW | DEFER
    score: float
    signals: dict = field(default_factory=dict)


def _embed(text: str) -> list[float] | None:
    """Embed name+definition for blocking/scoring. Best-effort: no model → None (exact/alias only)."""
    try:
        from doctalk.models.embed import embed_passages

        return embed_passages([text[:600]])[0]
    except Exception:  # noqa: BLE001 - resolution still works on lexical signals without vectors
        return None


def _types_compatible(a: str, b: str) -> bool:
    """Hard gate. Same type, or one side is the catch-all 'concept' (sources type loosely)."""
    return a == b or "concept" in (a, b)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _candidate_surfaces(e: Entity) -> set[str]:
    surf = {e.norm_key, e.name.lower().strip()}
    surf |= {a.lower().strip() for a in (e.aliases or [])}
    surf |= {a.lower().strip() for a in (e.acronyms or [])}
    return surf


def _neighbor_keys(session, entity_id: int) -> set[str]:
    keys: set[str] = set()
    for nid in repo.get_comention_entity_ids(session, entity_id):
        n = session.get(Entity, nid)
        if n is not None:
            keys.add(n.norm_key)
    return keys


def resolve_candidate(
    session,
    *,
    name: str,
    type_: str,
    aliases: list[str],
    definition: str,
    context_text: str,
    comention_keys: set[str],
) -> Resolution:
    """Resolve one extracted candidate to a canonical entity (matching, minting, or deferring)."""
    s = get_settings()
    nk = norm_key(name)
    pair = acronym_pair(name)
    acronyms = [pair[1]] if pair else []
    surfaces = [name, *aliases]
    surface_norms = {nk} | {x.lower().strip() for x in surfaces} | set(acronyms)
    types = {type_, "concept"}

    # --- block: exact/alias/acronym keys + embedding kNN -------------------
    key_set = {nk} | set(acronyms) | ({pair[0]} if pair else set())
    candidates: dict[int, Entity] = {
        e.id: e for e in repo.find_entities_by_norm_keys(session, key_set, types)
    }
    for e in repo.scan_alias_acronym_candidates(session, surface_norms, types):
        candidates[e.id] = e

    mention_vec = _embed(f"{name}. {definition or context_text}")
    if mention_vec is not None:
        for row in store.search_entity_names(mention_vec, s.resolve_block_k):
            e = session.get(Entity, row["entity_id"])
            if e is not None and e.status != "merged_into":
                candidates[e.id] = e

    cand_vectors = store.get_entity_vectors(list(candidates)) if mention_vec is not None else {}

    # --- score each candidate ---------------------------------------------
    ranked: list[tuple[float, dict, Entity]] = []
    for e in candidates.values():
        if not _types_compatible(type_, e.type):
            continue  # hard gate -> s = 0
        alias_hit = 1.0 if surface_norms & _candidate_surfaces(e) else 0.0
        lexical = _jaccard(set(nk.split()), set(e.norm_key.split()))
        cv = cand_vectors.get(e.id)
        embed = max(0.0, cosine(mention_vec, cv)) if (mention_vec and cv) else 0.0
        comention = _jaccard(comention_keys, _neighbor_keys(session, e.id))
        score = (
            s.resolve_w_alias * alias_hit
            + s.resolve_w_lexical * lexical
            + s.resolve_w_embed * embed
            + s.resolve_w_comention * comention
        )
        sig = {
            "alias": round(alias_hit, 3),
            "lexical": round(lexical, 3),
            "embed": round(embed, 3),
            "comention": round(comention, 3),
            "candidate_id": e.id,
        }
        ranked.append((min(1.0, score), sig, e))

    ranked.sort(key=lambda r: r[0], reverse=True)
    s_star = ranked[0][0] if ranked else 0.0
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    margin = s_star - second

    # --- two-threshold decision band --------------------------------------
    if ranked and s_star >= s.resolve_tau_high and margin >= s.resolve_margin:
        return _apply_match(session, ranked[0][2], surfaces, s_star, ranked[0][1])
    if s_star < s.resolve_tau_low:
        return _apply_new(session, name, type_, nk, aliases, acronyms, mention_vec,
                          ranked[0][1] if ranked else {})

    # middle band (or two strong candidates with a thin margin) -> DEFER
    return _apply_defer(
        session, name=name, type_=type_, nk=nk, aliases=aliases, acronyms=acronyms,
        definition=definition, mention_vec=mention_vec, ranked=ranked,
    )


# --- apply ------------------------------------------------------------------


def _store_vector(session, entity_id: int, type_: str, vec: list[float] | None) -> None:
    if vec is None:
        return
    store.add_entity_names([{"entity_id": entity_id, "type": type_, "vector": vec}])
    repo.set_entity_name_embedding_id(session, entity_id, entity_id)


def _apply_match(session, entity: Entity, surfaces, score, sig) -> Resolution:
    if entity.status == "pruned":  # a fresh mention re-admits a pruned entity (gate evolution)
        repo.set_entity_status(session, entity.id, "active")
    repo.add_entity_aliases(session, entity.id, surfaces)
    return Resolution(entity=entity, decision="MATCH", score=score, signals=sig)


def _existing_identity(session, name: str, type_: str) -> Entity | None:
    """The entity a create would UNIQUE-collide with, canonicalized. ``(name, type)`` IS identity
    per the schema, so a scored-low or LLM-"different" verdict cannot mint a duplicate — exact
    same-name re-extractions (the re-drop case) MATCH instead. Merged-away rows keep their name and
    still occupy the constraint, so follow the merge to the survivor; a pruned row whose name the
    gate now passes is reactivated (gate evolution re-admits it). Genuine polysemy under one
    name+type (salt the seasoning vs SALT the value) can't be two rows anyway — that's wiki-split's
    job, not a duplicate insert."""
    existing = repo.find_entity_by_name_type(session, name, type_)
    return repo.follow_merges(session, existing) if existing is not None else None


def _apply_new(session, name, type_, nk, aliases, acronyms, vec, sig) -> Resolution:
    existing = _existing_identity(session, name, type_)
    if existing is not None:
        return _apply_match(session, existing, [name, *aliases], 1.0, {**sig, "exact_name": True})
    entity = repo.create_entity(
        session, name=name, type_=type_, norm_key=nk, aliases=aliases, acronyms=acronyms
    )
    _store_vector(session, entity.id, type_, vec)
    return Resolution(entity=entity, decision="NEW", score=0.0, signals=sig)


def _apply_defer(session, *, name, type_, nk, aliases, acronyms, definition, mention_vec, ranked) -> Resolution:
    """Try the LLM on the ambiguous slice; only genuinely-hard cases reach the human queue."""
    s = get_settings()
    top = ranked[: min(3, len(ranked))]
    verdict = (
        _adjudicate(name, definition, [(c.name, c.id) for _, _, c in top], top[0][2])
        if (s.resolve_llm_adjudicate and top)
        else None
    )
    if verdict == "same":
        return _apply_match(session, top[0][2], [name, *aliases], top[0][0], {**top[0][1], "llm": "same"})
    if verdict == "different":
        return _apply_new(session, name, type_, nk, aliases, acronyms, mention_vec,
                          {**(top[0][1] if top else {}), "llm": "different"})

    # can't-tell / no LLM -> provisionally NEW tagged #unresolved + queue for a human
    existing = _existing_identity(session, name, type_)
    if existing is not None:  # same (name, type) already canonical — ambiguity is moot
        return _apply_match(
            session, existing, [name, *aliases],
            top[0][0] if top else 0.0,
            {**(top[0][1] if top else {}), "llm": verdict or "skipped", "exact_name": True},
        )
    entity = repo.create_entity(
        session, name=name, type_=type_, norm_key=nk, aliases=aliases, acronyms=acronyms,
        status="unresolved",
    )
    _store_vector(session, entity.id, type_, mention_vec)
    return Resolution(
        entity=entity, decision="DEFER",
        score=top[0][0] if top else 0.0,
        signals={"candidates": [c.id for _, _, c in top], "llm": verdict or "skipped"},
    )


def _adjudicate(name: str, definition: str, candidates: list[tuple[str, int]], best: Entity) -> str | None:
    """Ask the LLM same|different|can't-tell vs the top candidates. Best-effort; None on failure."""
    from doctalk.models.chat import chat

    try:
        opts = "; ".join(f"#{cid} {cname}" for cname, cid in candidates)
        out = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You disambiguate entity references. Answer with exactly one word: 'same' if "
                        "the mention denotes the SAME thing as the top candidate, 'different' if not, "
                        "or 'cant-tell' if the evidence is insufficient."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Mention: {name}\nDefinition: {definition}\nCandidates: {opts}\n"
                               f"Is the mention the same as '{best.name}'?",
                },
            ],
            options={"temperature": 0},
        ).strip().lower()
        if "same" in out:
            return "same"
        if "diff" in out:
            return "different"
        return "cant-tell"
    except Exception:  # noqa: BLE001 - adjudication is optional; fall through to the human queue
        return None
