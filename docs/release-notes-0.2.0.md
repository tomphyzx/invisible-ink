Numbers that survive, and text layers that tell the truth.

**A change that can affect scripts:** `scan` can now return exit code 1 and the new verdict
`mismatch` for a file that used to pass. `scan` also takes about one second longer per typed PDF
page, because each page is now read by OCR.

- `scan` compares each typed PDF page with its text layer. A font table that makes a drawn `9` read
  as `1` is now reported: the page shows $98,400.00 and an AI reads $18,400.00. JSON reports list
  the differences as `text_mismatch`.
- `clean` reads numbers in bordered tables far more accurately. Table rules are removed from the
  copy OCR reads, and the cleaned page keeps them.
- `clean --list-numbers` lists every number where the new OCR text differs from the original's text
  layer, to check against the page image.
- `clean --json` prints the result as JSON, including `number_differences` and the output path.
- `tools/measure_digit_accuracy.py` measures how well numbers survive `clean`.

Measured on synthetic quotes with 9 pt bordered tables (699 digit-bearing tokens), counting tokens
that came through exactly:

| | 0.1.0 | 0.2.0 |
|---|---|---|
| Prices and quantities | 46% | 99% |
| Single digits on their own | 38% | 78% |
| Codes mixing letters and digits | 5% | 72% |
| All | 35% | 88% |

These are synthetic tables, not real vendor documents. OCR still makes mistakes, so check any figure
that matters against the page image. The README's Limits section says what the new check can and
cannot catch; in particular, a scan's own OCR layer disagreeing with its picture is listed but not
counted.

The false-alarm check for the new comparison was run on Ubuntu 24.04 over 63 PDFs from system
documentation (1,155 pages), none of them reported as mismatches.

Found a way to hide text that Invisible Ink misses? Please report it privately: see SECURITY.md.
