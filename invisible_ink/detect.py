"""Find what a reader cannot see but a text extractor (and so an AI) will read.

The main test is direct: each page is drawn twice, with and without its text. A word
in the text layer that changes no pixels between the two cannot be seen, whatever the
trick (white on white, text in a colour matching its background, PDF's invisible text
mode, a shape or image drawn over it, a clip, a switched-off layer). A word that changes
pixels only slightly is faint. Words under 3 pt or outside the page are caught by size
and position.
"""

import concurrent.futures as cf
import difflib
import os
import re

from PIL import ImageStat

from . import chars, ocr, patterns
from .worker import NO_GHOSTSCRIPT, tool

TINY_PT = 3.0
# Largest change a word makes in any colour channel, out of 255, below which it is "faint".
# Hiding tricks sit within about 35 of the background (very light grey on white); white labels
# on light grey or light blue title bars change about 50 and are plainly readable.
FAINT_MAX = 40
NO_INK_FRACTION = 0.002  # a word changing fewer pixels than this share of its box draws nothing
BLANK_SD10 = 60          # contrast of the area x10: below it, the area is a flat colour

REASONS = {
    "invisible": "invisible: leaves no mark on the page",
    "covered": "covered by an image or shape",
    "faint": "faint: barely different from the background",
    "tiny": "too small to read (under 3 pt)",
    "off_page": "outside the page",
}
NO_TESSERACT = "Tesseract was not found, so the text layer was not compared with what the pages show."
INK_FAILED = "The check for covered and faint text failed on this page; only a weaker check ran."
SOFTWARE_FIELDS = {"Creator", "Producer", "Make", "Model"}
WORD_KINDS = {"hidden": "Text marked hidden in Word", "deleted": "Deleted text kept by tracked changes",
              "comment": "Comment", "alt_text": "Image alt text"}


def _norm(s):
    return re.sub(r"[^\w]", "", s.lower())


def _overlap(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))
    return ix * iy / area


# ----------------------------------------------------- text layer against the page itself

MIN_CONF_NUMBER = 85     # how sure OCR must be of a number before it can contradict the text layer
MIN_CONF_WORD = 85
WORD_MIN_LETTERS = 5
WORD_MAX_RATIO = 0.5     # similarity of text-layer word and OCR word below which they differ
WORD_MIN_FOUND = 3       # one odd word in an unusual font is not a finding
WORD_MIN_SHARE = 0.2
MAX_REREADS = 40         # per page; a page wrong in more places than this is already reported
LOOKALIKE = str.maketrans("OoQDIilLSZB", "00001111528")   # letters OCR and fonts swap with digits
AMOUNT = re.compile(r"^[(\-+$£€]*\d[\d,.]*[%)]?$")


def is_amount(text):
    """A plain number or money figure, not a code with letters in it."""
    return bool(AMOUNT.match(text))


def digits(text):
    return re.sub(r"\D", "", text)


def ocr_at(box_px, ocr_words):
    """The OCR words sitting on a text-layer word's box, left to right."""
    x0, y0, x1, y1 = box_px
    hits = []
    for w in ocr_words:
        a, b, c, d = w["box"]
        ix, iy = min(x1, c) - max(x0, a), min(y1, d) - max(y0, b)
        if ix > 0 and iy > 0 and ix >= 0.5 * min(x1 - x0, c - a) and iy >= 0.5 * min(y1 - y0, d - b):
            hits.append(w)
    return sorted(hits, key=lambda w: w["box"][0])


def _number_differs(text, shown):
    """Do these two readings, of equal digit count, name a different number?"""
    a, b = digits(text), digits(shown)
    if len(a) != len(b) or a == b:
        return False
    if is_amount(text):
        return True
    return text.translate(LOOKALIKE) != shown.translate(LOOKALIKE)      # a code: O and 0 are not news


def _compare_layer(page_no, words, reasons, scan_layer, ocr_words, img, dpi, scratch):
    """Text-layer words that do not match what the page shows, and the numbers OCR read the
    same as the text layer. Two separate readings of the page must agree against the text
    layer before anything is reported."""
    s = dpi / 72.0
    found, letters_seen, agreed = [], 0, set()
    rereads = 0

    def px(j):
        return (int(words[j][1] * s), int(words[j][2] * s), int(words[j][3] * s) + 1, int(words[j][4] * s) + 1)

    def report_item(j, kind, shown):
        found.append({"page": page_no, "kind": kind, "text_layer": words[j][0], "page_shows": shown,
                      "scan_layer": j in scan_layer})

    word_hits = []
    for j, (text, *_rest) in enumerate(words):
        if reasons[j] is not None:
            continue
        got = ocr_at(px(j), ocr_words)
        if not got:
            continue
        shown = "".join(w["text"] for w in got)
        conf = min(w["conf"] for w in got)
        if len(digits(text)) >= 2:
            if digits(shown) == digits(text):
                agreed.add(text)
                continue
            if conf < MIN_CONF_NUMBER or not _number_differs(text, shown):
                continue
            if rereads >= MAX_REREADS:
                continue
            rereads += 1
            again = ocr.read_crop(img, px(j), scratch, digits=is_amount(text))
            if digits(again) == digits(shown):
                report_item(j, "number", shown)
        elif text.isalpha() and text.isascii() and len(text) >= WORD_MIN_LETTERS and j not in scan_layer:
            # Plain letters only: English OCR reads accented and non-Latin words as something else.
            letters_seen += 1
            if len(got) == 1 and conf >= MIN_CONF_WORD and shown.isalpha():
                a, b = text.lower(), shown.lower()
                if difflib.SequenceMatcher(None, a, b).ratio() < WORD_MAX_RATIO:
                    word_hits.append((j, shown))
    # Words need many on one page to count: OCR misreads an unusual font now and then, but a
    # font whose table swaps letters is wrong everywhere.
    if letters_seen and len(word_hits) >= max(WORD_MIN_FOUND, int(WORD_MIN_SHARE * letters_seen)):
        confirmed = []
        for j, shown in word_hits[:MAX_REREADS]:
            again = ocr.read_crop(img, px(j), scratch)
            if difflib.SequenceMatcher(None, shown.lower(), again.lower()).ratio() >= 0.8:
                confirmed.append((j, shown))
        if len(confirmed) >= max(WORD_MIN_FOUND, int(WORD_MIN_SHARE * letters_seen)):
            for j, shown in confirmed:
                report_item(j, "word", shown)
    return found, agreed


def _page_for_ocr(ext, i):
    """The sharpest picture of a page, for OCR: (image, dpi)."""
    page = ext.pages[i]
    for key, dpi in (("hires", page.get("hires_dpi")), ("ppm", page["dpi"]), ("grey", page["dpi"])):
        img = ext.image(i, key) if page.get(key) else None
        if img is not None:
            return img, dpi
    return None, page["dpi"]


def number_differences(page_no, words, skip_boxes, ocr_words, dpi):
    """Every number whose OCR differs from the text layer: for a person to check against the
    page image. Unlike the scan check this reports each difference, misreadings included."""
    s = dpi / 72.0
    out = []
    for text, x0, y0, x1, y1 in words:
        d = digits(text)
        if not d or (x0, y0, x1, y1) in skip_boxes:
            continue
        got = ocr_at((int(x0 * s), int(y0 * s), int(x1 * s) + 1, int(y1 * s) + 1), ocr_words)
        shown = " ".join(w["text"] for w in got)
        if digits(shown) != d:
            out.append({"page": page_no, "kind": "number" if is_amount(text) else "code", "original": text,
                        "ocr": shown or None, "confidence": min((w["conf"] for w in got), default=None),
                        "box_pt": [round(v, 1) for v in (x0, y0, x1, y1)]})
    return out


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


def analyze(ext, name, packs=None, use_ocr=True, check_text_layer=True):
    """Build the report for an Extraction. packs: pattern packs to apply to visible text.
    check_text_layer: read printed PDF pages by OCR and compare them with their text layer."""
    report = {"file": name, "kind": ext.kind, "source_ext": ext.source_ext, "pages": len(ext.pages), "fragments": [],
              "truncated_pages": ext.truncated_pages, "sandboxed": ext.sandboxed, "ink_checked": ext.ink_checked,
              "hidden": [], "uncertain": [], "word": [], "invisible_chars": [], "layers_off": ext.layers_off,
              "active_content": ext.markers, "metadata": ext.metadata, "metadata_ai": [],
              "visible_ai_pages": [], "scanned_pages": [], "notes": list(ext.notes), "text_mismatch": [],
              "incomplete_pages": []}

    def incomplete(page, reason):
        """Record a check that did not run or did not finish. page is None for the whole file."""
        entry = {"page": page, "reason": reason}
        if entry not in report["incomplete_pages"]:
            report["incomplete_pages"].append(entry)

    hidden_boxes = {}
    page_text_visible = {}
    layer_ocr, agreed = {}, set()
    compare = ext.kind == "pdf" and check_text_layer
    if compare and not use_ocr:
        report["notes"].append("OCR was off, so the text layer was not compared with what the pages show.")
        compare = False
    elif compare and not tool("tesseract"):
        report["notes"].append(NO_TESSERACT)
        if any(ext.words):
            incomplete(None, NO_TESSERACT)
        compare = False
    if compare:
        def read_page(i):
            img, dpi = _page_for_ocr(ext, i)
            if img is None:
                return i, "No picture of the page could be made."
            cleared, rules = ocr.remove_rules(img, dpi)
            try:
                return i, (ocr.ocr_image(cleared, dpi, os.path.join(ext.workdir, "cmp-%03d.png" % (i + 1)),
                                         table=rules > 0), dpi, cleared)
            except ocr.OcrFailed as e:
                return i, "%s." % e
        wanted = [i for i in range(len(ext.pages)) if ext.words[i]]
        with cf.ThreadPoolExecutor(max(1, min(4, os.cpu_count() or 2))) as pool:
            for i, got in pool.map(read_page, wanted):
                if isinstance(got, str):
                    incomplete(i + 1, "The text layer was not compared with the page: " + got)
                else:
                    layer_ocr[i] = got
    for i, page in enumerate(ext.pages):
        words = ext.words[i]
        reasons, disagree = classify_words(words, page)
        if disagree:
            report["notes"].append("Page %d: Ghostscript could not draw %d word%s that Poppler draws (a font "
                                   "problem, not hidden text)." % (i + 1, len(disagree), "" if len(disagree) == 1 else "s"))
        if page["ink"] is None and words:
            incomplete(*((i + 1, INK_FAILED) if ext.ink_checked else (None, NO_GHOSTSCRIPT)))
            key = "ppm" if page.get("ppm") else "grey"
            reasons = _fallback_blank(words, reasons, ext.image(i, key), page["dpi"])
        # A scan's searchable text layer, or a picture's OCR layer, sits under or over a picture,
        # so its words draw nothing of their own. OCR of the page settles whether the picture
        # shows the same words there.
        candidates = [j for j, r in enumerate(reasons) if r in ("covered", "invisible")]
        ocr_words, ocr_dpi = None, page["dpi"]
        if layer_ocr.get(i):
            ocr_words, ocr_dpi = layer_ocr[i][:2]
        elif use_ocr and (candidates or ext.kind == "image"):
            img, ocr_dpi = _page_for_ocr(ext, i)
            if img is not None:
                try:
                    ocr_words = ocr.ocr_image(img, ocr_dpi, os.path.join(ext.workdir, "ocr-%03d.png" % (i + 1)))
                except ocr.OcrFailed as e:
                    incomplete(i + 1, "The page could not be read by OCR: %s." % e)
        seen = []
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
        if layer_ocr.get(i):
            try:
                found, same = _compare_layer(i + 1, words, reasons, set(seen), ocr_words, layer_ocr[i][2], ocr_dpi,
                                             os.path.join(ext.workdir, "crop-%03d.png" % (i + 1)))
                report["text_mismatch"] += found
                agreed |= same
            except ocr.OcrFailed as e:
                incomplete(i + 1, "The text layer was not fully compared with the page: %s." % e)
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

    # A font table that misreads a number misreads it every time. The same number read correctly
    # elsewhere in the file means this one was a passing OCR slip.
    report["text_mismatch"] = [m for m in report["text_mismatch"] if m["kind"] != "number" or m["text_layer"] not in agreed]
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
        # Word marks pasted web forms with hidden "Top of Form" / "Bottom of Form" text itself.
        if f["kind"] == "hidden":
            f = dict(f, text=re.sub(r"(Top|Bottom) of Form", " ", f["text"]).strip())
            if not f["text"]:
                continue
        report["word"].append(dict(f, ai=patterns.hits(f["text"])))
    report["invisible_chars"] = chars.find(chars.without_extractor_marks(ext.text) if ext.kind == "pdf" else ext.text)
    for f in report["invisible_chars"]:
        f["ai"] = patterns.hits(f["decoded"] or "")
    # Creator and Producer name the software that made the file ("ChatGPT", "Microsoft Word"),
    # and Make and Model the camera: not places anyone writes an instruction.
    report["metadata_ai"] = patterns.hits(" ".join(v for k, v in ext.metadata.items() if k not in SOFTWARE_FIELDS))
    for page_no, text in page_text_visible.items():
        if patterns.hits(text, packs or []):
            report["visible_ai_pages"].append(page_no)
    if report["scanned_pages"]:
        report["notes"].append("Scanned pages with a searchable text layer: %s. Their text layer was checked "
                               "against OCR of the page." % ", ".join(map(str, report["scanned_pages"])))
    report["incomplete_pages"].sort(key=lambda e: e["page"] or 0)
    report["verdict"] = verdict(report)
    return report, hidden_boxes


def verdict(r):
    """'incomplete' when a check could not run or finish, whatever else was found: it needs a
    person, and the findings are still in the report. Otherwise 'hidden' when the file carries
    text a reader cannot see, 'mismatch' when its text layer says something its printed pages
    do not, 'active' when it has only active or linked content, else 'clean'. A scan's own OCR
    layer disagreeing with the picture is listed but does not count: that is usually OCR error."""
    if r.get("incomplete_pages"):
        return "incomplete"
    if r["hidden"] or r["invisible_chars"] or r["layers_off"] or r["metadata_ai"]:
        return "hidden"
    if any(f["kind"] in ("hidden", "deleted") or f["ai"] for f in r.get("word", [])):
        return "hidden"
    if any(not m["scan_layer"] for m in r.get("text_mismatch", [])):
        return "mismatch"
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
         "fragments": [], "text_mismatch": [], "incomplete_pages": [],
         "hidden_ai": []}
    r["verdict"] = verdict(r)
    return r
