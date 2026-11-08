#!/usr/bin/env python3
"""How accurately do numbers survive `clean`? Measured on synthetic bordered-table quotes.

    python3 tools/measure_digit_accuracy.py [--repo CHECKOUT] [--seeds 7,11,23,31,47] [PDF...]

Builds the quotes with this checkout's tests/make_samples.py, runs `clean` from CHECKOUT (default:
this one, so another checkout gives a before-and-after), and compares each digit-bearing word
of the original's text layer with the cleaned file's OCR layer at the same place on the page.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))

import make_samples  # noqa: E402
from invisible_ink.sandbox import read_words  # noqa: E402


def words_of(pdf, tmp):
    out = os.path.join(tmp, "words.html")
    subprocess.run(["pdftotext", "-bbox", "-enc", "UTF-8", pdf, out], check=True)
    return read_words(out)


def at(word, others):
    _, x0, y0, x1, y1 = word
    hits = []
    for o in others:
        ix, iy = min(x1, o[3]) - max(x0, o[1]), min(y1, o[4]) - max(y0, o[2])
        if ix > 0 and iy > 0 and ix >= 0.5 * min(x1 - x0, o[3] - o[1]) and iy >= 0.5 * min(y1 - y0, o[4] - o[2]):
            hits.append(o)
    return " ".join(o[0] for o in sorted(hits, key=lambda o: o[1]))


def kind(text):
    if re.fullmatch(r"[$]?[\d,]+(\.\d+)?", text):
        return "amount" if len(re.sub(r"\D", "", text)) > 1 else "lone digit"
    return "code"


def score(original, cleaned, tally, tmp):
    src, new = words_of(original, tmp), words_of(cleaned, tmp)
    for page, other in zip(src, new):
        for w in page:
            if re.search(r"\d", w[0]):
                t = tally.setdefault(kind(w[0]), [0, 0, 0])
                got = at(w, other)
                t[0] += 1
                t[1] += got == w[0]
                t[2] += re.sub(r"\D", "", got) == re.sub(r"\D", "", w[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=HERE, help="checkout whose `clean` to measure")
    ap.add_argument("--seeds", default="7,11,23,31,47")
    ap.add_argument("files", nargs="*", help="extra PDFs to measure")
    a = ap.parse_args()
    tally = {}
    with tempfile.TemporaryDirectory() as tmp:
        pdfs = list(a.files)
        for s in a.seeds.split(","):
            pdf = os.path.join(tmp, "quote%s.pdf" % s)
            make_samples.quote(pdf, seed=int(s))
            pdfs.append(pdf)
        for pdf in pdfs:
            out = os.path.join(tmp, os.path.basename(pdf) + "-out.pdf")
            subprocess.run([sys.executable, os.path.join(a.repo, "bin", "invisible-ink"), "clean", "-o", out, pdf],
                           check=True, stdout=subprocess.DEVNULL)
            score(pdf, out, tally, tmp)
    n = e = d = 0
    print("%-11s %6s %16s %16s" % ("", "tokens", "exact", "digits right"))
    for k in ("amount", "lone digit", "code"):
        t = tally.get(k, [0, 0, 0])
        n, e, d = n + t[0], e + t[1], d + t[2]
        print("%-11s %6d %8d (%3.0f%%) %8d (%3.0f%%)" % (k, t[0], t[1], 100 * t[1] / max(t[0], 1), t[2],
                                                         100 * t[2] / max(t[0], 1)))
    print("%-11s %6d %8d (%3.0f%%) %8d (%3.0f%%)" % ("all", n, e, 100 * e / max(n, 1), d, 100 * d / max(n, 1)))


if __name__ == "__main__":
    main()
