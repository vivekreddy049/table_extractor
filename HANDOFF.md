# HANDOFF — zextract

Self-contained context for picking this project up cold, on any platform.
Written 2026-09-03.

---

## 1. What the assignment is

Zenalyst engineering assignment: **"Deterministic Table and Context Extraction
from Large, Messy PDFs."** 6 days, private git repo, 60-minute live technical
review afterwards. 100 points + 10 bonus.

Build a pipeline that finds every data table in a 100–400 page PDF, rebuilds its
exact grid (merged cells, multi-level headers), extracts every cell with
provenance back to a page and bounding box, captures interpreting context (title,
section, units, scale, footnotes, linked figures), stitches tables that run
across pages, writes SQLite + one Excel file per table, and produces a
**calibrated per-cell confidence signal and a ranked human-review queue**.

### Hard constraints (violating any invalidates the submission)

- **No language models or hosted document APIs at runtime.** Not OpenAI /
  Anthropic / Google / Textract / Document AI / Unstructured / LlamaParse /
  Reducto. No local LLM or vision-language model either. Not even "offline, just
  for column-header semantics" — the brief answers that question explicitly: no.
- **Allowed:** classical CV (OpenCV, morphology, Hough, connected components,
  projection profiles, clustering), PDF text-layer parsing (PyMuPDF, pdfplumber,
  pdfminer.six), OCR engines (Tesseract, PaddleOCR, EasyOCR), and small local
  seeded layout/table-detection models (Table Transformer, YOLO) **only as one
  signal in an ensemble, never the sole basis for grid structure or cell text**,
  with weights vendored in-repo and an ablation showing performance with them off.
- **Determinism:** two runs on the same input must produce byte-identical DB rows
  and Excel files, with timestamps isolated to a single `extraction_runs` row.
  They will run it twice and diff.
- **Offline:** must run in a network-isolated container with no API keys.
- **Budget:** 100 pages ≤ 20 min on 4 vCPU / 8 GB, no GPU, peak RSS < 4 GB.
- **No off-the-shelf one-liner:** `camelot.read_pdf()` / `tabula` / `docling` as
  the whole solution is an automatic fail. Running them as *baselines* with a
  win/lose analysis is encouraged.

### How it is scored

| # | Criterion | Pts |
|---|---|---|
| 1 | Table detection quality (P/R/F1 at IoU ≥ 0.7, incl. false-positive traps) | 12 |
| 2 | Structure reconstruction (TEDS-Struct, merged cells, header hierarchy) | 15 |
| 3 | Cell content accuracy (exact match on `raw_text`; separately `normalized_value`) | 15 |
| 4 | **Silent-error rate and calibration** (flagging PR curve, reliability diagram) | **18** |
| 5 | Context capture (title, section, unit/scale, footnote→cell, table→figure) | 12 |
| 6 | Cross-page stitching | 8 |
| 7 | Output contract compliance | 5 |
| 8 | Determinism, offline, budget | 5 |
| 9 | Engineering quality | 5 |
| 10 | Written reasoning (`DECISIONS.md`) | 5 |

**Bonus:** +4 pure-classical path within 10 pts of best config; +3 chart data
recovery; +3 active-learning loop from review-queue corrections.

**The governing rule:** *"Every cell must be either correct, or flagged as
uncertain. A wrong cell delivered with high confidence is the only unforgivable
failure."* Target: flag ≤ 8% of cells while those flags cover ≥ 95% of actual
errors. A 92%-accurate pipeline that catches 97% of its own mistakes beats a
96%-accurate one that cannot tell you which 4% are wrong.

### Required deliverables

```
README.md  ARCHITECTURE.md  DECISIONS.md  LIMITATIONS.md
metrics/   src/   tests/   Dockerfile (must build and run with no network)

python -m zextract run   --input doc.pdf --out ./out --offline
python -m zextract audit --out ./out
python -m zextract debug --out ./out --page 42
```
Plus an ≤8-minute screen recording walking one hard page end to end.

Output contract: `out/extraction.db`, `out/tables/*.xlsx` (4 sheets: Data,
Context, Provenance, Issues), `out/assets/*.png`, `out/manifest.json`,
`out/review_queue.csv`, `out/metrics.json`, `out/debug/page_042.png`,
`out/logs/run.jsonl`.

---

## 2. Sample documents

Three PDFs were provided (`~/Downloads/sample data(1)/sample data/`). All three
are **fully digital — no scanned pages anywhere.**

| | Apple Form 10-K | Shell MRPL (ICRA) | Shell.pdf |
|---|---|---|---|
| Pages | 121 | 7 | 3 |
| Producer | EDGAR HTML→PDF | Word → iTextSharp | Excel → "Print to PDF" |
| Encryption | RC4-128, opens with **empty password** | none | none |
| Horizontal rules | 1096 | 94 | 27 |
| **Vertical rules** | **6** | 16 | **0** |

### The ten measured findings that drove the design (F1–F10 in ARCHITECTURE.md §0)

1. **F1 — vertical rules are essentially absent.** Apple: 6 across 121 pages.
   Shell.pdf: zero. Real financial tables are horizontally ruled and vertically
   borderless. A "ruled detector vs borderless detector" split is the wrong seam.
2. **F2 — right edges cluster, left edges don't.** Apple p32 word histogram:
   `x1` has three sharp spikes (19/18/18 votes), `x0` is flat noise. Numbers are
   right-aligned; labels are ragged.
3. **F3 — currency symbols occupy their own physical column.** `$` at x≈363, 442,
   521 separate from the digits. A naive partition emits 7 columns where the
   document means 4.
4. **F4 — reading order ≠ visual order.** Shell.pdf p2 emits the whole P&L body
   *before* its header row, because Excel's print driver writes frozen panes last.
   Any "first line is the header" rule promotes a data row to a header.
5. **F5 — real documents contain genuinely broken values.** Shell.pdf p3 has live
   `#REF!` cells printed into the PDF, making a subtotal unreconcilable. This is
   the brief's "deliberate typo" case occurring naturally.
6. **F6 — repeating page furniture masquerades as figures.** MRPL image xrefs
   22/23/24 at identical bboxes on pages 1–6 (logos); only p7 has a real figure.
7. **F7 — the false-positive traps are in the samples.** Apple p3 is a TOC that is
   geometrically indistinguishable from a two-column data table.
8. **F8 — font weight is a usable header signal.** Apple: ArialMT 8.1 body,
   Arial-BoldMT 7.2 headers.
9. **F9 — glyphs lie.** Shell.pdf renders ₹ as a backtick (font encoding).
10. **F10 — no sample is scanned**, but the held-out set will be. Proposed fix:
    rasterise labelled digital pages with seeded degradation; the original text
    layer is then exact ground truth for the OCR path, free.

---

## 3. Repo layout

Project root: `C:\Users\VIVEK REDDY\Music\Desktop\17pm`

```
README.md ARCHITECTURE.md DECISIONS.md LIMITATIONS.md HANDOFF.md
config.yaml            every threshold, each with a comment stating its unit
pyproject.toml pytest.ini Dockerfile
notes/                 raw probe output from the sample PDFs
src/zextract/
  cli.py               run | audit | view | debug
  config.py            frozen config, hashed into extraction_runs
  geometry.py          Rect + overlap/gap/angle/quantile primitives
  model.py             all pipeline state dataclasses, JSON round-trippable
  pipeline.py          stage driver
  s1_intake/           loader.py (page typing) tokens.py docstrum.py
  s2_layout/           rules.py lines.py blocks.py furniture.py
  s3_detect/           columns.py regions.py
  s4_structure/        grid.py spans.py
  s5_ocr/              page.py (Tesseract fallback; skip if binary missing)
  s6_stitch/           stitch.py
  s7_normalise/        values.py apply.py
  s8_context/          bind.py assets.py footnotes.py
  s9_confidence/       score.py apply_calib.py
  calib/               model_v1.json
  persist/             schema.sql db.py excel.py run.py
  audit/               render.py (PNG overlay) viewer.py (HTML report)
  webapp.py            stdlib-only upload UI (`zextract serve`)
tests/                 102 tests
metrics/               eval.py + ground_truth/ (13 labels) + results.json
```

**Dependencies:** pymupdf, numpy, pillow, pyyaml, openpyxl. Dev: pytest.
Python 3.11. No network at any point.

---

## 4. What is BUILT and working

- **S1 intake** — per-page digital/raster/hybrid typing, empty-password decrypt,
  word tokens from per-character boxes carrying font/size/weight.
- **S1 Docstrum self-calibration** — per-page line pitch, inter-word gap, writing
  direction, modal font size, via k=5 nearest-neighbour histograms
  (O'Gorman 1993). **This is load-bearing:** every threshold downstream is a
  multiple of these, never an absolute measurement.
- **S2 layout** — vector rule harvesting + merge/dedupe; text lines by
  vertical-overlap clustering; cross-page running header/footer detection;
  blocks split by rules and scale-free gaps.
- **S3 detection** — column partition from vertical rules or persistent
  whitespace; table candidates grown by testing whether a *joint* partition still
  describes the union; trap classifier (`TRAP_TOC`, `TRAP_INDEX`,
  `TRAP_PROSE_2COL`, `TRAP_KEYVALUE`, `TRAP_TOO_FEW_ROWS`, `TRAP_EMPTY`).
- **S4 grid** — rows recovered per candidate, cells with token-level provenance,
  leading currency symbols attached to their number, ambiguous wraps flagged.
- **S7 normalisation** — Indian + Western grouping, all four negative conventions
  `(1,234)` `-1,234` `1,234-` `−1,234`, percent, currency symbols/codes,
  magnitude suffixes (Cr/Lakh/mn/bn/K), six+ date formats with day/month
  ambiguity flagged, spreadsheet error literals as a value type, five-way
  empty/dash/Nil/NA/0 distinction, per-column type voting at 70% agreement.
- **S8 context** — title (Table/Exhibit/Annexure/Schedule/Statement/Consolidated,
  bold-short, ALL CAPS), caption (Source/Note, italic, centred, smaller),
  section path, unit/scale regex with inheritance from earlier pages, unique
  figure harvest with repeating-logo exclusion, scored table→figure links.
  Footnote→cell binding is not built.
- **S9 signals** — type conformity, parse ambiguity, unit-glyph ambiguity, source
  error values, cross-foot/down-foot reconciliation (additive *and* subtractive
  readings), per-cell confidence, ranked `review_queue.csv`.
- **S10 persistence** — SQLite to the brief's schema (extended, never reduced);
  one 4-sheet `.xlsx` per table; native Excel types only above the confidence
  band, raw text and a tint below it. CLI and the web UI both call `persist_run`,
  which also writes `logs/run.jsonl` (no wall-clock) and `assets/` (crops +
  `index.json`).
- **S11 audit** — PNG page overlay renderer + self-contained HTML table viewer
  showing accepted *and* rejected candidates with reasons.

### Measured

| | |
|---|---|
| Apple 121 pages, S1–S4 | 12.5 s (103 ms/page) |
| Projected 100 pages | ~10 s of the 1200 s budget |
| Peak traced Python heap | 41 MB (limit 4 GB) |
| Two-run byte diff (state, viewer, PNGs) | identical |
| Tests | 102 passing |

| | Apple | Shell.pdf | MRPL |
|---|---|---|---|
| Tables accepted / rejected | 65 / 79 | 3 / 0 | 7 / 1 |
| Cells | 2,021 | 378 | 201 |
| Cells flagged for review | 454 (22.5%) | 58 (15.3%) | 48 (23.9%) |

---

## 5. What is NOT built

| Stage | State |
|---|---|
| **S5 OCR path** | Tesseract fallback on raster/hybrid pages with a thin text layer. Unmeasured: no sample is scanned. |
| **S6 cross-page stitching** | Signature + continuity. Ambiguous pairs stay separate. |
| **S8 context capture** | Title/caption/unit/assets/footnotes (marker→block). Not scored against labels. |
| **S9 calibration** | Frozen `calib/model_v1.json` on 418 cells. Full PAV not shipped. |
| **S4 spans / header hierarchy** | `row_span`/`col_span`/`header_path` emitted. MRPL p4 may still miss at detection. |
| **Ground truth** | 13 labels covering ARCHITECTURE.md §7. Scored: detection 7/11, cell exact 84.5%, 51 silent errors. |
| **metrics/ harness** | Content-overlap matching and a TEDS-Struct-like shape score. Not IoU ≥ 0.7, not published TEDS, no baselines. |
| **Recording** | Not made. |

---

## 6. The five design decisions that matter most

Full reasoning in `DECISIONS.md` (D1–D21). The load-bearing ones:

1. **D19/D2 — structure-first detection.** Do not propose regions by appearance
   then find their grid. Compute column-alignment structure everywhere, and
   define a table as a maximal run of blocks sharing a column partition. Region
   proposal is where appearance-overfitting lives.
2. **D20 — no absolute measurement constants.** Every threshold is a multiple of
   a Docstrum page statistic or a named `config.yaml` entry. Enforced by an AST
   lint test that fails on any float literal outside `{0, 0.5, 1, 2, inf}` in
   S2/S3/S4 code. This is the single biggest defence against a pipeline that
   scores 90 on the samples and 40 on held-out documents.
3. **D21 — leave-one-producer-out evaluation.** PDF *producer* determines layout
   convention far more than industry does. Target corpus: ~25 documents across
   ≥ 7 producers; fit on all but one, test on the held-out one, rotate, report
   the mean. Not yet done.
4. **D8 — never repair the document.** `#REF!` ships as `#REF!`. A failing
   cross-foot is flagged, never corrected.
5. **D7 — calibration model frozen into the repo.** Fit logistic + isotonic
   offline on labelled data, commit the coefficients, runtime only evaluates.
   That is how calibration coexists with byte-identical output. **Not yet done.**

---

## 7. Bugs found and fixed (don't reintroduce these)

Each was caught by looking at real output, and each has a regression test:

1. **Docstrum reported upright pages as rotated 90°.** On a sparse table a
   number's nearest neighbours are the numbers above and below it, so vertical
   pairs outnumber horizontal ones. Fix: on a digital page, *read* the writing
   direction from PyMuPDF's line `dir`; reserve inference for raster pages.
2. **The 30° angle cone admitted diagonal neighbours on adjacent lines**, putting
   142 spurious zeros into a 432-sample word-gap histogram. Fix: require vertical
   overlap for "same line" and horizontal overlap for "same column".
3. **Comparing per-block column partitions fragmented Apple's income statement
   into 11 tables.** A wide gutter's midpoint moves with the longest label in
   each block (245 / 251 / 283 for the same column). Fix: test a merge by
   re-partitioning the union.
4. **Running-footer detection by margin band found nothing.** Apple's footer is
   always the last line but floats y=523–671 on a 792pt page. Fix: ordinal line
   position, not geometry.
5. **`TRAP_INDEX` rejected genuine revenue tables.** `4,812` is comma-separated
   digits, so it matched the page-reference pattern — and matching was done on
   the row's *joined* text, discarding the cell boundaries that make the two
   distinguishable. Fix: per-cell full match, excluding grouped numbers.
6. **`PARSE_AMBIGUOUS` fired on prose containing a comma** ("September 28,"),
   flooding the review queue. Fix: only strings made purely of digits and
   separators are number candidates.
7. **Cross-foot swept header years into its sums** — residuals came back as
   exactly −2024 / −2023 / −2022. Fix: a line item must have a non-numeric label.
8. **Cross-foot treated every subtraction as a failure** (`Net income` = income −
   tax). Fix: test additive *and* subtractive readings, take the better residual.
9. **A table-level flag pushed every cell in the table below the review
   threshold** (44% of cells flagged). Fix: table flags lower table confidence;
   row flags gate that row; cell signals gate the cell.
10. **S6 stitching was dead code on every real document.** Its continuity gate
    compared the continuation's y0 against ~1.5 line pitches from the PHYSICAL
    page edge. Real bodies start 78pt (Apple) to 88pt (ICRA) down, so the gate
    opened on 0 of 121 and 0 of 7 pages. It passed its own unit test, whose
    fixture placed the continuation at y=20. Fixed by measuring from the
    document's own median top-of-content; regression test uses a 90pt margin.
11. **S9 destroyed every issue S6 recorded.** `score_table` did
    `table.issues = issues` rather than `.extend(...)`, wiping the
    STITCH_AMBIGUOUS entries written earlier in the run. 18 of them now reach
    the database on the Apple 10-K; before, zero did.
12. **`(8)%` parsed as text** — the sign is only visible after the `%` is
    stripped. Fix: re-check the sign after removing the percent.

---

## 8. Next steps, in priority order

0. **MEASURED RESULT, READ FIRST.** `metrics/eval.py` against 13 labels gives:
   detection 7/11 positives matched, 0/2 negatives wrongly accepted, cell exact
   match 84.5% on 418 cells. Apple p32 is now exact (25×4, content 100%).
   Flag precision 0.13, flag recall 0.22, **51 silent errors (12.2%)**. The
   remaining unmatched detections are MRPL p4 (spans), MRPL p5, and Shell p3's
   second table. Diagnosis in LIMITATIONS.md §0.

1. **S4 spans + header hierarchy** — geometric spans land; MRPL p4 may still
   miss at S3 (two overlapping candidates) before spans run.
2. **S9 calibration** — frozen model on 418 cells. Refit with
   `python metrics/calibrate.py` once more labels exist. Silent errors at 0.95
   still have no identifying feature.
3. **Screen recording.**

The brief's own advice: *"If you run out of time, ship less scope done properly
rather than everything done badly. Say so plainly in LIMITATIONS.md; we read that
file closely."*

---

## 9. How to run

```bash
pip install -e ".[dev]"
python -m pytest                                   # 102 tests

python -m zextract run   --input doc.pdf --out ./out --offline
python -m zextract audit --out ./out
python -m zextract view  --out ./out               # rebuild out/viewer.html
python -m zextract debug --out ./out --page 32     # overlay PNG
python -m zextract serve                           # upload UI on :8000
```

Open `out/viewer.html` to browse every table candidate, accepted and rejected.
