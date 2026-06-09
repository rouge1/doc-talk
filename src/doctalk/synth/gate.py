"""Pageworthiness gate — keeps data values out of the entity space.

A spec is full of strings that *look* like extraction candidates but are values, not subjects:
numeric/hex literals ("0", "0x0009"), measurements ("350 ms"), and document self-references
("Section 2.3", "Table 5"). A sloppy local model emits them as entities, and each one becomes a
junk wiki page (Core_v6.0.pdf yielded ~3,000 entities, many of this kind). ``is_pageworthy`` is the
deterministic, dependency-free predicate that rejects them — applied at extraction time
(``extract._coerce``) so junk never becomes a candidate, and retroactively by ``wiki-prune``.

Deliberately conservative: it rejects only shapes that are *structurally* data values. Anything
debatable passes — the costlier failure mode is dropping a real entity (e.g. "E0", a cipher whose
name is two characters of hex), so short all-caps acronym shapes are explicitly allowed and the
hex-ish rule requires both a digit and length >= 4.
"""

from __future__ import annotations

import re

# All-caps alnum starting with a letter: "E0", "HCI", "L2CAP". Real spec entities, never dropped
# for being short or hex-shaped.
_ACRONYM_SHAPE = re.compile(r"^[A-Z][A-Z0-9]+$")

# One letter + one digit: "h3", "f4", "s1" — the Bluetooth spec names its crypto functions this
# way (lowercase, so the acronym shape misses them). Equation *variables* are single letters or
# letter pairs ("x", "Cx", "Na"), so this shape re-admits no junk.
_NAMED_FN_SHAPE = re.compile(r"^[A-Za-z][0-9]$")

_PURE_NUMBER = re.compile(r"^[0-9]+([.,][0-9]+)*$")          # 0 · 3.5 · 1,000 · 3.2.1
_HEX_LITERAL = re.compile(r"^0x[0-9A-Fa-f]+$")               # 0x0009
_HEXISH = re.compile(r"^[0-9A-Fa-f]{4,}$")                   # 0009 · a1b2 (digit required, see below)

_UNITS = (
    "ms|us|µs|ns|s|hz|khz|mhz|ghz|db|dbm|dbi|kb|mb|gb|kbps|mbps|byte|bytes|bit|bits|"
    "octet|octets|slot|slots|symbol|symbols|v|mv|ma|µa|ua|w|mw|%|ppm|km|cm|mm|m"
)
_MEASUREMENT = re.compile(rf"^[0-9][0-9.,]*\s*({_UNITS})$", re.IGNORECASE)

# "Section 2.3", "Table 5-1", "Figure 3.2a" — the document referring to itself, not a subject.
_DOC_REF = re.compile(
    r"^(section|table|figure|fig|chapter|clause|annex|appendix|part|step|page|equation|eq|"
    r"vol|volume|note|item|case|row|column|byte|bit|octet|field)\s*[0-9][0-9A-Za-z.\-]*$",
    re.IGNORECASE,
)


def is_pageworthy(name: str, type_: str = "concept") -> bool:
    """True if ``name`` could be the subject of a wiki page; False for structural data values."""
    s = name.strip()
    if not s or not any(ch.isalpha() for ch in s):  # "0", "3.2.1", "—" — no letters, no page
        return False
    if _PURE_NUMBER.match(s) or _HEX_LITERAL.match(s):
        return False
    if _MEASUREMENT.match(s) or _DOC_REF.match(s):
        return False
    if _ACRONYM_SHAPE.match(s) or _NAMED_FN_SHAPE.match(s):  # "E0", "AES" — or "h3", "f4"
        return True
    if _HEXISH.match(s) and any(ch.isdigit() for ch in s):  # "a1b2" yes, "cafe" no
        return False
    if len(s) < 3:  # leftover two-char fragments that aren't acronym-shaped ("ab", "x)")
        return False
    return True
