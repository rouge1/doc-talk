"""LLM entity + claim extraction — sub-stage (1) of synthesis (``synth_entities``).

Reads a window of source text and emits a schema-validated list of entities, each with a few
grounded claims. The LLM is forced into JSON mode and *never* writes SQL or markdown here — it only
proposes structured candidates; persistence, provenance, and resolution happen downstream. Parsing
is defensive (strips code fences, tolerates an object-or-list top level, drops malformed items) so a
sloppy local model degrades to fewer entities rather than a crash.

Provenance is deliberately *not* taken from the model: the stage attributes each entity to the real
chunks whose text contains its name/alias, keeping claims auditable against the truth store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from doctalk.models.chat import chat
from doctalk.synth.gate import is_pageworthy

# Controlled vocabulary keeps pages groupable and the resolver's type-gating meaningful; an
# unrecognized type falls back to the catch-all "concept" rather than spawning a junk type.
ENTITY_TYPES = {"concept", "component", "protocol", "person", "organization", "product", "standard"}

_SYSTEM = (
    "You are a precise knowledge-extraction component for a local wiki. Given a passage, extract "
    "the salient named entities (concepts, components, protocols, people, organizations, products, "
    "standards) and, for each, a few short factual claims stated IN the passage. Use only "
    "information present in the passage — never invent facts. Extract only subjects a reader would "
    "want a reference page about. Do NOT extract data values: numeric or hexadecimal literals "
    "(0x0009, 350, 3.5), measurements or units (100 ms, 2.4 GHz), parameter/field values, or the "
    "document's own section/table/figure numbers (Section 2.3, Table 5). Respond with JSON only."
)

_SCHEMA_HINT = (
    'Return an object: {"entities": [{"name": str, "type": one of '
    f"{sorted(ENTITY_TYPES)}, "
    '"aliases": [str], "claims": [str]}]}. '
    "Claims are complete, self-contained sentences. Omit entities with no claims."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _json_blob(text: str) -> str | None:
    """Best-effort: pull the outermost JSON object/array out of a prose-wrapped reply.

    A small local model under load sometimes ignores JSON mode and pads the payload with prose
    ("Here is the breakdown… {…}"). Rather than discard the whole window, grab the first ``{``/``[``
    through its matching last ``}``/``]`` and let the caller try to parse that.
    """
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end > start else None


@dataclass
class ExtractedEntity:
    name: str
    type: str
    aliases: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)


def _coerce(raw: object) -> list[ExtractedEntity]:
    """Validate the model's JSON into clean dataclasses, dropping anything malformed."""
    if isinstance(raw, dict):
        raw = raw.get("entities", [])
    if not isinstance(raw, list):
        return []
    out: list[ExtractedEntity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        type_ = str(item.get("type", "concept")).strip().lower()
        if type_ not in ENTITY_TYPES:
            type_ = "concept"
        if not is_pageworthy(name, type_):  # data values (0x0009, "350 ms") never become candidates
            continue
        # A sloppy model sometimes returns these as a bare string — iterating it would explode a
        # claim into per-character "claims" ("SALT is…" -> "S","A","L","T",…). Wrap, don't iterate.
        raw_aliases = item.get("aliases", [])
        raw_claims = item.get("claims", [])
        aliases = [str(a).strip() for a in ([raw_aliases] if isinstance(raw_aliases, str) else raw_aliases) if str(a).strip()]
        claims = [str(c).strip() for c in ([raw_claims] if isinstance(raw_claims, str) else raw_claims) if str(c).strip()]
        if not claims:  # an entity with no grounded claim isn't worth a page
            continue
        out.append(ExtractedEntity(name=name, type=type_, aliases=aliases, claims=claims))
    return out


def parse_entities(text: str) -> list[ExtractedEntity]:
    """Parse a raw model response (JSON, possibly fenced) into entities. Exposed for testing."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return _coerce(json.loads(cleaned))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    blob = _json_blob(cleaned)  # prose-wrapped JSON -> salvage the payload
    if blob is not None and blob != cleaned:
        try:
            return _coerce(json.loads(blob))
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    return []


def extract_entities(
    passage: str, *, model: str | None = None, timeout: float | None = None
) -> list[ExtractedEntity]:
    """Call the local LLM to extract entities + claims from one passage of source text."""
    kwargs = {"timeout": timeout} if timeout is not None else {}
    response = chat(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"{_SCHEMA_HINT}\n\nPASSAGE:\n{passage}"},
        ],
        model=model,
        format="json",
        options={"temperature": 0},  # deterministic-ish extraction
        **kwargs,
    )
    return parse_entities(response)
