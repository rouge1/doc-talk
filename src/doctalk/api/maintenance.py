"""Maintenance API — the operator loop (lint → heal → merge → prune) over HTTP for the SPA.

Read endpoints (the findings dashboard + the slug-collision plan) are open; mutating endpoints go
through ``require_admin``, which checks the ``X-Admin-Token`` header against ``settings.admin_token``.
That token is empty by default (gate open, single-user local dev) and locks the moment it's set — so
the gate mechanism ships now and the password lands later, without exposing the destructive actions
ungated in the meantime. Every action reuses the exact functions the CLI calls; no logic forks here.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import Entity
from doctalk.db.session import session_scope
from doctalk.synth import dedupe, disambiguate, lint, merge, pages, wikirepo

router = APIRouter(prefix="/api/maintenance")


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Gate a mutating endpoint. No-op while ``admin_token`` is unset (dev); 401 on mismatch once
    it's configured. Reads never depend on this — only actions that change the truth store/wiki."""
    token = get_settings().admin_token
    if token and x_admin_token != token:
        raise HTTPException(status_code=401, detail="admin token required")


def _group(findings: list[lint.Finding]) -> list[dict[str, Any]]:
    """Collapse a flat finding list into ``[{kind, count, items:[{detail, ref}]}]`` for the dashboard."""
    by_kind: dict[str, list[lint.Finding]] = defaultdict(list)
    for f in findings:
        by_kind[f.kind].append(f)
    return [
        {"kind": kind, "count": len(items),
         "items": [{"detail": f.detail, "ref": f.ref} for f in items]}
        for kind, items in sorted(by_kind.items())
    ]


@router.get("/lint")
def lint_findings() -> dict:
    """Health check: orphans, unsupported claims, missing/deleted pages, slug collisions, etc."""
    with session_scope() as s:
        groups = _group(lint.lint(s, get_settings().wiki_dir))
    return {"total": sum(g["count"] for g in groups), "groups": groups}


@router.get("/audit")
def audit_findings() -> dict:
    """Integrity audit: wiki ↔ truth drift (dangling chunk sources, catalog-vs-disk)."""
    with session_scope() as s:
        groups = _group(lint.audit(s, get_settings().wiki_dir))
    return {"total": sum(g["count"] for g in groups), "groups": groups}


def _ent(e) -> dict:
    return {"id": e.id, "name": e.name, "type": e.type,
            "stem": e.wiki_path.rsplit("/", 1)[-1][:-3] if e.wiki_path else None}


def _remedy(reason: str) -> str:
    """How a skipped collision can still be resolved: a distinct-norm_key pair only collides in the
    slugifier, so it's mechanically fixable by giving each its own page (``disambiguate``); anything
    else (e.g. incompatible types — a same-name polysemy) genuinely needs a human (``manual``)."""
    return "disambiguate" if reason.startswith("distinct norm_key") else "manual"


@router.get("/slug-collisions")
def slug_collisions() -> dict:
    """The slug-collision plan: ``mergeable`` (safe to fold) + ``skipped`` (not a merge). Each skipped
    pair carries a ``remedy`` — ``disambiguate`` (give each its own page) or ``manual``."""
    with session_scope() as s:
        mergeable, skipped = merge.plan_slug_collision_merges(s)
        return {
            "mergeable": [{"src": _ent(src), "dst": _ent(dst)} for src, dst in mergeable],
            "skipped": [{"src": _ent(src), "dst": _ent(dst), "reason": why, "remedy": _remedy(why)}
                        for src, dst, why in skipped],
        }


@router.post("/merge-collisions", dependencies=[Depends(require_admin)])
def merge_collisions() -> dict:
    """Apply the safe slug-collision merges (reversible) and commit the wiki. Mutating — gated.
    Stamps the commit sha onto the merge rows so the whole batch can be undone by that handle."""
    with session_scope() as s:
        applied, skipped = merge.merge_slug_collisions(s)
        sha = None
        if applied and wikirepo.commit(f"wiki-merge: {len(applied)} slug collision(s)"):
            sha = wikirepo.head_sha()
            if sha:
                repo.set_merge_committed_sha(s, [mid for _, _, mid in applied], sha)
    dedupe.invalidate_plan_cache()  # entities moved — the next duplicates plan must recompute
    return {
        "applied": [{"src": sname, "dst": dname} for sname, dname, _ in applied],
        "skipped": [{"src": sname, "dst": dname, "reason": why} for sname, dname, why in skipped],
        "merged": len(applied), "sha": sha,
    }


class UndoRequest(BaseModel):
    sha: str  # the wiki-commit handle a merge batch was stamped with (from the apply response)


@router.post("/undo-merge", dependencies=[Depends(require_admin)])
def undo_merge(body: UndoRequest) -> dict:
    """Reverse a merge batch by its commit handle: repoint every moved claim/mention back, resurrect
    the folded-away entities, restore their pages + name vectors, and commit the reversal as its own
    wiki commit (merge then unmerge both show in the git log). Mutating — gated."""
    with session_scope() as s:
        undone = merge.undo_batch(s, body.sha)
        new_sha = None
        if undone and wikirepo.commit(f"wiki-unmerge: reversed {len(undone)} merge(s)"):
            new_sha = wikirepo.head_sha()
    dedupe.invalidate_plan_cache()  # entities resurrected — the next duplicates plan must recompute
    return {
        "undone": [{"src": src, "dst": dst} for src, dst in undone],
        "count": len(undone),
        "sha": new_sha,
    }


@router.get("/duplicates")
def duplicate_plan() -> dict:
    """Read-only triage of the near-duplicate entities lint flags: each candidate pair scored with the
    resolver's own signals and bucketed into fold / judge / aside, plus every score for the gauge.
    No merges, no LLM — a plan to look at before any heal is wired up."""
    with session_scope() as s:
        return dedupe.plan_duplicates(s)


@router.get("/compare")
def compare_duplicate(a: int, b: int) -> dict:
    """Side-by-side evidence for one candidate pair: the score + signals, plus each entity's source
    passages (raw chunk text where it's mentioned) so a human can read both contexts and judge whether
    they're the same entity. Read-only."""
    with session_scope() as s:
        out = dedupe.compare_pair(s, a, b)
    if out is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return out


@router.post("/disambiguate", dependencies=[Depends(require_admin)])
def disambiguate_collisions() -> dict:
    """Give each genuinely-distinct slug collision its own page (a stable ``<base>-<disc>`` slug for the
    loser; the lower-id sibling keeps the bare slug). No claims move — this only un-shares the filename.
    Reversible. Mutating — gated. Returns the receipt + the wiki commit handle."""
    with session_scope() as s:
        applied = disambiguate.disambiguate_collisions(s)
        sha = None
        if applied and wikirepo.commit(f"wiki-disambiguate: {len(applied)} slug collision(s)"):
            sha = wikirepo.head_sha()
    dedupe.invalidate_plan_cache()  # slugs changed — drop the cached plan so sample stems stay fresh
    return {
        "applied": [{"name": name, "base": base, "slug": slug, "id": eid}
                    for name, base, slug, eid in applied],
        "count": len(applied), "sha": sha,
    }


class DisambiguateUndoRequest(BaseModel):
    ids: list[int]  # the entity ids to fold back onto the shared slug (from the apply receipt)


@router.post("/undo-disambiguate", dependencies=[Depends(require_admin)])
def undo_disambiguate(body: DisambiguateUndoRequest) -> dict:
    """Reverse a disambiguation batch: clear each entity's slug override so it rejoins the shared slug
    (restoring the prior collision, exactly as un-merge does), retire its standalone page, and commit
    the reversal as its own wiki commit. Mutating — gated."""
    with session_scope() as s:
        undone = disambiguate.undo_disambiguations(s, body.ids)
        new_sha = None
        if undone and wikirepo.commit(f"wiki-disambiguate: reversed {len(undone)} split(s)"):
            new_sha = wikirepo.head_sha()
    dedupe.invalidate_plan_cache()  # slugs changed — drop the cached plan so sample stems stay fresh
    return {"undone": [{"name": n, "base": b} for n, b in undone], "count": len(undone), "sha": new_sha}


@router.get("/recent-disambiguations")
def recent_disambiguations() -> dict:
    """The entities currently split off to their own slug (the durable record), so the receipt + Undo
    survive a reload. Empty when nothing has been disambiguated."""
    with session_scope() as s:
        ents = disambiguate.disambiguated_entities(s)
        return {
            "count": len(ents),
            "entities": [{"id": e.id, "name": e.name, "base": pages.base_slug_for(e), "slug": e.slug}
                         for e in ents],
        }


@router.get("/recent-merges")
def recent_merges() -> dict:
    """The most recent reversible merge batch (the rows sharing the newest commit handle), so the page
    can still offer Undo after a reload. Empty when nothing reversible has been merged."""
    with session_scope() as s:
        latest_sha: str | None = None
        for m in repo.get_entity_merges(s):  # id-ordered → the last committed batch wins
            if m.committed_sha and m.moved is not None:
                latest_sha = m.committed_sha
        if latest_sha is None:
            return {"sha": None, "count": 0, "merges": []}
        batch = repo.get_merges_by_sha(s, latest_sha)
        merges = []
        for m in batch:
            src, dst = s.get(Entity, m.from_id), s.get(Entity, m.into_id)
            merges.append({
                "id": m.id,
                "src": src.name if src else f"#{m.from_id}",
                "dst": dst.name if dst else f"#{m.into_id}",
            })
        return {"sha": latest_sha, "count": len(merges), "merges": merges}
