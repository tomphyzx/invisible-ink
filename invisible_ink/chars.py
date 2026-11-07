"""Invisible Unicode characters: zero-width characters, tag characters, variation-selector
smuggling and bidirectional overrides. None of them shows on screen, but an AI reads every one.

    find(text)  -> list of findings
    strip(text) -> text with the suspicious characters removed
"""

import unicodedata

TAG = range(0xE0000, 0xE0080)            # "ASCII smuggling": each tag character mirrors one ASCII character
ZERO_WIDTH = {0x200B, 0x2060, 0xFEFF, 0x180E, 0x2061, 0x2062, 0x2063, 0x2064, 0x034F,
              0x115F, 0x1160, 0x3164, 0xFFA0}
JOINERS = {0x200C, 0x200D}               # legitimate in emoji and many scripts; flagged between ASCII only
BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
BLACK_FLAG = 0x1F3F4                     # subdivision flags (England, Scotland, Wales) use tag characters

KIND_NAMES = {"tag": "tag characters", "variation": "variation selectors", "zero_width": "zero-width characters",
              "bidi": "text-direction overrides"}


def _is_vs(cp):
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def _vs_byte(cp):
    return cp - 0xFE00 if cp <= 0xFE0F else cp - 0xE0100 + 16


def _kind(text, i):
    cp = ord(text[i])
    if cp in TAG:
        return "tag"
    if _is_vs(cp):
        return "variation"
    if cp in ZERO_WIDTH:
        return None if (cp == 0xFEFF and i == 0) else "zero_width"
    if cp in JOINERS:
        before = text[i - 1] if i > 0 else " "
        after = text[i + 1] if i + 1 < len(text) else " "
        return "zero_width" if before.isascii() and after.isascii() else None
    if cp in BIDI:
        return "bidi"
    return None


def _runs(text):
    """Yield (kind, start, end) for each run of suspicious characters."""
    i, n = 0, len(text)
    while i < n:
        k = _kind(text, i)
        if k is None:
            i += 1
            continue
        j = i + 1
        while j < n and _kind(text, j) == k:
            j += 1
        yield k, i, j
        i = j


def _legit(text, kind, i, j):
    if kind == "tag":                    # a flag emoji: black flag, tags, cancel tag
        return i > 0 and ord(text[i - 1]) == BLACK_FLAG and ord(text[j - 1]) == 0xE007F
    if kind == "variation":              # one selector after a character is ordinary emoji/CJK use
        return j - i == 1
    return False


def _decode(text, kind, i, j):
    if kind == "tag":
        s = "".join(chr(ord(c) - 0xE0000) for c in text[i:j] if 0xE0020 <= ord(c) <= 0xE007E)
        return s or None
    if kind == "variation":
        data = bytes(_vs_byte(ord(c)) for c in text[i:j])
        s = data.decode("utf-8", "replace")
        printable = sum(ch.isprintable() for ch in s)
        return s if s and printable >= 0.8 * len(s) else None
    return None


def _context(text, i):
    before = "".join(c if c.isprintable() else " " for c in text[max(0, i - 80):i] if _kind(c, 0) is None)
    return " ".join(before.split())[-40:]


def find(text, limit=200):
    out = []
    for kind, i, j in _runs(text):
        if _legit(text, kind, i, j):
            continue
        out.append({"kind": kind, "count": j - i, "decoded": _decode(text, kind, i, j),
                    "after": _context(text, i)})
        if len(out) >= limit:
            break
    return out


def strip(text):
    keep, last = [], 0
    for kind, i, j in _runs(text):
        if _legit(text, kind, i, j):
            continue
        keep.append(text[last:i])
        last = j
    keep.append(text[last:])
    return "".join(keep)


def printable(s):
    """Make text safe to print in a terminal: control and invisible characters become \\u escapes."""
    out = []
    for c in s:
        if c in "\n\t" or (c.isprintable() and unicodedata.category(c) not in ("Cf", "Co", "Cs")):
            out.append(c)
        else:
            out.append("\\u%04x" % ord(c) if ord(c) <= 0xFFFF else "\\U%08x" % ord(c))
    return "".join(out)
