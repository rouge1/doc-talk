"""Entity-name normalization — step (0) of ``synth_resolve`` (see ``docs/entity-resolution.md``).

``norm_key`` is the cheap blocking key the resolver matches on: NFKC, lowercased, whitespace
collapsed, leading articles and a small set of trailing generic qualifiers stripped ("the E0
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
_WS = re.compile(r"\s+")


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
