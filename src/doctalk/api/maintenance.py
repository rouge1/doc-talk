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

from doctalk.config import get_settings
from doctalk.db.session import session_scope
from doctalk.synth import lint, merge, wikirepo

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


@router.get("/slug-collisions")
def slug_collisions() -> dict:
    """The slug-collision merge plan: ``mergeable`` (safe to fold) + ``skipped`` (left manual)."""
    with session_scope() as s:
        mergeable, skipped = merge.plan_slug_collision_merges(s)
        return {
            "mergeable": [{"src": _ent(src), "dst": _ent(dst)} for src, dst in mergeable],
            "skipped": [{"src": _ent(src), "dst": _ent(dst), "reason": why}
                        for src, dst, why in skipped],
        }


@router.post("/merge-collisions", dependencies=[Depends(require_admin)])
def merge_collisions() -> dict:
    """Apply the safe slug-collision merges (reversible) and commit the wiki. Mutating — gated."""
    with session_scope() as s:
        applied, skipped = merge.merge_slug_collisions(s)
    sha = None
    if applied:
        if wikirepo.commit(f"wiki-merge: {len(applied)} slug collision(s)"):
            sha = wikirepo.head_sha()
    return {
        "applied": [{"src": sname, "dst": dname} for sname, dname in applied],
        "skipped": [{"src": sname, "dst": dname, "reason": why} for sname, dname, why in skipped],
        "merged": len(applied), "sha": sha,
    }
