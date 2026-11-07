"""Sandboxed extraction worker.

This script is the only code that ever opens the original document. On Linux it
runs inside a bubblewrap sandbox with no network, no access to the home folder,
and only two folders visible: /work/in (the file, read-only) and /work/out.

    python3 -I worker.py SOURCE OUTDIR MODE        MODE is "scan" or "clean"

It writes, into OUTDIR:
  manifest.json   page list and sizes, file properties, active-content markers,
                  Word-structure findings, and per-word ink measurements
  pNNN.ppm        clean mode: each page as raw RGB pixels (binary PPM, P6), for the rebuild
  gNNN.pgm        scan mode: each page as raw greyscale pixels at 150 dpi (P5), for OCR
  words.html      the PDF text layer with word boxes (pdftotext -bbox)
  text.txt        all text the file carries, for the invisible-character check

The trusted side reads only those files. It never parses the original document.

Standard library plus Pillow. Run with python3 -I.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # our own read-only folder only
import chars  # noqa: E402

MAX_PAGES = 100
MAX_PIXELS_LONG_SIDE = 5100    # 17 in at 300 dpi, for pages without photos
MAX_PHOTO_LONG_SIDE = 7200     # 12 in at 600 dpi, for pages carrying a photo or scan
TARGET_DPI = 300
MAX_PHOTO_DPI = 600            # draw photo pages at the photo's own resolution, up to this
MIN_PHOTO_PIXELS = 250_000     # smaller images are icons and logos
INK_DPI = 150                  # resolution of the with-text / without-text comparison
MAX_TEXT = 20_000_000
MAX_FINDINGS = 300
# LibreOffice halves embedded photos by default. Keep them whole.
PDF_EXPORT = ('pdf:writer_pdf_Export:{"ReduceImageResolution":{"type":"boolean","value":"false"},'
              '"UseLosslessCompression":{"type":"boolean","value":"true"}}')
OFFICE_EXT = {".docx", ".doc", ".odt", ".rtf", ".docm", ".dotx", ".dot"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
# Markers of active or outward-reaching content in a PDF. Seen in the raw bytes
# only when the object dictionaries are not compressed, so this is a hint.
PDF_MARKERS = [b"/JavaScript", b"/JS", b"/OpenAction", b"/Launch", b"/EmbeddedFile",
               b"/RichMedia", b"/SubmitForm", b"/ImportData", b"/AA", b"/URI", b"/GoToR"]
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WORD_RE = re.compile(r'<word xMin="([-\d.]+)" yMin="([-\d.]+)" xMax="([-\d.]+)" yMax="([-\d.]+)">(.*?)</word>')


def run(cmd, timeout=240):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def tool(name):
    """Find a program, including the usual macOS app locations."""
    if name in os.environ.get("INVISIBLE_INK_WITHOUT", "").split(","):     # for testing fallbacks
        return None
    found = shutil.which(name)
    if found:
        return found
    for path in {"soffice": ["/Applications/LibreOffice.app/Contents/MacOS/soffice"]}.get(name, []):
        if os.path.exists(path):
            return path
    return None


def sniff(path):
    with open(path, "rb") as f:
        first = f.read(1024)
    head = first[:16]
    if b"%PDF-" in first:              # the standard allows bytes before the header
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head[:3] == b"\xff\xd8\xff" or head.startswith(b"\x89PNG") or head[:4] in (b"GIF8", b"II*\x00", b"MM\x00*") \
            or head[:2] == b"BM" or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    return "unknown"


# ------------------------------------------------------------------ Word files

def _text_of(el):
    return "".join(t.text or "" for t in el.iter() if t.tag in (W + "t", W + "delText", W + "instrText"))


def _paragraphs(root):
    return "\n".join(_text_of(p) for p in root.iter(W + "p"))


def word_structure(path, manifest, texts):
    """Things in a Word file that a printed page never shows but a text extractor may read."""
    findings, markers, meta = [], [], {}
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return
    with z:
        names = z.namelist()
        low = [n.lower() for n in names]
        if any(n.endswith("vbaproject.bin") for n in low):
            markers.append("macros (vbaProject.bin)")
        if any("activex" in n for n in low):
            markers.append("ActiveX controls")
        if any(n.startswith("word/embeddings/") for n in low):
            markers.append("embedded objects")
        for n in names:
            if n.endswith(".rels") and z.getinfo(n).file_size < 2_000_000:
                rels = z.read(n).decode("utf-8", "replace")
                for rel in re.findall(r"<Relationship\b[^>]*>", rels):
                    if 'TargetMode="External"' in rel and "/hyperlink" not in rel:
                        markers.append("remote template" if "attachedTemplate" in rel else "externally linked content")
        for part in ("docProps/core.xml", "meta.xml"):
            if part in names and z.getinfo(part).file_size < 1_000_000:
                xml = z.read(part).decode("utf-8", "replace")
                for tag in ("dc:creator", "cp:lastModifiedBy", "dc:title", "meta:initial-creator",
                            "dc:subject", "cp:keywords", "dc:description"):
                    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), xml, re.S)
                    if m and m.group(1).strip():
                        meta[tag] = m.group(1).strip()[:500]
        parts = [n for n in names if re.fullmatch(
            r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml", n)]
        for part in sorted(parts, key=lambda n: (n != "word/document.xml", n)):
            if z.getinfo(part).file_size > 50_000_000:
                continue
            where = part[5:-4]
            try:
                root = ET.fromstring(z.read(part))
            except ET.ParseError:
                continue
            texts.append(_paragraphs(root))
            if where == "comments":
                for c in root.iter(W + "comment"):
                    t = _text_of(c).strip()
                    if t:
                        findings.append({"kind": "comment", "where": where, "text": t})
                continue
            for para in root.iter(W + "p"):
                hidden = []
                for r in para.findall(W + "r") + [r for h in para.findall(W + "hyperlink") for r in h.findall(W + "r")]:
                    rpr = r.find(W + "rPr")
                    if rpr is not None and (rpr.find(W + "vanish") is not None or rpr.find(W + "specVanish") is not None):
                        hidden.append(_text_of(r))
                t = "".join(hidden)
                if t.strip():
                    findings.append({"kind": "hidden", "where": where, "text": t})
            for d in root.iter(W + "del"):
                t = _text_of(d)
                if t.strip():
                    findings.append({"kind": "deleted", "where": where, "text": t})
            for el in root.iter():
                if el.tag.endswith("}docPr") or el.tag.endswith("}cNvPr"):
                    for attr in ("descr", "title"):
                        t = (el.get(attr) or "").strip()
                        if t:
                            findings.append({"kind": "alt_text", "where": where, "text": t})
    merged = []
    for f in findings:
        if not (merged and f["kind"] == "alt_text" and merged[-1] == f):
            merged.append(f)
    for f in merged:
        f["text"] = f["text"][:2000]
    manifest["word_findings"] = merged[:MAX_FINDINGS]
    manifest["office_meta"] = meta
    manifest["markers"] += markers


def final_view(src, dest):
    """A copy of a Word file as its final version: tracked deletions dropped, insertions
    accepted, comment marks and invisible characters removed. LibreOffice would otherwise
    print deletions struck through. The text-carrying parts are edited as text so that
    nothing else in the file changes."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml", info.filename):
                xml = data.decode("utf-8", "replace")
                xml = re.sub(r"<w:del\b[^>]*/>", "", xml)
                xml = re.sub(r"<w:del\b[^>]*>.*?</w:del>", "", xml, flags=re.S)
                xml = re.sub(r"<w:ins\b[^>]*/>|<w:ins\b[^>]*>|</w:ins>", "", xml)
                xml = re.sub(r"<w:comment(Reference|RangeStart|RangeEnd)\b[^>]*/>", "", xml)
                xml = chars.strip(xml)
                data = xml.encode("utf-8")
            zout.writestr(info, data)
    return dest


def to_pdf_with_libreoffice(src, outdir):
    soffice = tool("soffice")
    if not soffice:
        raise RuntimeError("LibreOffice is needed for Word files and was not found.")
    os.makedirs(outdir, exist_ok=True)
    r = run([soffice, "--headless", "--norestore", "--nologo", "--nolockcheck", "--nodefault",
             "-env:UserInstallation=file://%s/lo-profile" % os.path.abspath(outdir), "--convert-to", PDF_EXPORT,
             "--outdir", outdir, src], timeout=300)
    pdfs = [f for f in os.listdir(outdir) if f.lower().endswith(".pdf")]
    if not pdfs:
        raise RuntimeError("LibreOffice could not convert the file: " + r.stderr.decode("utf-8", "replace")[-400:])
    return os.path.join(outdir, pdfs[0])


# ------------------------------------------------------------------------ PDFs

def pdf_info(pdf):
    r = run(["pdfinfo", "-isodates", pdf])
    if r.returncode != 0:
        raise RuntimeError("Not a readable PDF: " + r.stderr.decode("utf-8", "replace")[-300:])
    info = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()[:500]
    return info, int(info.get("Pages", "0") or 0)


def page_sizes(pdf, pages):
    r = run(["pdfinfo", "-f", "1", "-l", str(pages), pdf])
    sizes = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        m = re.match(r"Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", line)
        if m:
            sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
        m = re.match(r"Page\s+(\d+)\s+rot:\s+(\d+)", line)
        if m and int(m.group(2)) in (90, 270) and int(m.group(1)) in sizes:
            w, h = sizes[int(m.group(1))]
            sizes[int(m.group(1))] = (h, w)
    return sizes


def photo_ppi(pdf, pages):
    """Highest resolution (pixels per inch as placed) of any real photo or scan, per page."""
    r = run(["pdfimages", "-list", "-f", "1", "-l", str(pages), pdf])
    best = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines()[2:]:
        f = line.split()
        if len(f) < 14 or f[2] != "image":
            continue
        try:
            page, w, h, xppi, yppi = int(f[0]), int(f[3]), int(f[4]), float(f[12]), float(f[13])
        except ValueError:
            continue
        if w * h >= MIN_PHOTO_PIXELS:
            best[page] = max(best.get(page, 0.0), min(xppi, yppi))
    return best


def layers_off(pdf, raw):
    """Number of optional-content layers (OCGs) switched off when the file opens."""
    mutool = tool("mutool")
    if mutool:
        r = run([mutool, "show", pdf, "Root/OCProperties/D/OFF"])
        out = r.stdout.decode("latin-1", "replace")
        if r.returncode == 0:
            return len(re.findall(r"\d+\s+0\s+R", out))
    m = re.search(rb"/OFF\s*\[([^\]]*)\]", raw)
    return len(re.findall(rb"\d+\s+0\s+R", m.group(1))) if m else 0


def offpage_words(normal, wide, pages):
    """Per page, the words that appear only when reading beyond the page edges."""
    out = []
    for n_page, w_page in zip((normal.split("\f") + [""] * pages)[:pages], (wide.split("\f") + [""] * pages)[:pages]):
        counts = {}
        for w in n_page.split():
            counts[w] = counts.get(w, 0) + 1
        extra = []
        for w in w_page.split():
            if counts.get(w):
                counts[w] -= 1
            else:
                extra.append(w)
        out.append(" ".join(extra)[:5000])
    return out


def read_words(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        doc = f.read()
    return [[tuple(float(m.group(i)) for i in range(1, 5)) for m in WORD_RE.finditer(chunk)]
            for chunk in doc.split("<page ")[1:]]


def _box(b, s, w, h):
    x0, y0, x1, y1 = b
    return (max(0, int(x0 * s)), max(0, int(y0 * s)), min(w, int(x1 * s) + 1), min(h, int(y1 * s) + 1))


def ink_measure(pdf, page, boxes, outdir, keep_grey, entry=None):
    """Draw the page twice, with and without its text, and measure what each word adds.

    For every word: [changed pixels, box area, largest change (0-255), contrast of the area x10,
    contrast of the area in Poppler's drawing x10 (-1 if not needed), changed pixels in the box
    widened upwards]. A word that changes no pixels is in the text layer but cannot be seen."""
    from PIL import Image, ImageChops, ImageStat
    gs = tool("gs")
    a, b = os.path.join(outdir, "ink-a.ppm"), os.path.join(outdir, "ink-b.ppm")
    base = ["-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=ppmraw", "-r%d" % INK_DPI, "-dUseCropBox",
            "-dFirstPage=%d" % page, "-dLastPage=%d" % page]
    r1 = run([gs] + base + ["-sOutputFile=" + a, pdf])
    r2 = run([gs] + base + ["-dFILTERTEXT", "-sOutputFile=" + b, pdf])
    if r1.returncode or r2.returncode or not (os.path.exists(a) and os.path.exists(b)):
        return None
    with Image.open(a) as ia, Image.open(b) as ib:
        ca, cb = ia.convert("RGB"), ib.convert("RGB")
    if ca.size != cb.size:
        return None
    ga = ca.convert("L")
    # The largest change in any colour channel: light blue on white is plainly visible even
    # though its brightness is close to white's.
    red, green, blue = ImageChops.difference(ca, cb).split()
    diff = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    s = INK_DPI / 72.0
    heights = sorted(y1 - y0 for (x0, y0, x1, y1) in boxes) or [0]
    median_h = heights[len(heights) // 2]
    out, blank = [], []
    for k, (x0, y0, x1, y1) in enumerate(boxes):
        box = _box((x0, y0, x1, y1), s, ga.width, ga.height)
        if box[2] <= box[0] or box[3] <= box[1]:
            out.append([0, 0, 0, 0, -1, 0])
            continue
        h = diff.crop(box).histogram()
        area = (box[2] - box[0]) * (box[3] - box[1])
        top = max((i for i, v in enumerate(h) if v), default=0)
        sd = ImageStat.Stat(ga.crop(box)).stddev[0]
        changed = sum(h[13:])
        up = changed
        hgt = y1 - y0
        if changed == 0 and median_h > 0 and hgt > 1.5 * median_h:
            # Some fonts (maths fonts especially) report boxes far taller than their glyphs,
            # and offset from them. Look in the box widened upwards too.
            wide = _box((x0, y0 - 0.6 * hgt, x1, y1), s, ga.width, ga.height)
            up = sum(diff.crop(wide).histogram()[13:])
        out.append([changed, area, top, int(sd * 10), -1, up])
        if changed == 0 and sd < 6:
            blank.append(k)
    if blank:
        # A word that draws nothing over a flat area should look the same in Poppler's drawing.
        # If Poppler shows ink there, Ghostscript failed to draw that font: not hidden text.
        pbase = os.path.join(outdir, "ink-p")
        run(["pdftoppm", "-gray", "-r", str(INK_DPI), "-f", str(page), "-l", str(page), "-singlefile", pdf, pbase])
        if os.path.exists(pbase + ".pgm"):
            with Image.open(pbase + ".pgm") as ip:
                gp = ip.convert("L")
            for k in blank:
                box = _box(boxes[k], s, gp.width, gp.height)
                if box[2] > box[0] and box[3] > box[1]:
                    out[k][4] = int(ImageStat.Stat(gp.crop(box)).stddev[0] * 10)
            os.remove(pbase + ".pgm")
    if keep_grey:
        ga.save(os.path.join(outdir, "g%03d.pgm" % page))
        if entry is not None and any(row[0] == 0 and row[1] > 0 for row in out):
            # Words that draw nothing get a closer look by OCR, which needs more detail.
            hbase = os.path.join(outdir, "h%03d" % page)
            run(["pdftoppm", "-gray", "-r", str(TARGET_DPI), "-f", str(page), "-l", str(page), "-singlefile", pdf, hbase])
            if os.path.exists(hbase + ".pgm"):
                entry["hires"], entry["hires_dpi"] = "h%03d.pgm" % page, TARGET_DPI
    for f in (a, b):
        if os.path.exists(f):
            os.remove(f)
    return out


def render_pdf(pdf, outdir, manifest, mode, texts):
    info, pages = pdf_info(pdf)
    if pages < 1:
        raise RuntimeError("The PDF has no pages.")
    manifest["truncated_pages"] = max(0, pages - MAX_PAGES)
    pages = min(pages, MAX_PAGES)
    if manifest["kind"] == "pdf":      # a Word file's PDF is our own conversion; its properties are not the file's
        manifest["pdf_info"] = {k: info[k] for k in ("Title", "Author", "Creator", "Producer", "Subject", "Keywords")
                                if k in info}
    if info.get("Encrypted", "no").startswith("yes"):
        manifest["notes"].append("The PDF is encrypted, usually only to restrict copying or printing.")
    if info.get("JavaScript", "no") == "yes":
        manifest["markers"].append("JavaScript")
    if info.get("Form", "none") not in ("none", ""):
        manifest["notes"].append("The PDF has form fields. Text typed into them is not checked yet; "
                                 "`clean` keeps only what shows on the page.")
    with open(pdf, "rb") as f:
        raw = f.read(200_000_000)
    found = {m.decode().lstrip("/") for m in PDF_MARKERS if re.search(re.escape(m) + rb"[\s/<\[(]", raw)}
    # OpenAction, AA, URI and GoToR (links, initial view, links to other PDFs) are common in
    # ordinary PDFs; they matter only alongside scripts.
    if not found & {"JavaScript", "JS", "Launch"}:
        found -= {"OpenAction", "AA", "URI", "GoToR"}
    manifest["markers"] += sorted(found)
    manifest["layers_off"] = layers_off(pdf, raw)
    words_path = os.path.join(outdir, "words.html")
    run(["pdftotext", "-bbox", "-enc", "UTF-8", "-f", "1", "-l", str(pages), pdf, words_path])
    r = run(["pdftotext", "-enc", "UTF-8", "-f", "1", "-l", str(pages), pdf, "-"])
    normal = r.stdout.decode("utf-8", "replace")
    if manifest["kind"] == "pdf":       # a Word file's own text was read from its XML
        texts.append(normal)
    # Text placed outside the page is dropped by the normal extraction but not by every
    # extractor. Reading again over a far larger area shows it.
    r = run(["pdftotext", "-enc", "UTF-8", "-x", "-5000", "-y", "-5000", "-W", "20000", "-H", "20000",
             "-f", "1", "-l", str(pages), pdf, "-"])
    wide = r.stdout.decode("utf-8", "replace")
    manifest["offpage"] = offpage_words(normal, wide, pages)
    words = read_words(words_path)
    sizes = page_sizes(pdf, pages)
    ppi = photo_ppi(pdf, pages) if mode == "clean" else {}
    have_gs = tool("gs") is not None
    manifest["ink_checked"] = have_gs
    if not have_gs:
        manifest["notes"].append("Ghostscript was not found, so text covered by an image or shape, "
                                 "and faint text, were not checked.")
    for p in range(1, pages + 1):
        w_pt, h_pt = sizes.get(p, (612.0, 792.0))
        entry = {"width_pt": w_pt, "height_pt": h_pt, "photo_ppi": round(ppi.get(p, 0.0), 1), "ink": None}
        boxes = words[p - 1] if p - 1 < len(words) else []
        if have_gs:
            entry["ink"] = ink_measure(pdf, p, boxes, outdir, keep_grey=(mode == "scan"), entry=entry)
            if mode == "scan" and entry["ink"] is not None:
                entry["grey"] = "g%03d.pgm" % p
                entry["dpi"] = INK_DPI
        if mode == "scan" and "grey" not in entry:
            base = os.path.join(outdir, "g%03d" % p)
            run(["pdftoppm", "-gray", "-r", str(INK_DPI), "-f", str(p), "-l", str(p), "-singlefile", pdf, base])
            if os.path.exists(base + ".pgm"):
                entry["grey"], entry["dpi"] = "g%03d.pgm" % p, INK_DPI
        if mode == "clean":
            long_in = max(w_pt, h_pt) / 72.0
            src = ppi.get(p, 0.0)
            want, cap = (min(src, MAX_PHOTO_DPI), MAX_PHOTO_LONG_SIDE) if src > TARGET_DPI \
                else (TARGET_DPI, MAX_PIXELS_LONG_SIDE)
            dpi = max(int(min(want, cap / max(long_in, 0.1))), 50)
            base = os.path.join(outdir, "p%03d" % p)
            r = run(["pdftoppm", "-r", str(dpi), "-f", str(p), "-l", str(p), "-singlefile", pdf, base])
            if r.returncode != 0 or not os.path.exists(base + ".ppm"):
                raise RuntimeError("Could not render page %d." % p)
            entry["ppm"], entry["dpi"] = "p%03d.ppm" % p, dpi
        if "dpi" not in entry:
            raise RuntimeError("Could not render page %d." % p)
        manifest["pages"].append(entry)


def render_image(src, outdir, manifest, mode):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 80_000_000
    with Image.open(src) as im:
        frames = getattr(im, "n_frames", 1)
        im.seek(0)
        if frames > 1:
            manifest["notes"].append("Only the first frame of a multi-frame image was used.")
        exif = im.getexif()
        meta = {}
        for tag, name in ((0x013B, "Artist"), (0x010E, "ImageDescription"), (0x9C9C, "XPComment"),
                          (0x010F, "Make"), (0x0110, "Model"), (0x8298, "Copyright")):
            v = exif.get(tag)
            if v:
                v = v.decode("utf-16-le", "replace").rstrip("\x00") if isinstance(v, bytes) else str(v)
                meta[name] = v[:500]
        if exif.get_ifd(0x8825):
            meta["GPS"] = "location recorded"
        for k in ("comment", "Comment", "Description", "parameters"):
            if isinstance(im.info.get(k), (str, bytes)):
                v = im.info[k]
                meta[k] = (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)[:500]
        manifest["image_meta"] = meta
        img = ImageOps.exif_transpose(im).convert("RGB")
    w, h = img.size
    scale = min(1.0, MAX_PHOTO_LONG_SIDE / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    w, h = img.size
    entry = {"dpi": TARGET_DPI, "width_pt": w * 72.0 / TARGET_DPI, "height_pt": h * 72.0 / TARGET_DPI,
             "photo_ppi": round(TARGET_DPI / scale, 1), "ink": None}
    if mode == "clean":
        img.save(os.path.join(outdir, "p001.ppm"))
        entry["ppm"] = "p001.ppm"
    else:
        img.convert("L").save(os.path.join(outdir, "g001.pgm"))
        entry["grey"] = "g001.pgm"
    manifest["pages"].append(entry)


def main():
    src, outdir, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode not in ("scan", "clean"):
        raise RuntimeError("Unknown mode.")
    ext = os.path.splitext(src)[1].lower()
    manifest = {"source_ext": ext, "kind": None, "pages": [], "markers": [], "notes": [], "office_meta": {},
                "pdf_info": {}, "image_meta": {}, "word_findings": [], "layers_off": 0, "offpage": [], "truncated_pages": 0,
                "ink_checked": False}
    texts = []
    kind = sniff(src)
    if kind == "pdf":
        manifest["kind"] = "pdf"
        render_pdf(src, outdir, manifest, mode, texts)
    elif kind in ("zip", "ole", "rtf") or ext in OFFICE_EXT:
        manifest["kind"] = "office"
        if kind == "zip":
            word_structure(src, manifest, texts)
        if kind == "ole":
            manifest["markers"].append("legacy Word format (may hold macros)")
        conv = os.path.join(outdir, "conv")
        os.makedirs(conv, exist_ok=True)
        if kind == "zip":
            try:
                src = final_view(src, os.path.join(conv, "final" + (ext if ext in OFFICE_EXT else ".docx")))
            except (zipfile.BadZipFile, OSError, KeyError):
                pass
        pdf = to_pdf_with_libreoffice(src, os.path.join(conv, "pdf"))
        render_pdf(pdf, outdir, manifest, mode, texts)
        shutil.rmtree(os.path.join(outdir, "conv"), ignore_errors=True)
    elif kind == "image" or ext in IMAGE_EXT:
        manifest["kind"] = "image"
        render_image(src, outdir, manifest, mode)
    else:
        raise RuntimeError("Unsupported file type (%s)." % (ext or "no extension"))
    with open(os.path.join(outdir, "text.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(texts)[:MAX_TEXT])
    manifest["markers"] = sorted(set(manifest["markers"]))
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # report, never crash silently
        sys.stderr.write("WORKER ERROR: %s\n" % e)
        sys.exit(2)
