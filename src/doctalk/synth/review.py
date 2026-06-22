"""Resolving the #unresolved queue.

An entity the resolver couldn't confidently place is parked as ``status='unresolved'`` — a provisional
page that's *either* a genuinely new entity *or* a duplicate of an existing one, and the resolver
wouldn't guess. The duplicate case is the Duplicates triage (fold it from there); this module owns the
other verdict — **Keep**: accept it as genuinely distinct by promoting it to ``active``.

The review queue itself is usually empty (the provisional entity *is* the durable record), and
``render_index`` lists a page regardless of its entity's status, so Keep is a pure truth-store status
flip — nothing in the wiki changes, and it reverses by flipping back. Reads stay open; the API gates
the flip behind the admin token.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.db.models import Entity


def keep(session, entity_id: int) -> str | None:
    """Accept an unresolved entity as genuinely distinct — promote it to ``active``. Returns its name,
    or ``None`` if it isn't an unresolved entity (already resolved, merged away, or gone)."""
    e = session.get(Entity, entity_id)
    if e is None or e.status != "unresolved":
        return None
    repo.set_entity_status(session, entity_id, "active")
    return e.name


def reopen(session, entity_id: int) -> str | None:
    """Undo a Keep: send an entity back to the unresolved queue. Returns its name, or ``None`` if it
    isn't currently active — so a normal entity is never quietly demoted."""
    e = session.get(Entity, entity_id)
    if e is None or e.status != "active":
        return None
    repo.set_entity_status(session, entity_id, "unresolved")
    return e.name
