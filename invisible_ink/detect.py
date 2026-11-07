"""Find what a reader cannot see but a text extractor (and so an AI) will read.

The main test is direct: each page is drawn twice, with and without its text. A word
in the text layer that changes no pixels between the two cannot be seen, whatever the
trick (white on white, text in a colour matching its background, PDF's invisible text
mode, a shape or image drawn over it, a clip, a switched-off layer). A word that changes
pixels only slightly is faint. Words under 3 pt or outside the page are caught by size
and position.
"""

import difflib
import os
import re

from PIL import ImageStat

from . import chars, ocr, patterns

TINY_PT = 3.0
FAINT_MAX = 60           # largest change a word makes, out of 255, below which it is "faint"
NO_INK_FRACTION = 0.002  # a word changing fewer pixels than this share of its box draws nothing
BLANK_SD10 = 60          # contrast of the area x10: below it, the area is a flat colour

REASONS = {
    "invisible": "invisible: leaves no mark on the page",
    "covered": "covered by an image or shape",
    "faint": "faint: barely different from the background",
    "tiny": "too small to read (under 3 pt)",
    "off_page": "outside the page",
}
WORD_KINDS = {"hidden": "Text marked hidden in Word", "deleted": "Deleted text kept by tracked changes",
              "comment": "Comment", "alt_text": "Image alt text"}


def _norm(s):
    return re.sub(r"[^\w]", "", s.lower())


def _overlap(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))
    return ix * iy / area


def classify_words(words, page):
    """Reason for each word that cannot be seen, or None, and the words the two renderers drew
    differently. words: (text, x0, y0, x1, y1) in pt."""
    ink = page["ink"]
    out, disagree = [], []
    for i, (text, x0, y0, x1, y1) in enumerate(words):
        reason = None
        if not re.search(r"\w", text):
            pass
        elif x1 < 0 or y1 < 0 or x0 > page["width_pt"] or y0 > page["height_pt"]:
            reason = "off_page"
        elif (y1 - y0) < TINY_PT:
            reason = "tiny"
        elif ink is not None:
            changed, area, top, sd10, poppler_sd10, changed_up = ink[i]
            if area > 0 and changed <= max(1, area * NO_INK_FRACTION):
                if changed_up > max(1, area * NO_INK_FRACTION):
                    reason = None            # a tall maths-font box; the glyph sits above it
                elif sd10 >= BLANK_SD10:
                    reason = "covered"
                elif poppler_sd10 >= BLANK_SD10:
                    reason = None            # Poppler draws it; Ghostscript lacked the font
                    disagree.append(i)
                else:
                    reason = "invisible"
            elif area > 0 and top < FAINT_MAX:
                reason = "faint"
        out.append(reason)
    return out, disagree


def _fallback_blank(words, reasons, img, dpi):
    """Without Ghostscript: a word over a perfectly flat area cannot be visible."""
    if img is None:
        return reasons
    g = img.convert("L")
    s = dpi / 72.0
    for i, (text, x0, y0, x1, y1) in enumerate(words):
        if reasons[i] or not re.search(r"\w", text):
            continue
        box = (max(0, int(x0 * s)), max(0, int(y0 * s)), min(g.width, int(x1 * s) + 1), min(g.height, int(y1 * s) + 1))
        if box[2] - box[0] >= 3 and box[3] - box[1] >= 3 and ImageStat.Stat(g.crop(box)).stddev[0] < 6:
            reasons[i] = "invisible"
    return reasons


def _seen_by_ocr(word, box_px, ocr_words):
    target = _norm(word)
    if not target:
        return False
    for w in ocr_words:
        if _overlap(box_px, w["box"]) > 0.3:
            got = _norm(w["text"])
            if got and difflib.SequenceMatcher(None, target, got).ratio() >= 0.75:
                return True
    return False


def _wordy(text):
    """At least two real words: three or more letters each."""
    return sum(1 for w in text.split() if sum(c.isalpha() for c in w) >= 3) >= 2


def _passages(page_no, words, reasons, boxes_out):
    """Group neighbouring hidden words into passages; each takes its most common reason."""
    out, cur, last = [], None, None
    for (text, x0, y0, x1, y1), reason in zip(words, reasons):
        if reason is None:
            if cur and re.search(r"\w", text):
                cur = None
            continue
        boxes_out.append(((x0, y0, x1, y1), reason))
        # A gap wider than ordinary line spacing, or a jump back up the page, starts a new passage.
        h = max(y1 - y0, 1.0)
        gap = last is not None and (y0 - last[1] > 0.6 * h or y1 < last[0])
        last = (y0, y1)
        if cur and not gap:
            cur["words"].append(text)
            cur["reasons"][reason] = cur["reasons"].get(reason, 0) + 1
        else:
            cur = {"page": page_no, "words": [text], "reasons": {reason: 1}}
            out.append(cur)
    for p in out:
        reasons = p.pop("reasons")
        p["reason"] = max(sorted(reasons), key=lambda r: reasons[r])
        p["text"] = " ".join(p.pop("words"))
        p["word_count"] = len(p["text"].split())
    return out


def analyze(ext, name, packs=None, use_ocr=True):
    """Build the report for an Extraction. packs: pattern packs to apply to visible text."""
    report = {"file": name, "kind": ext.kind, "source_ext": ext.source_ext, "pages": len(ext.pages), "fragments": [],
              "truncated_pages": ext.truncated_pages, "sandboxed": ext.sandboxed, "ink_checked": ext.ink_checked,
              "hidden": [], "uncertain": [], "word": [], "invisible_chars": [], "layers_off": ext.layers_off,
              "active_content": ext.markers, "metadata": ext.metadata, "metadata_ai": [],
              "visible_ai_pages": [], "scanned_pages": [], "notes": list(ext.notes)}
    hidden_boxes = {}
    page_text_visible = {}
    for i, page in enumerate(ext.pages):
        words = ext.words[i]
        reasons, disagree = classify_words(words, page)
        if disagree:
            report["notes"].append("Page %d: Ghostscript could not draw %d word%s that Poppler draws (a font "
                                   "problem, not hidden text)." % (i + 1, len(disagree), "" if len(disagree) == 1 else "s"))
        if page["ink"] is None and words:
            key = "ppm" if page.get("ppm") else "grey"
            reasons = _fallback_blank(words, reasons, ext.image(i, key), page["dpi"])
        # A scan's searchable text layer, or a picture's OCR layer, sits under or over a picture,
        # so its words draw nothing of their own. OCR of the page settles whether the picture
        # shows the same words there.
        candidates = [j for j, r in enumerate(reasons) if r in ("covered", "invisible")]
        ocr_words, ocr_dpi = None, page["dpi"]
        if use_ocr and (candidates or ext.kind == "image"):
            for key, dpi in (("hires", page.get("hires_dpi")), ("ppm", page["dpi"]), ("grey", page["dpi"])):
                img = ext.image(i, key) if page.get(key) else None
                if img is not None:
                    ocr_dpi = dpi
                    ocr_words = ocr.ocr_image(img, dpi, os.path.join(ext.workdir, "ocr-%03d.png" % (i + 1)))
                    break
        if candidates and ocr_words is not None:
            s = ocr_dpi / 72.0
            seen, shows_text = [], []
            for j in candidates:
                box = tuple(int(v * s) for v in words[j][1:])
                if _seen_by_ocr(words[j][0], box, ocr_words):
                    seen.append(j)
                elif reasons[j] == "covered" and any(_overlap(box, w["box"]) > 0.3 for w in ocr_words):
                    shows_text.append(j)
            for j in seen:
                reasons[j] = None
            if len(seen) >= 5 and len(seen) >= len(candidates) - len(seen):
                # Mostly a scan's text layer: what OCR could not confirm under the picture is
                # probably OCR error on one side or the other.
                report["scanned_pages"].append(i + 1)
                shows_text = [j for j in candidates if reasons[j] == "covered"]
            if shows_text:
                # The picture shows text where these words sit, but OCR reads it differently:
                # most likely the picture's own OCR layer, read badly. Listed, not counted,
                # unless the words read like instructions to an AI.
                text = " ".join(words[j][0] for j in shows_text)
                if patterns.hits(text):
                    pass
                else:
                    report["uncertain"].append({"page": i + 1, "text": text, "word_count": len(shows_text)})
                    for j in shows_text:
                        reasons[j] = None
        elif candidates and not use_ocr and any(reasons[j] == "covered" for j in candidates):
            report["notes"].append("OCR was off, so words that may belong to a scan's text layer were not "
                                   "cross-checked.")
        boxes = []
        report["hidden"] += _passages(i + 1, words, reasons, boxes)
        if i < len(ext.offpage) and re.search(r"\w", ext.offpage[i]):
            report["hidden"].append({"page": i + 1, "reason": "off_page", "text": ext.offpage[i],
                                     "word_count": len(ext.offpage[i].split())})
        hidden_boxes[i] = boxes
        visible = " ".join(w[0] for w, r in zip(words, reasons) if r is None)
        if ocr_words and ext.kind == "image":
            visible += " " + " ".join(w["text"] for w in ocr_words if w["conf"] >= 60)
        page_text_visible[i + 1] = visible

    if len(report["scanned_pages"]) >= max(2, 0.5 * len(ext.pages)):
        # A scanned document: drawing sheets and figure pages in it carry the same kind of
        # OCR layer even where OCR of the page confirms too few words to tell on its own.
        keep = []
        for p in report["hidden"]:
            if p["reason"] == "covered" and not patterns.hits(p["text"]):
                report["uncertain"].append({"page": p["page"], "text": p["text"], "word_count": p["word_count"]})
            else:
                keep.append(p)
        report["hidden"] = keep
        report["uncertain"].sort(key=lambda u: u["page"])
    all_hidden = " ".join(p["text"] for p in report["hidden"])
    for p in report["hidden"]:
        p["ai"] = patterns.hits(p["text"])
    # Stray numbers and letters (old OCR layers in figures leave plenty) cannot carry an
    # instruction. They are summarised, not counted, unless they read like one together.
    report["fragments"] = [p for p in report["hidden"] if not _wordy(p["text"]) and not p["ai"]]
    report["hidden"] = [p for p in report["hidden"] if _wordy(p["text"]) or p["ai"]]
    report["hidden_ai"] = patterns.hits(all_hidden)
    for f in ext.word_findings:
        report["word"].append(dict(f, ai=patterns.hits(f["text"])))
    report["invisible_chars"] = chars.find(ext.text)
    for f in report["invisible_chars"]:
        f["ai"] = patterns.hits(f["decoded"] or "")
    report["metadata_ai"] = patterns.hits(" ".join(ext.metadata.values()))
    for page_no, text in page_text_visible.items():
        if patterns.hits(text, packs or []):
            report["visible_ai_pages"].append(page_no)
    if report["scanned_pages"]:
        report["notes"].append("Scanned pages with a searchable text layer: %s. Their text layer was checked "
                               "against OCR of the page." % ", ".join(map(str, report["scanned_pages"])))
    report["verdict"] = verdict(report)
    return report, hidden_boxes


def verdict(r):
    """'hidden' when the file carries text a reader cannot see, 'active' when it has only
    active or linked content, else 'clean'."""
    if r["hidden"] or r["invisible_chars"] or r["layers_off"] or r["metadata_ai"]:
        return "hidden"
    if any(f["kind"] in ("hidden", "deleted") or f["ai"] for f in r.get("word", [])):
        return "hidden"
    if r["active_content"]:
        return "active"
    return "clean"


def analyze_text(text, name, packs=None):
    """Plain-text files: invisible characters and AI-addressed wording only."""
    found = chars.find(text)
    for f in found:
        f["ai"] = patterns.hits(f["decoded"] or "")
    r = {"file": name, "kind": "text", "source_ext": os.path.splitext(name)[1].lower(), "pages": 0,
         "truncated_pages": 0, "sandboxed": None, "ink_checked": None, "hidden": [], "uncertain": [], "word": [],
         "invisible_chars": found, "layers_off": 0, "active_content": [], "metadata": {}, "metadata_ai": [],
         "visible_ai_pages": [1] if patterns.hits(text, packs or []) else [], "scanned_pages": [], "notes": [],
         "fragments": [],
         "hidden_ai": []}
    r["verdict"] = verdict(r)
    return r
