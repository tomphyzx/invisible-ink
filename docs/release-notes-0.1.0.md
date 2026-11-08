First public release.

- `scan` reports text a reader can't see but an AI will read, in PDF, Word, image and text files.
- Hidden text is found by drawing each page with and without its text, so it is caught however
  it was hidden: white on white, matching colours, covered by a shape or picture, invisible
  rendering mode, clipped, faint, tiny or outside the page.
- Word files: hidden text, kept tracked deletions, comments and alt text.
- Invisible Unicode: zero-width characters, tag-character and variation-selector smuggling
  (decoded), text-direction overrides.
- Wording aimed at an AI is marked, with optional grading, hiring and peer-review packs.
- `clean` rebuilds a copy from page pixels, so nothing hidden survives.
- Scanned PDFs with OCR layers come out clean.
- Files are opened in a bubblewrap sandbox on Linux.
- `clean -o` takes a folder (created if needed) or, for one file, a file name; nothing is
  ever overwritten.

Tested on Ubuntu 24.04 (with the sandbox) and macOS 26 on Apple silicon (without it), and
against about 80 real documents: PDFs from Word, LaTeX and other tools, scanned PDFs, patents,
papers, slide decks, Word and Google Docs files, photos and screenshots.

Found a way to hide text that Invisible Ink misses? Please report it privately: see SECURITY.md.
