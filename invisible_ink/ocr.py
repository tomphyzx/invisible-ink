"""Tesseract OCR: word boxes for the visibility cross-check, and the rebuilt page's text layer."""

import csv
import io
import os
import re
import subprocess

from PIL import Image, ImageDraw

ENV = dict(os.environ, OMP_THREAD_LIMIT="1")
# Tesseract's table finder works against us once the rules are gone from a table: it splits the
# cells' short lines apart and loses single digits.
NO_TABLE_FINDER = ("-c", "textord_tabfind_find_tables=0")
RULE_DARK = 190          # a pixel darker than this can be part of a table rule
RULE_MIN_H_IN = 0.5      # shortest horizontal rule, in inches
RULE_MIN_V_IN = 0.35     # shortest vertical rule; table rules run the height of several rows
RULE_MAX_THICK_IN = 0.06 # anything thicker is a filled shape or a large letter, not a rule
RULE_MIN_COUNT = 3       # fewer rules than this is a letterhead or a signature line, not a table


def parse_tsv(text):
    words = []
    reader = csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader, None)
    if not header:
        return words
    idx = {k: i for i, k in enumerate(header)}
    for row in reader:
        if len(row) < len(header):
            continue
        txt = row[idx["text"]].strip()
        if not txt or row[idx["level"]] != "5":
            continue
        try:
            conf = float(row[idx["conf"]])
        except ValueError:
            conf = -1.0
        x, y = int(row[idx["left"]]), int(row[idx["top"]])
        w, h = int(row[idx["width"]]), int(row[idx["height"]])
        words.append({"text": txt, "box": (x, y, x + w, y + h), "conf": conf,
                      "line": ("ocr", row[idx["block_num"]], row[idx["par_num"]], row[idx["line_num"]])})
    return words


def _rule_bands(grey, min_run, max_thick):
    """Horizontal rules in a greyscale image: (x0, y0, x1, y1) with x1, y1 exclusive."""
    w, h = grey.size
    pixels = grey.point(lambda v: 0 if v < RULE_DARK else 255).tobytes()
    long_run = re.compile(b"\\x00{%d,}" % min_run)
    bands, cur = [], None
    for y in range(h):
        runs = [(m.start(), m.end()) for m in long_run.finditer(pixels, y * w, (y + 1) * w)]
        if runs:
            spans = [(a - y * w, b - y * w) for a, b in runs]
            if cur is not None:
                cur["y1"] = y + 1
                cur["spans"] += spans
            else:
                cur = {"y0": y, "y1": y + 1, "spans": spans}
        elif cur is not None:
            bands.append(cur)
            cur = None
    if cur is not None:
        bands.append(cur)
    return [(min(a for a, _ in b["spans"]), b["y0"], max(e for _, e in b["spans"]), b["y1"]) for b in bands
            if b["y1"] - b["y0"] <= max_thick]


def remove_rules(img, dpi):
    """A copy of a page with its table rules painted out, for OCR only, and how many it found.

    Rules touching digits are what wreck OCR of a table: a border beside "$293.60" gets read as
    part of it. Returns (img, 0) when the page has too few rules to be a table."""
    grey = img.convert("L")
    max_thick = max(1, int(RULE_MAX_THICK_IN * dpi))
    horizontal = _rule_bands(grey, max(1, int(RULE_MIN_H_IN * dpi)), max_thick)
    vertical = [(y0, x0, y1, x1) for x0, y0, x1, y1 in               # the transpose swaps x and y
                _rule_bands(grey.transpose(Image.TRANSPOSE), max(1, int(RULE_MIN_V_IN * dpi)), max_thick)]
    if len(horizontal) + len(vertical) < RULE_MIN_COUNT:
        return img, 0
    out = img.copy()
    d = ImageDraw.Draw(out)
    for x0, y0, x1, y1 in horizontal + vertical:
        # One pixel beyond the rule, to take its grey anti-aliased edges too. Filled with the
        # colour just outside the rule, so rules on a tinted page do not leave pale stripes.
        pad = (max(0, x0 - 1), max(0, y0 - 1), x1 + 1, y1 + 1)
        ring = out.crop((pad[0], max(0, pad[1] - 3), pad[2], pad[1])).getcolors(1 << 16) or [(1, (255, 255, 255))]
        d.rectangle((pad[0], pad[1], pad[2] - 1, pad[3] - 1), fill=max(ring)[1])
    return out, len(horizontal) + len(vertical)


def ocr_words(image_path, lang="eng", table=False):
    """Word boxes in pixels for an image file we wrote ourselves."""
    r = subprocess.run(["tesseract", image_path, "stdout", "-l", lang, "--psm", "3"]
                       + list(NO_TABLE_FINDER if table else ()) + ["tsv"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV, timeout=300)
    return parse_tsv(r.stdout.decode("utf-8", "replace"))


def read_crop(img, box, scratch, digits=False, lang="eng"):
    """A second, separate reading of one word: its box cut out, enlarged, read as a single line.
    box is in the image's own pixels. digits: read only digits and the marks around amounts."""
    x0, y0, x1, y1 = box
    pad = max(3, (y1 - y0) // 4)
    crop = img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))
    crop = crop.convert("L").resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
    framed = Image.new("L", (crop.width + 40, crop.height + 40), 255)
    framed.paste(crop, (20, 20))
    framed.save(scratch)
    cmd = ["tesseract", scratch, "stdout", "-l", lang, "--psm", "7"]
    if digits:
        cmd += ["-c", "tessedit_char_whitelist=0123456789$,.%-()"]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV, timeout=60)
    finally:
        os.remove(scratch)
    return " ".join(r.stdout.decode("utf-8", "replace").split())


def ocr_image(img, dpi, scratch, max_dpi=300, lang="eng", table=False):
    """OCR a PIL image, reading it at no more than max_dpi (Tesseract does best near 300),
    and return word boxes in the image's own pixels."""
    f = max(1.0, dpi / float(max_dpi))
    small = img if f == 1.0 else img.resize((int(img.width / f), int(img.height / f)))
    small.save(scratch)
    try:
        words = ocr_words(scratch, lang, table=table)
    finally:
        os.remove(scratch)
    if f != 1.0:
        for w in words:
            x0, y0, x1, y1 = w["box"]
            w["box"] = (int(x0 * f), int(y0 * f), int(x1 * f) + 1, int(y1 * f) + 1)
    return words


def ocr_page_pdf(jpeg_path, outbase, lang="eng", psm=3, table=False):
    """Searchable one-page PDF (image + invisible OCR text) plus its TSV, from our JPEG."""
    r = subprocess.run(["tesseract", jpeg_path, outbase, "-l", lang, "--psm", str(psm)]
                       + list(NO_TABLE_FINDER if table else ()) + ["pdf", "tsv"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=ENV, timeout=300)
    pdf, tsv = outbase + ".pdf", outbase + ".tsv"
    if r.returncode != 0 or not os.path.exists(pdf):
        raise RuntimeError("OCR failed: " + r.stderr.decode("utf-8", "replace")[-300:])
    with open(tsv, encoding="utf-8", errors="replace") as f:
        return pdf, parse_tsv(f.read())


def swap_page_image(pdf_path, ocr_jpeg, shown_jpeg):
    """Put the page image the reader should see into a PDF Tesseract made from another JPEG.

    Tesseract embeds its input JPEG unchanged, so the OCR can run on a copy with the table rules
    painted out while the page still shows them. Both files are ours and have the same size in
    pixels. Raises RuntimeError if the PDF is not laid out as expected."""
    with open(pdf_path, "rb") as f:
        data = f.read()
    with open(ocr_jpeg, "rb") as f:
        old = f.read()
    with open(shown_jpeg, "rb") as f:
        new = f.read()
    at = data.find(old)
    if at < 0 or data.find(old, at + 1) >= 0:
        raise RuntimeError("The OCR page image was not found once in the PDF.")
    length_at = data.rfind(b"/Length %d" % len(old), 0, at)
    start = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data)
    if length_at < 0 or start is None:
        raise RuntimeError("Unexpected PDF layout.")
    xref_at = int(start.group(1))
    table = re.match(rb"xref\s+0\s+(\d+)\s+", data[xref_at:])
    if xref_at <= at or table is None:
        raise RuntimeError("Unexpected PDF layout.")
    old_field, new_field = b"/Length %d" % len(old), b"/Length %d" % len(new)
    grew = len(new) - len(old) + len(new_field) - len(old_field)
    entries_at = xref_at + table.end()
    count = int(table.group(1))
    rows = []
    for n in range(count):
        row = data[entries_at + n * 20: entries_at + (n + 1) * 20]
        offset = int(row[:10])
        rows.append(b"%010d" % (offset + grew if offset > at else offset) + row[10:])
    out = (data[:length_at] + new_field + data[length_at + len(old_field):at] + new
           + data[at + len(old):entries_at] + b"".join(rows) + data[entries_at + count * 20:start.start()]
           + b"startxref\n%d\n%%%%EOF\n" % (xref_at + grew))
    with open(pdf_path, "wb") as f:
        f.write(out)
