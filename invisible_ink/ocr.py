"""Tesseract OCR: word boxes for the visibility cross-check, and the rebuilt page's text layer."""

import csv
import io
import os
import subprocess

ENV = dict(os.environ, OMP_THREAD_LIMIT="1")


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


def ocr_words(image_path, lang="eng"):
    """Word boxes in pixels for an image file we wrote ourselves."""
    r = subprocess.run(["tesseract", image_path, "stdout", "-l", lang, "--psm", "3", "tsv"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV, timeout=300)
    return parse_tsv(r.stdout.decode("utf-8", "replace"))


def ocr_image(img, dpi, scratch, max_dpi=300, lang="eng"):
    """OCR a PIL image, reading it at no more than max_dpi (Tesseract does best near 300),
    and return word boxes in the image's own pixels."""
    f = max(1.0, dpi / float(max_dpi))
    small = img if f == 1.0 else img.resize((int(img.width / f), int(img.height / f)))
    small.save(scratch)
    try:
        words = ocr_words(scratch, lang)
    finally:
        os.remove(scratch)
    if f != 1.0:
        for w in words:
            x0, y0, x1, y1 = w["box"]
            w["box"] = (int(x0 * f), int(y0 * f), int(x1 * f) + 1, int(y1 * f) + 1)
    return words


def ocr_page_pdf(jpeg_path, outbase, lang="eng", psm=3):
    """Searchable one-page PDF (image + invisible OCR text) plus its TSV, from our JPEG."""
    r = subprocess.run(["tesseract", jpeg_path, outbase, "-l", lang, "--psm", str(psm), "pdf", "tsv"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=ENV, timeout=300)
    pdf, tsv = outbase + ".pdf", outbase + ".tsv"
    if r.returncode != 0 or not os.path.exists(pdf):
        raise RuntimeError("OCR failed: " + r.stderr.decode("utf-8", "replace")[-300:])
    with open(tsv, encoding="utf-8", errors="replace") as f:
        return pdf, parse_tsv(f.read())
