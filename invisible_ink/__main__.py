"""Command line.

    invisible-ink scan FILE...     report what is hidden (exit 0 clean, 1 something found, 2 error)
    invisible-ink clean FILE...    write a rebuilt copy with nothing hidden in it
    invisible-ink check            show which helper programs were found
"""

import argparse
import os
import platform
import sys

from . import __version__, chars, clean, detect, patterns, report, sandbox
from .worker import tool

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".rst", ".log"}
MAX_TEXT = 20_000_000


def _is_text(path):
    return os.path.splitext(path)[1].lower() in TEXT_EXT


def _read_text(path):
    if os.path.getsize(path) > MAX_TEXT:
        raise ValueError("the text file is larger than 20 MB")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _scan_one(path, mode, args, packs):
    name = os.path.basename(path)
    if _is_text(path):
        return detect.analyze_text(_read_text(path), name, packs), None, None
    ext = sandbox.extract(path, mode, require_sandbox=args.require_sandbox)
    try:
        r, boxes = detect.analyze(ext, name, packs, use_ocr=not args.no_ocr)
    except BaseException:
        ext.cleanup()
        raise
    return r, boxes, ext


def cmd_scan(args, packs):
    reports, status = [], 0
    for path in args.files:
        try:
            r, _, ext = _scan_one(path, "scan", args, packs)
            if ext:
                ext.cleanup()
        except (sandbox.SandboxError, OSError, ValueError, RuntimeError) as e:
            print("%s: could not be checked: %s" % (path, chars.printable(str(e))), file=sys.stderr)
            status = 2
            continue
        reports.append(r)
        if r["verdict"] != "clean" and status == 0:
            status = 1
        if not args.json:
            print(report.text_report(r, verbose=args.verbose))
    if args.json:
        print(report.json_report(reports))
    return status


def cmd_clean(args, packs):
    status = 0
    if args.output and len(args.files) > 1 and not os.path.isdir(args.output):
        print("With several files, -o must be a folder.", file=sys.stderr)
        return 2
    for path in args.files:
        try:
            if _is_text(path):
                r = detect.analyze_text(_read_text(path), os.path.basename(path), packs)
                target = clean.output_path(path, args.output, os.path.splitext(path)[1])
                clean.clean_text(path, target)
            else:
                r, boxes, ext = _scan_one(path, "clean", args, packs)
                try:
                    suffix = ".pdf"
                    if ext.kind == "image":
                        suffix = ".jpg" if os.path.splitext(path)[1].lower() in (".jpg", ".jpeg") else ".png"
                    target = clean.output_path(path, args.output, suffix)
                    clean.rebuild(ext, r, boxes, target, use_ocr=not args.no_ocr, report_page=args.report_page)
                finally:
                    ext.cleanup()
        except (sandbox.SandboxError, OSError, ValueError, RuntimeError) as e:
            print("%s: could not be cleaned: %s" % (path, chars.printable(str(e))), file=sys.stderr)
            status = 2
            continue
        print("%s -> %s" % (path, target))
        for line in clean.summary_lines(r):
            print("  " + chars.printable(line))
        if r.get("sandboxed") is False:
            print("  Warning: no sandbox was available, so the file was opened directly.")
    return status


def cmd_check(_args, _packs):
    rows = [("pdftotext / pdftoppm (poppler)", tool("pdftoppm") and tool("pdftotext"), "required"),
            ("tesseract", tool("tesseract"), "required for clean; used by scan on scanned pages"),
            ("gs (Ghostscript)", tool("gs"), "strongly recommended: finds covered and faint text"),
            ("soffice (LibreOffice)", tool("soffice"), "needed for Word files"),
            ("mutool (MuPDF)", tool("mutool"), "optional: counts switched-off PDF layers reliably"),
            ("bwrap (bubblewrap sandbox)", "yes" if sandbox.bwrap_available() else None, "Linux: recommended")]
    print("invisible-ink %s, Python %s, %s" % (__version__, platform.python_version(), platform.platform()))
    for name, found, why in rows:
        print("  %-32s %-8s %s" % (name, "found" if found else "MISSING", why))
    return 0 if tool("pdftoppm") and tool("pdftotext") else 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog="invisible-ink",
                                 description="Find and remove text a reader can't see but an AI will read.")
    ap.add_argument("--version", action="version", version="invisible-ink " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("files", nargs="+", metavar="FILE")
    common.add_argument("--pack", default="", help="also flag wording typical of: grading, hiring, review, "
                        "or all (comma-separated). Hidden text is always checked against every pack.")
    common.add_argument("--no-ocr", action="store_true", help="skip OCR (faster; clean output has no text layer)")
    common.add_argument("--require-sandbox", action="store_true", help="refuse to open files without bubblewrap")
    s = sub.add_parser("scan", parents=[common], help="report what is hidden")
    s.add_argument("--json", action="store_true", help="print the report as JSON")
    s.add_argument("-v", "--verbose", action="store_true", help="show file properties and every finding")
    c = sub.add_parser("clean", parents=[common], help="write a rebuilt copy with nothing hidden")
    c.add_argument("-o", "--output", help="output file, or folder for several files")
    c.add_argument("--report-page", action="store_true", help="add a first page saying what was removed")
    sub.add_parser("check", help="show which helper programs were found")
    args = ap.parse_args(argv)
    try:
        packs = patterns.parse_packs(getattr(args, "pack", ""))
    except ValueError as e:
        ap.error(str(e))
    return {"scan": cmd_scan, "clean": cmd_clean, "check": cmd_check}[args.cmd](args, packs)


if __name__ == "__main__":
    sys.exit(main())
