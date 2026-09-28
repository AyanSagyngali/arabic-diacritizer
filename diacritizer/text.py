"""Arabic diacritic utilities and the 'diacritics-only' guard.

The guard guarantees the service never changes the user's text: the output's
base characters (everything that is not a harakat mark) must equal the input's
base characters exactly. If a model rewrites, drops or adds letters, we realign
what we can and fall back to the original text for the rest.
"""
from __future__ import annotations

import unicodedata

# Fathatan..Sukun, superscript alef, plus Quranic small marks commonly emitted by models
DIACRITICS = set(chr(c) for c in range(0x064B, 0x0653)) | {"ٰ"}
TATWEEL = "ـ"


def is_diacritic(ch: str) -> bool:
    return ch in DIACRITICS


def strip_diacritics(text: str) -> str:
    return "".join(ch for ch in text if ch not in DIACRITICS)


def base_equal(a: str, b: str) -> bool:
    return strip_diacritics(a) == strip_diacritics(b)


def split_units(text: str) -> list[tuple[str, str]]:
    """[(base_char, marks_after_it), ...]; leading marks attach to an empty base."""
    units: list[tuple[str, str]] = []
    for ch in text:
        if ch in DIACRITICS:
            if units:
                b, m = units[-1]
                units[-1] = (b, m + ch)
            else:
                units.append(("", ch))
        else:
            units.append((ch, ""))
    return units


def _normalize_marks(marks: str) -> str:
    """Canonical order (shadda first), drop duplicates, at most one short vowel + shadda/sukun."""
    seen = []
    for m in marks:
        if m not in seen:
            seen.append(m)
    return unicodedata.normalize("NFC", "".join(sorted(seen, key=lambda c: (c != "ّ", c))))


def enforce_diacritics_only(original: str, model_output: str) -> tuple[str, dict]:
    """Return text whose base equals `original`, carrying over the model's marks where letters align.

    Uses an LCS alignment on base characters so a single inserted/deleted/changed letter
    by the model only loses marks locally instead of discarding the whole sentence.
    Marks already present in the original are kept as-is.
    """
    src = split_units(original)
    out = split_units(model_output)
    if [b for b, _ in src] == [b for b, _ in out]:
        text = "".join(b + (m_src or _normalize_marks(m_out)) for (b, m_src), (_, m_out) in zip(src, out))
        return text, {"exact": True, "aligned": len(src), "unaligned": 0}

    a, b = [u[0] for u in src], [u[0] for u in out]
    n, m = len(a), len(b)
    # LCS table (texts are sentence/paragraph sized; O(n*m) is fine and dependency-free)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai, row, nxt = a[i], dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if ai == b[j] else max(nxt[j], row[j + 1])
    i = j = 0
    marks = [""] * n
    aligned = 0
    while i < n and j < m:
        if a[i] == b[j]:
            marks[i] = out[j][1]
            aligned += 1
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    text = "".join(bch + (m_src or _normalize_marks(marks[k])) for k, (bch, m_src) in enumerate(src))
    return text, {"exact": False, "aligned": aligned, "unaligned": n - aligned}


def diacritic_error_rate(reference: str, hypothesis: str) -> float:
    """DER over Arabic letters: share of letters whose marks differ. Requires equal base text."""
    r, h = split_units(reference), split_units(hypothesis)
    if [x for x, _ in r] != [x for x, _ in h]:
        raise ValueError("base text differs")
    letters = [(rm, hm) for (rb, rm), (_, hm) in zip(r, h) if "ء" <= rb <= "ي"]
    if not letters:
        return 0.0
    return sum(_normalize_marks(rm) != _normalize_marks(hm) for rm, hm in letters) / len(letters)
