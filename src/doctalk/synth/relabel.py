"""wiki-relabel — strip leaked transaction-ID row labels from already-synthesized entity names.

Extraction now strips ``"T_ID <n> - <subject>"`` labels at the source (``normalize.strip_row_label``),
but a spec swept before that fix left malformed twins in the store: a test-vector table minted
"T_ID 5 - RTT AA candidates" beside the real "RTT AA candidates". Re-extracting the whole spec costs
hundreds of GPU calls; this sweep repairs the store directly and reversibly, by disposition:

- **fold** — the stripped name keys to an existing entity: fold the labeled twin into it
  (``merge.apply_merge``; claims repointed, redirect stub, undoable by the wiki-commit sha).
- **rename** — no twin exists: rewrite the name + key in place and move the page to the clean slug.
- **prune** — the label *was* the whole name ("T_ID 5"): a bare table coordinate with no subject,
  pruned like any gate-failing entity (``status='pruned'``, claims kept).

Read the disposition with ``plan_relabel``; apply it with ``apply_relabel``. The caller regenerates
the index and writes one wiki commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from doctalk.db import repo
from doctalk.db.models import Entity
from doctalk.synth import merge, pages, wikirepo
from doctalk.synth.normalize import norm_key, strip_row_label
from doctalk.vector import store as vstore


@dataclass
class Relabel:
    """One planned repair. ``clean`` is the name after the label is stripped ("" → a bare label)."""

    entity_id: int
    raw: str
    clean: str
    action: str  # "fold" | "rename" | "prune"
    into_id: int | None = None
    into_name: str | None = None


def _richest(session, entities: list[Entity]) -> Entity:
    """The fold survivor: most claims, tie → source_count, tie → lowest id (the clean twin wins, since
    a labeled twin is the thin offshoot of the real subject)."""
    return max(entities, key=lambda e: (merge._claim_count(session, e.id), e.source_count, -e.id))


def plan_relabel(session) -> list[Relabel]:
    """Classify every active/unresolved entity whose name carries a strippable row label. Read-only.

    A labeled twin folds into an existing entity already keyed to its clean form; absent one, labeled
    siblings stripping to the same key converge on their richest member (a rename, the others fold in);
    a bare label is pruned."""
    rows = list(session.scalars(select(Entity).where(Entity.status.in_(("active", "unresolved")))))
    by_key: dict[str, list[Entity]] = {}
    for e in rows:
        by_key.setdefault(e.norm_key, []).append(e)

    labeled = [(e, strip_row_label(e.name)) for e in rows if strip_row_label(e.name) != e.name]
    siblings: dict[str, list[Entity]] = {}
    for e, clean in labeled:
        if clean:
            siblings.setdefault(norm_key(clean), []).append(e)

    plan: list[Relabel] = []
    for e, clean in labeled:
        if not clean:
            plan.append(Relabel(e.id, e.name, "", "prune"))
            continue
        ck = norm_key(clean)
        existing = [t for t in by_key.get(ck, []) if t.status == "active"]
        if existing:  # a real, already-clean entity under this key is the fold target
            twin = _richest(session, existing)
            plan.append(Relabel(e.id, e.name, clean, "fold", twin.id, twin.name))
            continue
        # no clean twin: the richest labeled sibling is renamed, any others fold into it
        target = _richest(session, siblings.get(ck, [e]))
        if e.id == target.id:
            plan.append(Relabel(e.id, e.name, clean, "rename"))
        else:
            plan.append(Relabel(e.id, e.name, clean, "fold", target.id, strip_row_label(target.name)))
    return plan


def _prune_one(session, entity: Entity, wiki_dir: Path) -> None:
    """Prune a bare-label husk, mirroring ``prune.prune``: drop the page file + catalog row (only when
    that row still belongs to this entity — the slug-collision guard), then ``status='pruned'``."""
    if entity.wiki_path:
        page = repo.get_wiki_page_by_path(session, entity.wiki_path)
        if page is not None and page.entity_id == entity.id:
            (wiki_dir / entity.wiki_path).unlink(missing_ok=True)
            repo.delete_wiki_page(session, entity.wiki_path)
    repo.prune_entity(session, entity.id)
    vstore.delete_entity_name(entity.id)


def _rename(session, entity: Entity, clean: str, wiki_dir: Path) -> None:
    """Rewrite the name + key in place, move the page to the new slug, re-embed the clean name."""
    from doctalk.db.models import utcnow
    from doctalk.synth.resolve import _embed, _store_vector

    old_path = entity.wiki_path
    repo.rename_entity(session, entity.id, name=clean, norm_key=norm_key(clean))
    entity = session.get(Entity, entity.id)
    new_path = f"entities/{pages.slug_for(entity)}.md"
    md_hash = wikirepo.write_page(new_path, pages.render_entity_page(session, entity))
    repo.upsert_wiki_page(
        session, path=new_path, title=entity.name, kind="entity", entity_id=entity.id,
        source_count=entity.source_count, last_synth_at=utcnow(), md_hash=md_hash,
    )
    repo.set_entity_wiki_path(session, entity.id, new_path)
    if old_path and old_path != new_path:  # the slug moved — retire the stale file + catalog row
        page = repo.get_wiki_page_by_path(session, old_path)
        if page is not None and page.entity_id == entity.id:
            (wiki_dir / old_path).unlink(missing_ok=True)
            repo.delete_wiki_page(session, old_path)
    _store_vector(session, entity.id, entity.type, _embed(clean))


# Finalize rename targets before folding into them, so a fold writes the survivor page at its clean slug.
_ORDER = {"rename": 0, "fold": 1, "prune": 2}


def apply_relabel(session, wiki_dir: Path) -> dict:
    """Apply the plan: fold labeled twins, rename the twinless, prune the husks; regenerate the index.
    Returns ``{folds, renames, prunes, merge_ids}`` — the caller commits the wiki and stamps the sha
    onto ``merge_ids`` so the folds undo by that handle."""
    folds: list[tuple[str, str]] = []
    renames: list[tuple[str, str]] = []
    prunes: list[str] = []
    merge_ids: list[int] = []

    for r in sorted(plan_relabel(session), key=lambda r: _ORDER[r.action]):
        e = session.get(Entity, r.entity_id)
        if e is None or e.status not in ("active", "unresolved"):
            continue  # already repaired by an earlier step (a sibling fold/rename)
        if r.action == "fold":
            twin = session.get(Entity, r.into_id) if r.into_id else None
            if twin is None or twin.status != "active":
                continue
            merge_ids.append(merge.apply_merge(session, e, twin, reason="row-label: fold labeled twin"))
            folds.append((r.raw, twin.name))
        elif r.action == "rename":
            _rename(session, e, r.clean, wiki_dir)
            renames.append((r.raw, r.clean))
        else:
            _prune_one(session, e, wiki_dir)
            prunes.append(r.raw)

    if folds or renames or prunes:
        wikirepo.write_page("index.md", pages.render_index(session))
    return {"folds": folds, "renames": renames, "prunes": prunes, "merge_ids": merge_ids}
