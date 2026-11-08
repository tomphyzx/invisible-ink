"""The scan report, as text for a terminal or as JSON."""

import json

from .chars import KIND_NAMES, printable
from .detect import REASONS, WORD_KINDS

PACK_LABEL = {"grading": "grading", "hiring": "hiring", "review": "peer review"}


def _ai(tags):
    if not tags:
        return ""
    packs = [PACK_LABEL[t] for t in tags if t != "general"]
    return "   << reads like instructions to an AI%s >>" % (" (%s wording)" % ", ".join(packs) if packs else "")


def _quote(text, limit=600):
    t = printable(" ".join(text.split()))
    return '"%s%s"' % (t[:limit], "..." if len(t) > limit else "")


def _mismatch_lines(items, verbose):
    shown = items if verbose else items[:20]
    lines = ["  Page %d (%s): text layer says %s, page shows %s" % (
        m["page"], m["kind"], _quote(m["text_layer"], 60), _quote(m["page_shows"], 60)) for m in shown]
    if len(shown) < len(items):
        lines.append("  ... and %d more (use --verbose)" % (len(items) - len(shown)))
    return lines


def text_report(r, verbose=False):
    out = []
    fmt = {"pdf": "PDF", "office": "Word file", "image": "image", "text": "text file"}.get(r["kind"], r["kind"])
    head = "%s, %d page%s" % (fmt, r["pages"], "" if r["pages"] == 1 else "s") if r["pages"] else fmt
    if r["sandboxed"] is not None:
        head += ". Opened in a sandbox: %s" % ("yes" if r["sandboxed"] else "NO")
    out += ["", "Invisible Ink report: %s" % printable(r["file"]), head + "."]
    if r["truncated_pages"]:
        out.append("Only the first %d pages were checked (%d more not checked)." % (r["pages"], r["truncated_pages"]))

    if r["hidden"]:
        n = sum(p["word_count"] for p in r["hidden"])
        out += ["", "HIDDEN TEXT: %d word%s in %d passage%s" % (n, "" if n == 1 else "s", len(r["hidden"]),
                                                                  "" if len(r["hidden"]) == 1 else "s")]
        for p in r["hidden"]:
            out.append("  Page %d, %s:" % (p["page"], REASONS[p["reason"]]))
            out.append("    " + _quote(p["text"]) + _ai(p["ai"]))
        if r.get("hidden_ai") and not any(p["ai"] for p in r["hidden"]):
            out.append("  Taken together, the hidden text is worded as instructions to an AI.")
    if r.get("fragments"):
        n = sum(p["word_count"] for p in r["fragments"])
        pages = sorted({p["page"] for p in r["fragments"]})
        out += ["", "Also %d stray word%s, number%s or letter%s out of sight on page %s. On their own they cannot "
                "carry an instruction, so they are not counted (old OCR layers in figures leave many)%s"
                % (n, "" if n == 1 else "s", "" if n == 1 else "s", "" if n == 1 else "s",
                   ", ".join(map(str, pages)), ":" if verbose else ". --verbose lists them.")]
        if verbose:
            for p in r["fragments"]:
                out.append("  Page %d: %s" % (p["page"], _quote(p["text"], 200)))
    if r["uncertain"]:
        out += ["", "TEXT LAYER UNDER PICTURES THAT SHOW TEXT (most likely the picture's own OCR layer; not counted)"]
        shown = r["uncertain"] if verbose else r["uncertain"][:5]
        for p in shown:
            out.append("  Page %d: %s" % (p["page"], _quote(p["text"], 300)))
        if len(shown) < len(r["uncertain"]):
            out.append("  ... and %d more (use --verbose)" % (len(r["uncertain"]) - len(shown)))
    typed = [m for m in r.get("text_mismatch", []) if not m["scan_layer"]]
    scanned = [m for m in r.get("text_mismatch", []) if m["scan_layer"]]
    if typed:
        out += ["", "TEXT LAYER DOES NOT MATCH THE PAGE: an AI reading this file gets something other than a "
                    "reader sees. Check the page image."]
        out += _mismatch_lines(typed, verbose)
    if scanned:
        out += ["", "A SCAN'S OCR LAYER DISAGREES WITH ITS PICTURE (usually OCR error, sometimes not; not counted):"]
        out += _mismatch_lines(scanned, verbose)
    if r["word"]:
        out += ["", "IN THE WORD FILE BUT NOT ON THE PAGE"]
        for f in r["word"]:
            where = "" if f["where"] == "document" else " (%s)" % f["where"]
            out.append("  %s%s: %s%s" % (WORD_KINDS[f["kind"]], where, _quote(f["text"], 400), _ai(f["ai"])))
    if r["invisible_chars"]:
        out += ["", "INVISIBLE CHARACTERS"]
        for f in r["invisible_chars"][: (200 if verbose else 20)]:
            line = "  %d %s" % (f["count"], KIND_NAMES[f["kind"]])
            if f["after"]:
                line += ' after "%s"' % printable(f["after"])
            out.append(line)
            if f["decoded"]:
                out.append("    they spell: " + _quote(f["decoded"]) + _ai(f["ai"]))
        if len(r["invisible_chars"]) > 20 and not verbose:
            out.append("  ... and %d more (use --verbose)" % (len(r["invisible_chars"]) - 20))
    if r["layers_off"]:
        out += ["", "SWITCHED-OFF LAYERS: %d. Some PDF readers and text extractors include their text." % r["layers_off"]]
    if r["active_content"]:
        out += ["", "ACTIVE OR LINKED CONTENT: " + ", ".join(r["active_content"])]
    if r["visible_ai_pages"]:
        out += ["", "VISIBLE TEXT WORDED AS INSTRUCTIONS TO AN AI: page %s. A human reader sees it too."
                % ", ".join(map(str, r["visible_ai_pages"]))]
    if r["metadata"] and (verbose or r["metadata_ai"]):
        out += ["", "FILE PROPERTIES" + _ai(r["metadata_ai"])]
        for k, v in r["metadata"].items():
            out.append("  %s: %s" % (printable(k), printable(v)))
    elif r["metadata"]:
        out += ["", "File properties: %s (use --verbose to show)" % ", ".join(r["metadata"])]
    for note in r["notes"]:
        out += ["", "Note: " + note]

    out.append("")
    if r["verdict"] == "hidden":
        out.append("RESULT: found content a reader would not see. "
                   "`invisible-ink clean` makes a copy without it.")
    elif r["verdict"] == "mismatch":
        out.append("RESULT: the file's text layer says something its pages do not show. "
                   "`invisible-ink clean` makes a copy that reads what the pages show.")
    elif r["verdict"] == "active":
        out.append("RESULT: nothing hidden found, but the file has active or linked content. "
                   "`invisible-ink clean` makes a copy without it.")
    else:
        out.append("RESULT: nothing hidden found.")
    if r["sandboxed"] is False:
        out.append("Warning: no sandbox was available, so the file was opened directly.")
    return "\n".join(out)


def json_report(reports):
    return json.dumps(reports if len(reports) != 1 else reports[0], indent=2, ensure_ascii=True)
