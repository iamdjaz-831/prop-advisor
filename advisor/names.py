"""Name normalisation so 'A.J. Brown Jr.' on one feed matches 'AJ Brown' on another."""
from __future__ import annotations

import re
import unicodedata

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`]", "", s)
    s = re.sub(r"[^a-z0-9+ ]", " ", s)
    return " ".join(t for t in s.split() if t not in _SUFFIXES)


def norm_team(s: str) -> str:
    n = norm_name(s)
    if n.startswith("la "):
        n = "los angeles " + n[3:]
    return n
