"""Entity-name normalization — step (0) of ``synth_resolve`` (see ``docs/entity-resolution.md``).

``norm_key`` is the cheap blocking key the resolver matches on: NFKC, lowercased, whitespace AND
underscores collapsed (so ``AFH_channel_map`` and "AFH channel map" key alike — the model renders the
same concept both ways, and treating them as one avoids the fragmentation that left slug-colliding
duplicate pages), leading articles and a small set of trailing generic qualifiers stripped ("the E0
procedure" / "E0 process" → ``e0``). The stripped qualifiers are *not* discarded by callers — they
keep the original surface as an alias. Deterministic and dependency-free so it's stable across runs
and trivially testable; the same key is reused by the future fuzzy/embedding resolver.
"""

from __future__ import annotations

import re
import unicodedata

# Trailing generic nouns that add no identity ("E0 cipher" and "E0" are the same thing). Kept
# small and conservative — over-stripping causes conflation, the costlier failure mode.
_GENERIC_TRAILING = {"procedure", "process", "mechanism", "feature", "function", "method"}
_LEADING_ARTICLES = {"the", "a", "an"}
_WS = re.compile(r"[\s_]+")  # underscores are separators: AFH_channel_map keys like "AFH channel map"


_ACRONYM = re.compile(r"^(.+?)\s*\(([A-Za-z][A-Za-z0-9.\-]{1,15})\)\s*$")

# Spec test-vector tables key each row with a transaction id ("T_ID 5 - RTT AA candidates" is row 5
# of a transaction-ID table, not an entity named that). The row's real subject follows the label.
# Strip the label so the surface collapses to that subject and resolves into the genuine entity
# instead of fragmenting into a per-row twin. Deliberately narrow — the label must be the
# transaction-ID keyword followed by a number, so real names that merely end in a digit ("AES 128",
# "IEEE 802.11") are never touched.
_ROW_LABEL = re.compile(r"^t[_\- ]?id\s*[#:]?\s*\d+\s*(?:[-–—:]\s*)?", re.IGNORECASE)


def strip_row_label(surface: str) -> str:
    """Drop a leading transaction-ID row label, keeping the cell's real subject:
    ``"T_ID 5 - RTT AA candidates"`` → ``"RTT AA candidates"``. Returns ``""`` when the surface is
    *only* a label (``"T_ID 5"``), which names a table row, not a subject — extraction drops it and
    the gate fails it so ``wiki-prune`` can reap any that predate this rule."""
    return _ROW_LABEL.sub("", surface.strip()).strip()


def acronym_pair(surface: str) -> tuple[str, str] | None:
    """Detect a definitional ``Foo Bar (FB)`` surface and return ``(expansion_norm, acronym_norm)``
    — the bidirectional bridge that lets "L2CAP" resolve to "Logical Link Control…". None if no
    such pattern. High-value for specs, which literally define their acronyms."""
    m = _ACRONYM.match(surface.strip())
    if not m:
        return None
    return norm_key(m.group(1)), m.group(2).lower().replace(".", "")


def norm_key(surface: str) -> str:
    """Normalize a surface form to its blocking key (may be empty for junk input)."""
    s = unicodedata.normalize("NFKC", surface).lower().strip()
    s = _WS.sub(" ", s)
    s = s.strip(" \t\n\r\"'`.,:;()[]{}")
    tokens = s.split(" ") if s else []
    while len(tokens) > 1 and tokens[0] in _LEADING_ARTICLES:
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _GENERIC_TRAILING:
        tokens = tokens[:-1]
    return " ".join(tokens)
