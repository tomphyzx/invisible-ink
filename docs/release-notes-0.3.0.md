Hidden text in more places, and MuPDF is now required for PDFs.

**Changes that can affect scripts and installs**

- **MuPDF (`mutool`) is now required** to clean a PDF and to scan one completely. Without it
  `clean` makes no copy of a PDF and exits 2, and `scan` reports a PDF as `incomplete` (exit 1).
  Word files and images are not affected. The reason: a layer hidden on screen that is stored in a
  compressed part of a PDF cannot be seen without MuPDF.
- `scan` can now return exit code 1 for a file that used to pass: a PDF with text outside the
  page a viewer shows (print-ready PDFs with printer's marks or a job line among them), text
  squeezed into a sliver, hidden text that runs up the page, or a layer that does not show on
  screen.
- A cleaned copy of a cropped PDF now has the page size a viewer shows, not the larger size stored
  in the file.
- Findings can carry a new reason, `off_screen`.
- `invisible-ink check` now exits 2 when Tesseract or MuPDF is missing, not only Poppler, and its
  last line names what is missing.

**What is now caught**

- **The page is what a viewer shows.** Text in a PDF's margin outside the visible page (cropped
  away, or a print shop's marks and job line) is reported as outside the page. `clean` used to
  draw such text into the cleaned copy as ordinary visible text; it no longer does.
- **Layers that do not show on screen.** A layer set to print but not to show on screen is
  reported and its words are listed. `clean` used to carry them into the cleaned copy; it now
  leaves them out. Layers switched off by a PDF's base state are counted as switched-off layers.
- **Squeezed text.** Text squeezed sideways into an unreadable sliver is reported as tiny, whatever
  its height, and `clean` paints the whole of it over.
- **Turned text.** Hidden text that runs up the page is counted as a passage, and so is most
  hidden text set on a slant. It used to be listed one word at a time and not counted.

**A check that fails is not a clean result**

- When MuPDF is installed but fails on a file (an error, a crash, a timeout, or an answer that
  cannot be read), `scan` now reports the PDF as `incomplete` and `clean` makes no copy. Before,
  a failed layer check could read as "no layers".

**Ubuntu 26.04, and newer Ghostscript**

- On Ubuntu 26.04 the sandbox could not run Ghostscript, so every PDF was reported as not fully
  checked. Fixed: the sandbox's work folders are now `/tmp/work/in` and `/tmp/work/out` inside it.
  Nothing else about the sandbox changed.
- Layers now read the same on Ghostscript 10.02 and 10.06. A layer that shows on screen and not in
  print no longer raises a note on 10.06.
- Tested on Ubuntu 24.04 (Poppler 24.02, Ghostscript 10.02, MuPDF 1.23, Tesseract 5.3) and Ubuntu
  26.04 (Poppler 26.01, Ghostscript 10.06, MuPDF 1.27, Tesseract 5.5).

**Smaller changes**

- Cropped PDFs no longer get the note "Ghostscript could not draw N words", and a PDF cropped at
  its left edge no longer has visible text reported as outside the page.
- The reason shown for tiny text now reads "too small to read (under 3 pt high, or squeezed into a
  sliver)".
- A word under a comment icon (a sticky note) can now be listed as uncertain: a reader cannot see
  it on screen. It is not counted.
- The tests now treat MuPDF as a needed helper program, and skip their end-to-end tests without it.

**What `scan` can still miss**

- **`scan` can miss some hidden text.** A few ways of setting text so that it merges with
  visible text, or of squeezing or turning it, can still give a `clean` verdict. In every
  such case we know of, the copy `clean` makes does not contain that text, because it is
  rebuilt from what the page shows on screen. When it matters, pass on the cleaned copy, not
  the original.
- **Text outside the visible page counts as hidden**, including printer's marks and job
  lines on print-ready PDFs. **So does text that prints but does not show on screen**, such
  as a print-only watermark. Expect such files to be reported; the words are listed so you
  can judge them.

Found a way to hide text that Invisible Ink misses? Please report it privately: see SECURITY.md.
