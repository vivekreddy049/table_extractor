# zextract

Deterministic table and context extraction from large, messy PDFs.
No language model touches the document, at any stage, in any capacity.

Design: [`ARCHITECTURE.md`](ARCHITECTURE.md) · Trade-offs: [`DECISIONS.md`](DECISIONS.md)

---

## Setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

No network access is needed at any point, and none is used — `tests/test_constraints.py`
proves it by making `socket.socket` raise for the duration of a full run.

## Use

```bash
python -m zextract run   --input doc.pdf --out ./out --offline
python -m zextract audit --out ./out
python -m zextract view  --out ./out              # rebuild out/viewer.html
python -m zextract debug --out ./out --page 32    # overlay render of one page
python -m zextract serve                          # web UI at http://127.0.0.1:8000
```

`serve` opens a local page where you can **upload a PDF and see its stats**, and
**compare each reconstructed table against the source page side by side** — the
page is rendered with the extraction region outlined in red, next to the grid we
built from it, with the row-label hierarchy shown and low-confidence cells
tinted. A grid that looks plausible on its own is not evidence; put it next to
the pixels it came from. Stats shown:
pages, tables accepted, candidates rejected with their reason codes, cells,
flag rate, timing, and links straight into the table viewer, review queue,
SQLite file and workbooks. Standard library only — no web framework, no CDN
assets, so it runs inside a network-isolated container. It binds to localhost
and executes the pipeline on whatever is posted to it, so do not expose it to a
network without authentication in front.

`run` writes `out/viewer.html` — **open it to browse every table found**. It shows
the reconstructed grid for each candidate, accepted and rejected alike, with the
rejection reason, measured column alignment, and each cell's page and bounding box
on hover. Rejected candidates are shown deliberately: "we never found it" and "we
found it and refused it for a stated reason" are different outcomes, and only one
of them is a bug.

`debug` writes a PNG of one page with tokens, text lines, ruling lines and block
boundaries overlaid.

---

**Full context for anyone picking this up cold — including the assignment brief,
what is and is not built, and every bug already found and fixed — is in
[`HANDOFF.md`](HANDOFF.md). Read [`LIMITATIONS.md`](LIMITATIONS.md) before
trusting any number this pipeline produces.**

## Supplying your own ground truth

The labels in `metrics/ground_truth/` were written by the same author as the
extractor, so the evaluation is **not independent** (`LIMITATIONS.md` §0b).
Independently-authored labels are worth more than any number of self-authored
ones. To add some:

```bash
# one table per CSV, named <document>__p<page>__<name>.csv
python metrics/import_labels.py "Apple Form 10-K__p32__income_statement.csv"

# or one workbook per document, one sheet per table, sheets named p32__name
python metrics/import_labels.py apple_labels.xlsx --document "Apple Form 10-K.pdf"

python metrics/verify_labels.py --samples "<dir of sample PDFs>"   # are they real
python metrics/eval.py          --samples "<dir of sample PDFs>"   # how it scores
```

Transcribe what the page says, cell by cell, header row included. Leave a cell
blank where the page is blank — blank is scored, and it differs from `-` or
`Nil`. For a page that should yield **no** table, the whole file is one cell:
`NOT_A_TABLE` (optionally `NOT_A_TABLE,TRAP_TOC` to assert the reason).

`verify_labels.py` checks every labelled string against the PDF's text layer and
will tell you if a cell is not on the page — it caught two of my own
transcription errors that re-reading did not.

## What works today

**S1 — intake, page typing, self-calibration.** Per-page digital / raster / hybrid
classification. Empty-password decryption. Word tokens from per-character boxes
with font, size and weight. Docstrum nearest-neighbour analysis (O'Gorman 1993)
giving per-page line pitch, inter-word gap and writing direction — the load-bearing
piece, because every threshold downstream is a multiple of these rather than an
absolute measurement ([D20](DECISIONS.md)).

**S2 — layout.** Ruling lines from vector primitives, merged and deduplicated.
Text lines by vertical-overlap clustering. Cross-page running header/footer
detection. Blocks split by rules and scale-free gap thresholds.

**S3 — table detection.** Column partition per block from vertical rules or
persistent whitespace; table candidates grown by testing whether a *joint*
partition still describes the union; trap classifier with named reason codes
(`TRAP_TOC`, `TRAP_INDEX`, `TRAP_PROSE_2COL`, `TRAP_KEYVALUE`, `TRAP_TOO_FEW_ROWS`).

**S4 — grid.** Rows recovered inside each candidate, cells assigned with full
token-level provenance, leading currency symbols attached to the number they
introduce, ambiguous wraps flagged rather than merged.

**S7 — normalisation.** Indian and Western digit grouping, all four negative
conventions (`(1,234)`, `-1,234`, `1,234-`, `−1,234`), percent, currency symbols
and codes, magnitude suffixes (Cr/Lakh/mn/bn/K), six date formats with day/month
ambiguity flagged rather than defaulted, spreadsheet error literals as a value
type, and the five-way empty/dash/Nil/NA/0 distinction preserved.

**S9 — validation signals.** Type conformity, parse and unit ambiguity, source
error values, and cross-foot reconciliation testing both additive and
subtractive readings. Produces per-cell confidence and a ranked
`review_queue.csv`. **The confidence is a ranking, not a calibrated probability
— see LIMITATIONS.md §2.**

**S10 — persistence.** SQLite at `out/extraction.db` on the brief's schema
(extended, never reduced), plus one four-sheet workbook per table under
`out/tables/`. Native Excel types are written only above the confidence band;
below it the raw string is written and the cell tinted, so uncertainty is
visible in the file itself. `run` also writes `out/logs/run.jsonl` and
`out/assets/` (empty `index.json` when the document has no figures).

**S8 — context.** Title, caption, section path and unit/scale note are bound
from geometry and generic prefixes. Unique figures are harvested (repeating
logos are furniture, not assets) and scored against tables on the same page.
Footnote markers (`*`, `†`, `(1)`) bind to blocks on this page and the next two.

## What does not work yet

- **S5 OCR path** — implemented as a Tesseract fallback on raster/hybrid pages.
  The samples are digital, so it has no accuracy measurement. Digital pages are
  never re-OCR'd.
- **S6 stitching** — signature + continuity. Tables that do not sit near the
  top of the next page stay split (by design).
- **S8 footnotes** — markers bind to blocks on this page and the next two;
  free-text references ("see note 3") are not bound.
- **S9 calibration** — frozen `model_v1.json` on 418 labelled cells. A full PAV
  fit on that set is not shipped (it would flag every cell). See LIMITATIONS.md §2.
- **S4 spans / header hierarchy** — spans and `header_path` are emitted.
  MRPL p4 can still fail *detection* (two overlapping candidates) before spans run.

---

## Measured, on this machine

| | |
|---|---|
| Apple 10-K, 121 pages, full pipeline | **24.3 s** incl. SQLite + 65 workbooks |
| Projected 100 pages | **~10 s** of the 1200 s budget |
| Peak traced Python heap | 41 MB |
| Two-run byte diff — `state/`, `viewer.html`, `debug/*.png`, `logs/run.jsonl`, **`tables/*.xlsx`**, DB rows | identical |
| Tests | 102 passed |

### Results on the three samples

| | Apple 10-K | Shell.pdf | Shell MRPL |
|---|---|---|---|
| Pages | 121 | 3 | 7 |
| Tables accepted | 65 | 3 | 7 |
| Candidates rejected | 79 | 0 | 1 |
| Cells | 2,021 | 378 | 201 |
| Cells flagged for review | 454 (22.5%) | 58 (15.3%) | 48 (23.9%) |
| Rules (h / v) | 1096 / 6 | **27 / 0** | 94 / 16 |

Rejections on Apple: `TRAP_TOO_FEW_ROWS` 50, `TRAP_TOC` 11, `TRAP_PROSE_2COL` 10,
`TRAP_KEYVALUE` 8.

### Scored against ground truth (`metrics/eval.py`, 13 labels)

```
detection        7/11 positives matched, 0/2 negatives wrongly accepted
cell exact match 84.5%   (418 cells)
flagged          104 (24.9%)
flag precision   0.13
flag recall      0.22
SILENT ERRORS    51 (12.2%)
```

Apple p32 (income statement) is now **25×4, structure 1.00, content 100%** --
the split-header silent error is gone. MRPL p4 (spanning header) is still
unmatched. Diagnosis in LIMITATIONS.md §0.

```bash
python metrics/eval.py --samples "<dir of sample PDFs>"
```

**The per-document counts below are not accuracy.** They are counts of what the
pipeline emitted. Scored numbers live in the section above and in
`metrics/results.json`. "65 tables accepted" still means 65 candidates survived
the trap classifier, not 65 correct tables.

### Known-good and known-bad

Apple p32 (Consolidated Statements of Operations) reconstructs as a single 25×4
table with the wrapped period header joined (`September 28, 2024`), every label,
figure and parenthesised negative in the right cell, the section headings
(`Net sales:`, `Cost of sales:`) preserved as their own rows, the running footer
excluded, and the trailing note excluded.

Shell.pdf p1 reconstructs as 46×4 with the full balance sheet (one extra header
line vs the label). p2's profit-and-loss comes out 7 columns wide against a
4-column label: the document prints inner and outer subtotal bands, and the
extractor treats them as columns.

MRPL p4 (rating history) is **unmatched**. It is the corpus's hardest table — a
three-level spanning header over `Date`/`Rating` pairs, with almost every cell
wrapped across three or four lines — and it comes out as two overlapping
candidates with the header flattened. Wrap merging is built; span detection
(S4.3) is not.

---

## Notes for a reviewer

Five things are deliberate and worth knowing before reading the code.

**A `Block` is not a table row.** Apple p32 sets its income statement at ordinary
leading with rules only under subtotals, so `Net sales:`, `Products $294,866 …` and
`Services 96,169 …` all fall in one block. A wrapped cell and a new row are
geometrically identical until you know where the columns are, so blocks are the
*scope* for column analysis and rows are recovered in S4.

**Table candidates are grown by re-partitioning, not by comparing partitions.** The
obvious approach — partition each block, group blocks whose partitions look alike —
fails, because a column boundary derived from one block is the midpoint of that
block's gutters, and a wide gutter's midpoint moves with the longest label that
block happens to contain. On Apple p32 the label/number boundary comes out at 245,
251 and 283 in three consecutive blocks of the *same* table. Asking instead whether
a single partition still describes the union answers the real question, and it
absorbs the `$` columns for free.

**Writing direction is read, not inferred.** Docstrum infers direction from
neighbour angles because it was built for connected components on a scan. On a
sparse financial table a number's nearest neighbours are the numbers above and
below it, so vertical pairs outnumber horizontal ones and the page reads as rotated
90° when it is upright. On a digital page the direction is in the file.

**Furniture is found by line position, not by a margin band.** Apple's running
footer is always the last line of the page but floats between y=523 and y=671 on a
792pt page. No geometric band finds it; ordinal position does.

**Ambiguity is flagged, not resolved.** A line that could be a wrapped label or a
section heading is kept as its own row and flagged `ROW_WRAP_AMBIGUOUS`. Splitting
a wrapped label is visible and recoverable; silently merging two data rows destroys
a number and is not.
