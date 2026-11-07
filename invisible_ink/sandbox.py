"""Run the extraction worker, in a bubblewrap sandbox when one is available, and read back its output.

The trusted side reads only what the worker wrote: a JSON manifest (size-capped,
type-checked), raw PPM/PGM pixels (strictly parsed here, never handed to an image
decoder), the pdftotext word list (parsed with a narrow regex) and plain text.
It never opens the original file.
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

PACKAGE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(PACKAGE, "worker.py")
MAX_MANIFEST = 8_000_000
MAX_WORDS_FILE = 60_000_000
MAX_TEXT_FILE = 20_000_000
MAX_SIDE = 8000


class SandboxError(RuntimeError):
    pass


def bwrap_available():
    if os.environ.get("INVISIBLE_INK_NO_SANDBOX") or not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        return False
    r = subprocess.run(["bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                        "--unshare-all", "--die-with-parent", "/usr/bin/true"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def _bwrap_cmd(workdir, filename, mode):
    return ["bwrap",
            "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--ro-bind", "/etc", "/etc",          # fonts, ld cache, LibreOffice config
            "--ro-bind-try", "/var/lib/ghostscript", "/var/lib/ghostscript",   # Ghostscript's font map
            "--ro-bind-try", "/var/cache/fontconfig", "/var/cache/fontconfig",
            "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--ro-bind", PACKAGE, "/app/invisible_ink",
            "--ro-bind", os.path.join(workdir, "in"), "/work/in",
            "--bind", os.path.join(workdir, "out"), "/work/out",
            "--unshare-all",                       # includes network
            "--die-with-parent", "--new-session",
            "--clearenv",
            "--setenv", "PATH", "/usr/bin",
            "--setenv", "HOME", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
            "--chdir", "/tmp",
            "/usr/bin/python3", "-I", "-B", "/app/invisible_ink/worker.py",
            "/work/in/" + filename, "/work/out", mode]


def extract(original_path, mode, require_sandbox=False, timeout=900):
    """Copy the original into a fresh work folder, run the worker, return an Extraction."""
    workdir = tempfile.mkdtemp(prefix="invisible-ink-")
    os.chmod(workdir, 0o700)
    os.makedirs(os.path.join(workdir, "in"))
    os.makedirs(os.path.join(workdir, "out"))
    ext = os.path.splitext(original_path)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,6}", ext):
        ext = ""
    filename = "document" + ext
    shutil.copyfile(original_path, os.path.join(workdir, "in", filename))
    if bwrap_available():
        cmd, sandboxed = _bwrap_cmd(workdir, filename, mode), True
    elif require_sandbox:
        shutil.rmtree(workdir, ignore_errors=True)
        raise SandboxError("No sandbox is available (bubblewrap, Linux only), so the file was not opened.")
    else:
        cmd = [sys.executable, "-I", "-B", WORKER, os.path.join(workdir, "in", filename),
               os.path.join(workdir, "out"), mode]
        sandboxed = False
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise SandboxError("Opening the file took too long and was stopped.")
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace")
        m = re.search(r"WORKER ERROR: (.*)", msg)
        shutil.rmtree(workdir, ignore_errors=True)
        raise SandboxError(m.group(1)[:400] if m else "The file could not be opened.")
    try:
        return Extraction(os.path.join(workdir, "out"), workdir, sandboxed)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def read_pnm(path):
    """Strict binary PPM (P6) or PGM (P5) reader, maxval 255 -> PIL image."""
    with open(path, "rb") as f:
        data = f.read()
    m = re.match(rb"P([56])\s+(?:#[^\n]*\n\s*)*(\d+)\s+(\d+)\s+(\d+)\s", data)
    if not m:
        raise SandboxError("A rendered page has an unexpected format.")
    channels = 3 if m.group(1) == b"6" else 1
    w, h, maxval = int(m.group(2)), int(m.group(3)), int(m.group(4))
    if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE and maxval == 255):
        raise SandboxError("A rendered page has unexpected dimensions.")
    pixels = data[m.end():]
    if len(pixels) != w * h * channels:
        raise SandboxError("A rendered page is truncated.")
    return Image.frombytes("RGB" if channels == 3 else "L", (w, h), pixels)


_WORD_RE = re.compile(r'<word xMin="([-\d.]+)" yMin="([-\d.]+)" xMax="([-\d.]+)" yMax="([-\d.]+)">(.*?)</word>')


def read_words(path):
    """pdftotext -bbox output -> list (per page) of (text, x0, y0, x1, y1) in points."""
    if not os.path.exists(path):
        return []
    if os.path.getsize(path) > MAX_WORDS_FILE:
        raise SandboxError("The text layer is unreasonably large.")
    with open(path, encoding="utf-8", errors="replace") as f:
        doc = f.read()
    pages = []
    for chunk in doc.split("<page ")[1:]:
        words = []
        for m in _WORD_RE.finditer(chunk):
            x0, y0, x1, y1 = (float(m.group(i)) for i in range(1, 5))
            words.append((html.unescape(m.group(5))[:200], x0, y0, x1, y1))
        pages.append(words)
    return pages


def _strs(d, n=40, size=500):
    out = {}
    if isinstance(d, dict):
        for k, v in list(d.items())[:n]:
            out[str(k)[:40]] = str(v)[:size]
    return out


def _ink(value, count):
    """Per-word ink measurements, or None when missing or malformed."""
    if not isinstance(value, list) or len(value) != count:
        return None
    out = []
    for row in value:
        if not (isinstance(row, list) and len(row) == 6 and all(isinstance(x, int) and -1 <= x < 10**9 for x in row)):
            return None
        out.append(tuple(row))
    return out


class Extraction:
    def __init__(self, outdir, workdir, sandboxed):
        self.outdir, self.workdir, self.sandboxed = outdir, workdir, sandboxed
        mpath = os.path.join(outdir, "manifest.json")
        if not os.path.exists(mpath) or os.path.getsize(mpath) > MAX_MANIFEST:
            raise SandboxError("The worker did not produce a usable manifest.")
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
        if not isinstance(m, dict) or not isinstance(m.get("pages"), list) or not m["pages"]:
            raise SandboxError("The worker produced no pages.")
        self.kind = str(m.get("kind"))
        self.source_ext = str(m.get("source_ext", ""))[:10]
        self.markers = [str(x)[:80] for x in m.get("markers", [])][:40]
        self.notes = [str(x)[:300] for x in m.get("notes", [])][:20]
        self.truncated_pages = int(m.get("truncated_pages", 0))
        self.layers_off = int(m.get("layers_off", 0))
        self.ink_checked = bool(m.get("ink_checked", False))
        off = m.get("offpage", [])
        self.offpage = [str(x)[:5000] for x in off][:len(m["pages"])] if isinstance(off, list) else []
        self.metadata = {}
        for src in ("pdf_info", "office_meta", "image_meta"):
            self.metadata.update(_strs(m.get(src, {})))
        self.word_findings = []
        for f in m.get("word_findings", [])[:300]:
            if isinstance(f, dict) and f.get("kind") in ("hidden", "deleted", "comment", "alt_text"):
                self.word_findings.append({"kind": f["kind"], "where": str(f.get("where", ""))[:40],
                                           "text": str(f.get("text", ""))[:2000]})
        words = read_words(os.path.join(outdir, "words.html"))
        self.words = [words[i] if i < len(words) else [] for i in range(len(m["pages"]))]
        self.pages = []
        for i, p in enumerate(m["pages"]):
            if not isinstance(p, dict):
                raise SandboxError("Unexpected page entry.")
            entry = {"dpi": int(p["dpi"]), "width_pt": float(p["width_pt"]), "height_pt": float(p["height_pt"]),
                     "photo_ppi": float(p.get("photo_ppi", 0.0)), "ink": _ink(p.get("ink"), len(self.words[i]))}
            entry["hires_dpi"] = int(p.get("hires_dpi", 0) or 0)
            for key, pattern in (("ppm", r"p\d{3}\.ppm"), ("grey", r"g\d{3}\.pgm"), ("hires", r"h\d{3}\.pgm")):
                name = p.get(key)
                if name is not None:
                    if not re.fullmatch(pattern, str(name)):
                        raise SandboxError("Unexpected page file name.")
                    entry[key] = os.path.join(outdir, str(name))
            self.pages.append(entry)
        tpath = os.path.join(outdir, "text.txt")
        self.text = ""
        if os.path.exists(tpath) and os.path.getsize(tpath) <= MAX_TEXT_FILE:
            with open(tpath, encoding="utf-8", errors="replace") as f:
                self.text = f.read()

    def image(self, i, key):
        path = self.pages[i].get(key)
        return read_pnm(path) if path and os.path.exists(path) else None

    def cleanup(self):
        shutil.rmtree(self.workdir, ignore_errors=True)
