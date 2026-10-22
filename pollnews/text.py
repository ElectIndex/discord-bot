"""Name helpers shared by the NYT and manual paths."""

from __future__ import annotations

import unicodedata

PARTICLES = {"de", "la", "del", "van", "von", "da", "di", "le", "st", "st."}
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().replace(".", " ").replace("-", " ").replace("’", "'").split())


def short_name(name: str) -> str:
    """Last name, keeping particles ("de la Cruz") and dropping suffixes."""
    if fold(name).startswith("generic "):
        return name
    toks = name.split()
    while len(toks) > 1 and toks[-1].lower().rstrip(",") in SUFFIXES:
        toks.pop()
    if len(toks) <= 1:
        return " ".join(toks) or name
    out = [toks[-1]]
    i = len(toks) - 2
    while i > 0 and toks[i].lower() in PARTICLES:
        out.insert(0, toks[i])
        i -= 1
    return " ".join(out)
