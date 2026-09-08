# ARCHITECTURE — `zextract`

Deterministic table and context extraction from large, messy PDFs.
No language model touches the document, at any stage, in any capacity.

---

## 0. Grounding: what the provided samples actually contain

Every design choice below is justified against measured properties of the three
sample PDFs, not against a generic mental model of "a PDF with tables."
Raw probe output lives in `notes/probe_profile.txt` and `notes/probe_geometry.txt`.

| Property | Apple Form 10-K | Shell MRPL (ICRA) | Shell.pdf |
|---|---|---|---|
| Pages | 121 | 7 | 3 |
| Producer | EDGAR HTML→PDF converter | MS Word → iTextSharp | **Excel → "Microsoft Print to PDF"** |
| Text layer | Full, 411k chars | Full, 15k chars | Full, 7.7k chars |
| Scanned/raster pages | **none** | none | none |
| Encryption | RC4 128-bit, **opens with empty password** | none | none |
| Page rotation | all 0 | all 0 | all 0 |
| Vertical rules | **0 across the entire document** | 2 per page | **0** |
| Horizontal rules | 2–40 per table page | 27–90 per page | 10–20 per page |
| Body font | ArialMT 8.1 / Arial-BoldMT 7.2 | — | — |

### The findings that drive the architecture

**F1 — Vertical rules are essentially absent from real financial PDFs.**
Apple's 121 pages contain zero vertical rules. Shell.pdf contains zero. Every
table in both is horizontally ruled and vertically borderless. This is exactly
the regime the brief calls out as the one "which neither approach handles alone."
A pipeline built as "ruled detector OR borderless detector" fails on 2 of 3 samples.
**Consequence:** the primary detector must be a *hybrid* that consumes horizontal
rules as row evidence and infers columns from geometry independently. See §S3, §S4.

**F2 — Right edges cluster; left edges do not.**
Word-coordinate histogram, Apple p32 (Consolidated Statements of Operations),
176 words:

```
x1 (right edge) : 432 x19   511 x18   590 x18    <- three sharp spikes = three numeric columns
x0 (left edge)  :  19 x10    35 x 8   407 x 8    <- diffuse, no structure
```

Numeric columns in financial documents are right-aligned; label columns are
left-aligned and ragged. A column detector keyed on left edges or on whitespace
gutters alone throws away the strongest signal on the page.
**Consequence:** column inference runs a *dual-anchor* projection — left-edge
density for label columns, right-edge density for numeric columns — and merges
the two. See §S4.2.

**F3 — Currency symbols occupy their own physical column.**
On Apple p32 the `$` glyphs sit at x0 ≈ 363, 442, 521 — four occurrences each,
in narrow bands separate from the digits at x1 ≈ 432, 511, 590. A naive column
detector emits six columns (`$`, number, `$`, number, `$`, number) where the
document means three. **Consequence:** a symbol-column absorption pass. See §S4.4.

**F4 — Reading order ≠ visual order.**
Shell.pdf p2 emits the entire body of the P&L *before* the header row
(`Note / Particulars / 2022-23 / 2021-22`), because Excel's print driver wrote the
frozen header last. Anything that consumes `page.get_text()` in stream order and
assumes the first line is the header will silently promote a data row to a header.
**Consequence:** all structure is built from **coordinates only**. Stream order is
used for exactly one thing: tie-breaking otherwise-equal geometric sorts, so the
output stays deterministic. See §S5.

**F5 — Real documents contain genuinely broken values.**
Shell.pdf p3 (Cash Flow) contains literal `#REF!` cells — a live Excel error
printed into the PDF. Two rows below it, "Cash Generated from Operations" is
therefore unreconcilable. This is the assignment's "subtotal that deliberately
does not reconcile" case occurring naturally in a provided sample.
**Consequence:** `#REF!`, `#DIV/0!`, `#N/A`, `#VALUE!` are a recognised value type
(`error`), never coerced to null, and they raise a `SOURCE_ERROR_VALUE` issue plus
a `CROSSFOOT_UNVERIFIABLE` (not `CROSSFOOT_FAIL`) on every subtotal that depends
on them. We never repair the document. See §S7, §S9.

**F6 — Repeating page furniture masquerades as figures.**
MRPL images with xrefs 22, 23, 24 appear at byte-identical bounding boxes on
pages 1–6: `(18,808,79,830)`, `(482,807,626,832)`, `(500,10,589,77)` — footer logo,
footer badge, header logo. Page 7 carries the one genuine figure, xref 68 at
`(108,196,518,503)`. Emitting 19 assets instead of 1 poisons table→figure
association. **Consequence:** an asset is page furniture if the same xref appears
at IoU ≥ 0.95 on ≥ 40% of pages; it is excluded from `assets` and recorded once as
a page-furniture template. See §S2, §S8.

**F7 — The false-positive traps are present in the samples, not hypothetical.**
Apple p3 is a Table of Contents: left-aligned item labels, right-aligned page
numbers, one strong gutter. Geometrically it is indistinguishable from a two-column
data table. It must be rejected. See §S3.4.

**F8 — Font weight is a usable, deterministic header signal.**
Apple p32 uses `ArialMT 8.1` for body and `Arial-BoldMT 7.2` for headers — a size
*and* weight change. This is free structural evidence available from the text layer
with no model. It is one vote in header-row detection, never the sole basis. See §S4.5.

**F9 — Font encoding is unreliable; glyphs lie.**
Shell.pdf renders the rupee sign as a backtick: the header reads `` (` Million) ``
where the document displays `(₹ Million)`. The text layer is byte-exact for digits
but not for symbols outside the font's declared encoding.
**Consequence:** currency detection reads from a symbol table *plus* the surrounding
words (`Million`, `Rs.`, `crore`, `lakh`), never from a single glyph. Where the glyph
is ambiguous the unit is emitted with reduced confidence and a `UNIT_GLYPH_AMBIGUOUS`
issue rather than a guess. See §S7.3.

**F10 — There are no scanned pages in any sample.**
The OCR path (S1 raster branch, S5 per-cell OCR) therefore has *zero* natural test
coverage here, while the held-out set is promised to contain skewed image-only pages.
**Consequence:** we manufacture ground truth — see §7.2, "the rasterisation trick."

---

## 1. Design axioms

1. **Geometry is the source of truth.** Every structural claim is derived from
   coordinates. Stream order, HTML-converter artefacts and reading order are advisory.
2. **Additive, never destructive.** `raw_text` is captured first and never
   overwritten. Normalisation writes new fields alongside it. Any stage may add
   evidence; no stage may delete it.
3. **Every claim carries its evidence.** A cell knows its page, its bbox, its source
   (`text-layer` / `ocr`), and which detector votes produced the grid it sits in.
4. **Uncertainty is a first-class output, not an afterthought.** Confidence is
   computed from deterministic signals and *calibrated* against a hand-labelled set.
   An uncalibrated 0.9 is worthless; the number must mean "9 out of 10 such cells
   are right."
5. **We do not repair the document.** If the source says `#REF!`, we ship `#REF!`
   and flag it. A silently plausible substitute is the one unforgivable failure.
6. **Refusal is a valid, scored output.** "This table is unreadable, here is an
   issues row" beats a guess.

---

## 2. Pipeline overview

```
  doc.pdf -> S1 -> S2 -> S3 -> S4 -> S5 -> S6 -> S7 -> S8 -> S9 -> S10/S11

  S1  intake        page typing (digital / raster / hybrid), deskew, rotation
  S2  layout        header/footer strip, blocks, headings, figures, captions, footnotes
  S3  detect        hybrid region detector + arbitration + trap rejection
  S4  structure     rows, dual-anchor columns, spans, header hierarchy, indent tree
  S5  cell text     text-layer geometric assignment | per-cell OCR on crops
  S6  stitch        page-level tables -> logical tables
  S7  normalise     type inference, Indian/Western grouping, negatives, scale, dates
  S8  context       title, section path, unit/scale inheritance, footnotes, figures
  S9  confidence    deterministic features -> calibrated probability -> review queue
  S10 persist       SQLite + one .xlsx per logical table
  S11 audit         overlay renderer, manifest, logs
```

Each stage is a pure function `(state, config) -> state'`. The state is a plain
dataclass tree that is JSON-serialisable at every boundary, so any stage can be run,
dumped, diffed and re-entered in isolation. That property is what makes the live-review
debugging session ("here is a page we break on") tractable.

---

## S1 · Intake and page typing

**Per page, not per document.** Classification uses three measurements:

| Signal | Digital | Raster | Hybrid |
|---|---|---|---|
| Text-layer chars | > 200 | < 20 | ≥ 20 |
| Image area / page area | any | > 0.75 | > 0.30 |
| Chars inside image bboxes | ~0 | n/a | > 0 |

A page is `hybrid` when a large image covers a region that the text layer does not
describe — the scanned exhibit pasted into a digital report. Hybrid pages route
*per region*: text-layer extraction outside the image, OCR inside it.

Apple p108 is the pattern in miniature: 39 characters (`Exhibit 19.1 / Insider
Trading Policy / A-2`) plus one full-page image. Chars-only heuristics call it
digital and lose the exhibit entirely.

**Rotation** comes from `/Rotate` first (authoritative, free), then from a text-line
angle histogram when `/Rotate` is 0 but lines run vertically — the landscape table
in a portrait document. **Deskew** applies only to raster pages, by projection-profile
angle search over ±5° in 0.1° steps, and the applied angle is recorded in
`pages.deskew_angle` so every bbox can be mapped back to original page space.

**Apple's RC4 encryption** decrypts with an empty user password (`needs_pass == 0`).
We attempt empty-password decryption once; if a real password is required we fail
loudly with an issues row rather than emitting a zero-table document.

---

## S2 · Layout segmentation

Regions produced: `running_header`, `running_footer`, `heading`, `body`, `table_candidate`,
`figure`, `caption`, `footnote_block`.

**Running header/footer detection is cross-page, and it is the cheapest large win
in the whole pipeline.** For each page, take text lines in the top 12% and bottom
12% of the page box. Group across pages by (normalised text with digits masked,
y-band within 3pt). A group appearing on ≥ 40% of pages is furniture. This kills:

- `www.icra.in / Page | N / Sensitivity Label : Public` on all 7 MRPL pages
- `Apple Inc. | 2024 Form 10-K | N` on 121 Apple pages
- the three repeating MRPL logo images (F6), by the same rule applied to image xrefs

Furniture is excluded from table candidates and from `assets`, but is *retained* in
the page model, because the footer often carries the document-level unit declaration.

**Headings** are detected by font-size z-score against the page's modal body size,
plus weight change, plus short line length, plus vertical isolation. The heading
stack maintained across pages produces `section_path` for S8.

**Footnote blocks** sit below the last body block, in a font ≥ 0.8pt smaller than
modal body, and begin with a marker (`*`, `†`, `1`, `(1)`, `#`). Markers are
extracted with coordinates so S8 can bind them to cells.

---

## S3 · Table region detection

Three detectors vote. Arbitration is by **evidence score, not by priority order** —
this is the central departure from the "two detectors and a rule" framing in the brief,
and F1 is why.

### S3.1 Detector A — ruled (morphological)

Vector path for digital pages: harvest `page.get_drawings()`, keep primitives that
are axis-aligned runs (`re` with one dimension < 2.5pt, or `l` with |dx|<1 or |dy|<1),
merge collinear segments within 1.5pt, discard runs < 20pt. Raster path: binarise,
open with a 1×k / k×1 structuring element, connected components.
Vector-first matters: it is exact, ~50× cheaper than rasterising, and immune to
JPEG hairline breakage.

Emits: `h_lines[]`, `v_lines[]`, and `closed_cells[]` from intersections.

### S3.2 Detector B — borderless (projection + alignment)

Vertical ink-density projection over the candidate band; minima persisting across
≥ 80% of candidate rows are column boundaries. Row bands from y-clustering of text
line baselines with a gap threshold set at `1.6 × modal_line_gap`.

### S3.3 Detector C — hybrid / partial-rule  ← **the one that matters here**

Triggered when `len(h_lines) ≥ 2 and len(v_lines) ≤ 1`. Horizontal rules are taken
as *authoritative row-band boundaries* (they are drawn by the producer, so they are
exact), and columns come entirely from Detector B's dual-anchor analysis running
*inside* those bands. On the sample corpus this is not an edge case — it is the
majority case: Apple 100%, Shell.pdf 100%, MRPL partially.

### S3.4 Arbitration and the negative classifier

Each candidate region scores on: rule coverage, column-boundary persistence,
numeric-token density, row-count, alignment regularity, and inter-column gap variance.

Then an explicit **trap classifier** runs, and it is a *rejecter with named reasons* —
not a threshold. Each rule emits a reason code that lands in `issues` even when the
region is rejected, so a false rejection is auditable:

| Code | Rule |
|---|---|
| `TRAP_TOC` | ≥ 60% of rows end in a bare integer ≤ page_count, dot/space leader runs present, or the nearest heading matches `contents|index` |
| `TRAP_INDEX` | ≥ 60% of rows match `label, digits(, digits)*` with comma-separated page refs |
| `TRAP_PROSE_2COL` | exactly 2 candidate columns, mean tokens-per-cell > 8, numeric density < 5%, both columns left-aligned with ragged right edges |
| `TRAP_CALLOUT` | a closed rectangle with < 2 interior rules and < 2 inferred columns |
| `TRAP_UNDERLINE` | a single horizontal rule under one text line, no second rule within 3× line height |
| `TRAP_KEYVALUE` | 2 columns, ≤ 3 rows, left column ends in `:` |

Apple p3 (TOC) trips `TRAP_TOC` on all three of its sub-rules. Every rejection is
recorded with its code, so §8-1 scoring on the false-positive traps is measurable
rather than hoped-for.

**Side-by-side tables** (an explicit adversarial item) are separated before
arbitration: if the column-boundary profile contains a gutter wider than
`3 × median_gutter` that persists across ≥ 90% of rows *and* both sides independently
satisfy the region score, the region is split into two candidates.

---

## S4 · Structure recognition

### S4.1 Rows, from blocks

Block boundaries come from horizontal rules where present (exact), else from
baseline clustering; rows are then recovered inside a block once the column
partition is known (see §9.2). Multi-line cell content is handled here, not later: a text line joins the
*current* band rather than opening a new one when its left edge is indented relative
to the band's first line and no rule separates them, or when the band's other columns
are empty at that y. This is what keeps
`"- Total outstanding dues of micro enterprises and small enterprises"`
(Shell.pdf p1, wrapped across two physical lines) as one cell.

### S4.2 Columns via dual-anchor projection  ← **the F2 mechanism**

Build two weighted histograms over x with 1pt bins, smoothed by a 3pt kernel:

- **L-anchor:** density of token *left* edges → recovers ragged label columns
- **R-anchor:** density of token *right* edges → recovers right-aligned numeric columns

Peaks are extracted from each independently and reconciled: an R-peak with sharpness
above threshold defines a numeric column whose right boundary is the peak and whose
left boundary is the following gutter minimum; an L-peak defines a left-aligned column
symmetrically. Where both fire in the same band, the sharper peak wins and the loser
is retained as `column_evidence` for the confidence model.

On Apple p32 the R-anchor produces three unambiguous peaks (19, 18, 18 votes) where a
gutter-only method produces mush. This is the single highest-value idea in the design
and it comes directly from measuring the sample rather than from the literature.

### S4.3 Spans

`col_span` where a token's x-extent crosses ≥ 1 inferred boundary *and* the cells it
would occupy are otherwise empty across the band. `row_span` where a cell's ink extends
across a row-band boundary while neighbouring columns have content in both bands.
Header spans are detected the same way and produce the `header_path` tree.

### S4.4 Symbol-column absorption  ← **the F3 mechanism**

A detected column is a *symbol column* when ≥ 80% of its non-empty cells are a single
token drawn from `{$, ₹, €, £, %, *, †, (, )}` and its width is < 2.5% of table width.
It is merged into its neighbour — right for a leading currency symbol, left for a
trailing `%` or footnote marker — and the fact is recorded as
`columns.merged_from_symbol_column` so the grid remains explainable.
Footnote markers absorbed this way are handed to S8 for cell-level binding, which is
how `Export*` on the brief's Figure 6 gets its footnote attached to the cell rather
than to the page.

### S4.5 Header hierarchy

A row is a header row if it accumulates enough votes: above the first rule that
separates it from a numeric-dense band; font weight/size differs from body (F8);
low numeric density; spans present; distinct fill or rule weight. Multi-level headers
are stacked top-down and `header_path` is the ordered list from outermost to innermost
(`["FY 2024-25", "Q4"]`).

### S4.6 Indentation → `row_label_path`

Cluster first-column left edges into indent levels (1-D k-means with the number of
levels chosen by the largest gap in sorted centroid distances, capped at 4).
Maintain a stack; each row's `row_label_path` is the stack contents at its level.
Shell.pdf p1 exercises the full depth:
`["I. ASSETS", "Current Assets", "(b) Financial Assets", "(ii) Trade Receivables"]`.
Note that Shell.pdf additionally uses an *enumerator column* (`(a)`, `(b)`, `(i)`,
`(ii)`) rather than pure whitespace — that column is detected as an enumerator
column and folded into the label path rather than emitted as data.

---

## S5 · Cell text extraction

**Digital:** pull spans with coordinates, assign to cells by centroid containment with
a fallback to maximum-overlap for tokens straddling a boundary. **We never re-OCR what
the text layer already has exactly.** Token→cell assignment residual (distance from
token centroid to cell centre, normalised) is retained as the `grid_snap_residual`
feature for S9.

**Raster / hybrid:** OCR the *cell crop*, not the page. Crops are padded 2px, upscaled
to a target 32px x-height, binarised (Sauvola), and passed to Tesseract with a PSM
chosen per cell class (`--psm 7` single line, `--psm 6` for wrapped cells) and a
character allowlist for columns already typed numeric. Per-character confidences are
retained.

**Text hygiene** (deterministic, ordered, and each step logged): NFKC normalisation;
Unicode minus U+2212, en-dash U+2013 and hyphen-minus unified for numeric parsing but
preserved in `raw_text`; non-breaking and thin spaces normalised; ligatures decomposed;
soft hyphens dropped; superscript digits recognised as footnote markers rather than
value digits. Watermark suppression: glyphs whose fill colour has low contrast against
background, or whose rotation differs from the page's modal text angle by > 15°, are
routed to a `watermark` layer and excluded from cells while remaining in the page model.

---

## S6 · Cross-page stitching

Two page-level tables merge into one logical table when a **signature match** and a
**continuity check** both pass.

*Signature* = the tuple `(n_cols, normalised column right-edge vector, header_path
hash, column type vector)`. Right-edge vectors are compared after scaling to page
width, with a tolerance of 1.5% of page width — this absorbs the "column widths drift
by a point or two" case without merging genuinely different tables.

*Continuity* requires: consecutive pages (or separated only by pages containing no
body content); the candidate is the first table region on its page; and the vertical
gap from page top to the region is within 1.5× the modal top-margin.

*Header handling:* if page N+1's first row matches page N's header row by text
similarity ≥ 0.9, it is marked `is_header` and excluded from data — the "duplicate the
header row into the data" failure the brief calls out. If there is no repeated header,
the continuation inherits page N's `header_path` wholesale.

*Explicit markers* (`(Contd.)`, `Continued`, `contd`) are a **bonus vote, never a
requirement** — the brief is explicit that they are often absent, and MRPL's
"Rating history for past three years" table spans a page break with no marker at all.

*Refusal:* when signature matches but continuity fails, or vice versa, the tables stay
separate and a `STITCH_AMBIGUOUS` issue is written against both. We prefer two correct
tables over one wrong one.

---

## S7 · Normalisation and typing

Column type is inferred by voting over cells, requiring ≥ 70% agreement; the minority
is not coerced — it becomes a `TYPE_NONCONFORMITY` signal for S9, because one string in
a numeric column is exactly the red flag the brief asks for.

Types: `integer`, `decimal`, `currency`, `percent`, `date`, `text`, `ratio`,
`empty`, `nil`, `na`, `dash`, `error`.

### S7.1 The five-way null distinction

**`empty` / `dash` / `nil` / `na` / `0` are preserved as five separate types.** `-` in
Shell.pdf's "Capital work-in-progress / prior year" column means *nil this period*;
`0.00` would mean measured zero; a blank means not applicable. Collapsing them destroys
information no downstream sum can recover.

### S7.2 Number parsing

In order, with the matched rule recorded on the cell:

- Indian grouping `1,23,45,678` — detected by a 2-digit group appearing after the first group
- Western grouping `12,345,678`
- Negatives: `(1,234)` → −1234, `-1,234`, `1,234-` (trailing sign, common in SAP exports), `−1,234` (U+2212)
- Percent, currency symbols and ISO codes
- Magnitude suffixes `Cr`, `Crore`, `Lakh`, `Lac`, `mn`, `bn`, `K`, `Mn`, `Bn`
- Excel error literals (F5) → type `error`, `normalized_value` NULL, issue raised

Ambiguity is resolved and *reported*, not silently chosen: `1,234` is unambiguous;
`1,23,456` is unambiguously Indian; `12,345` is Western. A string like `1,234.56.78`
is `text` with a `PARSE_AMBIGUOUS` issue.

### S7.3 Units, currency and dates

Currency is read from a symbol table **plus surrounding words** (F9) — never from a
single glyph, because Shell.pdf proves the glyph can be wrong. An ambiguous glyph
yields `UNIT_GLYPH_AMBIGUOUS` and reduced confidence, not a guess.

Dates: at least `DD/MM/YYYY`, `MM/DD/YYYY`, `DD-Mon-YYYY`, `Mon DD, YYYY`,
`YYYY-MM-DD`, `DD.MM.YYYY`, plus Indian fiscal forms `FY2025`, `FY 2024-25`, `Q3 FY25`.
`DD/MM` vs `MM/DD` is resolved *per column* using any unambiguous member (a day > 12),
and where the whole column is ambiguous both readings are stored and the cell is flagged
`DATE_ORDER_AMBIGUOUS` rather than defaulted to a locale.

---

## S8 · Context capture

| Context | Rule | Evidence in samples |
|---|---|---|
| `title` | nearest heading-styled line above the region within 3× line height, matching `Table \|Exhibit \|Annexure \|Schedule ` or bold-and-short | MRPL "Annexure I: Instrument details" |
| `caption` | line directly below region in caption style (smaller, italic or centred) | MRPL "Source: Company" |
| `section_path` | heading stack from S2 at the region's page/y | Apple "Item 7A. Quantitative and Qualitative Disclosures About Market Risk" |
| `unit_scale_note` | inheritance scope — see below | Apple "(In millions, except…)"; Shell "(₹ Million)" |
| footnotes | marker matched from cell → footnote block, **searching the current page then the next two pages** | brief's "footnote text on the following page" |
| assets | figure crops, page furniture excluded (F6) | MRPL p7 xref 68 |

### S8.1 Unit and scale inheritance

The brief's adversarial set includes "a scale declared once at the top of a section
governing eight tables below it." The rule:

1. A scale declaration inside the table's own bbox or within 2 line-heights above it
   binds to that table only, at confidence 0.95.
2. Otherwise, the nearest preceding declaration is inherited, and **its scope ends at
   the next heading of equal-or-shallower depth than the heading that contained it**,
   or at the next declaration, whichever comes first. Inherited scale is written with
   confidence 0.7 and `unit_scale_source = 'inherited'`.
3. A declaration is never inherited across a document-part boundary (Part I / Part II).

Apple exercises case 1 (every statement restates "In millions"). Shell.pdf exercises a
harder variant: the unit sits *inside the header row* (`2022-23 (₹ Million)`), so unit
detection also scans header cells and, on a hit, binds **per-column** rather than per-table.

### S8.2 Table→figure association

The brief demands we defend the rule. Ours is an additive score, and the *score itself*
becomes `relation_confidence`, so a weak association is visibly weak rather than
silently asserted:

```
score = 0.40 * same_page
      + 0.25 * shared_numbering        (Table 4.2 <-> Figure 4.1, same section number)
      + 0.20 * textual_reference       (caption or nearby prose names the table)
      + 0.10 * adjacency               (vertical gap < 2x line height)
      + 0.05 * series_count_match      (bar count == table data-row count)

relation = 'visualises'  if score >= 0.55
         = 'co-located'  if score >= 0.35
         = no link       otherwise
```

---

## S9 · Validation and confidence

### Features (all deterministic, all cheap)

- `ocr_char_conf_min` / `_mean` (raster only; 1.0 for text-layer)
- `grid_snap_residual` — normalised token-to-cell-centre distance
- `type_conformity` — does this cell match its column's inferred type
- `digit_count_z` — digit count vs the column's distribution
- `crossfoot_delta` / `downfoot_delta` — signed reconciliation residual
- `header_body_colcount_mismatch`
- `row_duplicate_hash_collision`
- `col_boundary_persistence` — how stable this column was across the table's rows
- `detector_agreement` — how many detectors voted for this region
- `is_stitch_boundary_row`, `is_span_cell`, `symbol_absorbed`

### Cross-foot and down-foot

Subtotal rows are identified by label regex (`total`, `sub-total`, `net`, `aggregate`,
`grand total`) *and* by arithmetic discovery: any row whose value equals the sum of a
contiguous run of rows above it within tolerance. Both directions are checked. A match
raises confidence on **every cell in the reconciled set** — a passing cross-foot is
strong positive evidence for the whole row, not just the total. A mismatch lowers all
of them and raises `CROSSFOOT_FAIL`; **we flag, we never fix** (F5, and the brief's
deliberate-typo case).

### Calibration — the part that is actually worth 18 points

Raw feature combination is a logistic model fit on the hand-labelled set, then passed
through **isotonic regression** to map scores to true correctness probabilities.
The reliability diagram is emitted as part of `metrics.json`.

Determinism is preserved by **freezing the fitted coefficients into a versioned JSON
checked into the repo** (`src/zextract/calib/model_v1.json`). Fitting is an offline
developer action (`make calibrate`), never part of a run. A run reads coefficients and
computes; it never learns. This is what lets calibration coexist with byte-identical
output.

### Review queue

Cells are ranked by `expected_error = (1 − p_correct) × severity_weight`, where severity
weights a currency cell in a total row above a text cell in a footnote. `review_queue.csv`
carries reason codes, and we report the full **precision–recall curve of flagging**, with
the chosen operating point marked. The 8% / 95% target is reported as a point on that
curve, and if we cannot reach it we say so numerically in `LIMITATIONS.md` rather than
tuning the threshold until the number looks right.

---

## S10 · Persistence

Schema is the brief's minimum, extended (never reduced). Extensions and why:

- `column_header_tokens(column_id, level, token)` — a **normalised child table** so the
  brief's litmus query ("every cell whose column header path contains 'Revenue', with
  page number and unit") is a plain join, not a `LIKE` over a JSON blob. The brief says
  "if that query is awkward, your schema is wrong"; a JSON `header_path` alone is awkward.
- `cell_features(cell_id, feature_name, value)` — the confidence inputs, so a reviewer
  can ask *why* a cell was flagged.
- `detector_votes_json` on `tables` — already in the brief; we populate it with per-detector
  scores, not just a winner.
- `unit_scale_source` on `tables`, `unit` on `columns` — declared vs inherited (S8.1).
- `pages.page_type` ∈ `digital|raster|hybrid`, `pages.deskew_angle`.
- `issues.code` is a closed vocabulary shared with `review_queue.csv`.

Excel: `out/tables/T{idx:03d}__p{start}-{end}__{slug}.xlsx`, four sheets
(Data / Context / Provenance / Issues). Native Excel types are written **only where
confidence ≥ the calibrated 0.95 band**; below it the raw string is written and the
cell is tinted, so a human opening the file sees uncertainty without reading a manual.
Merged cells preserved, header rows frozen.

---

## S11 · Auditability

`python -m zextract debug --out ./out --page 42` renders the page with: detected
regions (green = accepted, red = rejected-with-code), morphological rules found,
inferred column boundaries with their L/R anchor provenance in different colours,
cell boxes tinted by confidence, and footnote/asset links drawn as arrows. This is
built on day 2, not day 6 — it is the primary development instrument, and the brief
says the live review will use it.

---

## 3. Determinism

| Risk | Mitigation |
|---|---|
| Set/dict iteration order | ordered containers only; every sort key is total, with a geometric tie-break (`y`, then `x`, then a content hash) |
| Float non-associativity | fixed accumulation order; comparisons via `math.isclose` with declared tolerances; `Decimal` for money |
| Clustering instability | seeded, deterministic init (sorted-quantile seeding, not k-means++ RNG) |
| Timestamps | confined to `extraction_runs`; all other tables timestamp-free |
| Filesystem order | `sorted()` on every directory listing |
| PNG output | fixed encoder settings, no embedded timestamps |
| Model non-determinism | seeded, CPU-only, fixed thread count; ablatable |
| JSON | `sort_keys=True`, fixed separators, `ensure_ascii=False` |

`make determinism-check` runs the pipeline twice into two directories and byte-diffs
everything except the single `extraction_runs` row. It runs in CI and it is a test,
not a manual step.

---

## 4. Resource budget

Target: 100 pages ≤ 20 min, 4 vCPU / 8 GB, peak RSS < 4 GB.

Levers: vector-first line extraction (no rasterisation on digital pages — this alone is
the difference between minutes and tens of minutes on Apple's 121 pages); page-level
multiprocessing with a hard worker cap; rasterise only raster/hybrid pages and only at
the DPI the OCR needs; stream page state to disk rather than holding 121 pages of
`get_drawings()` in memory; per-cell OCR bounded by a cell-count budget with an
`OCR_BUDGET_EXCEEDED` issue if exceeded rather than an unbounded run.

Measured numbers go in `metrics.json` and `README.md` — the brief asks for actuals.

---

## 5. Baselines

`camelot` (lattice + stream), `pdfplumber.extract_tables`, and `tabula` run in
`metrics/baselines/` on the same labelled set and the same metrics. Expectation:
we lose to camelot-lattice on fully ruled tables, and win decisively on (a) Apple's
vertical-rule-free tables, (b) trap rejection, (c) everything calibration-related,
where the baselines offer nothing at all. Where we lose, `DECISIONS.md` says so.

---

## 6. Module layout

```
src/zextract/
  cli.py              run | audit | debug
  config.py           frozen dataclass config, YAML overlay, hashed into extraction_runs
  model.py            state dataclasses (JSON round-trippable at every stage boundary)
  s1_intake/  s2_layout/  s3_detect/  s4_structure/  s5_cells/
  s6_stitch/  s7_normalise/  s8_context/  s9_confidence/
  persist/            sqlite schema + writers, xlsx writer
  audit/              overlay renderer, manifest, jsonl logger
  calib/model_v1.json frozen calibration coefficients
metrics/
  ground_truth/       hand-labelled tables (see §7)
  baselines/          camelot / pdfplumber / tabula harness
  eval.py             detection PRF@IoU0.7, TEDS-Struct, cell EM, calibration curves
tests/
```

---

## 7. Ground truth — building the ruler first

The brief is explicit that hand-labelling is part of the exercise, and that Day 1 is
for it. Plan: **12 tables**, chosen for coverage rather than convenience.

| # | Source | Why this one |
|---|---|---|
| 1–2 | Apple p32, p34 | horizontal-rules-only, `$` symbol columns, indented row hierarchy |
| 3 | Apple p30 | 4-col table embedded in prose, multi-line header |
| 4 | Apple p3 | **negative** — TOC trap, ground truth is "not a table" |
| 5–6 | Shell.pdf p1, p2 | deep indent hierarchy, enumerator column, header-after-body (F4) |
| 7 | Shell.pdf p3 | `#REF!` errors, unreconcilable subtotal (F5) |
| 8 | MRPL p1 | ruled, 2 vertical rules, multi-line cells |
| 9 | MRPL p4 | **two-level header with spanning cells** (`Current rating (FY2026)` / `Chronology … past 3 years` over `FY2026 FY2025 FY2024 FY2023` over `Date \| Rating` pairs) — the hardest structure in the corpus |
| 10 | MRPL p4→p5 | cross-page continuation with no `(Contd.)` marker |
| 11 | MRPL p5 | Annexure table + `Source: Company` caption |
| 12 | MRPL p7 | table adjacent to a real figure (xref 68) — association test |

Labels are stored as JSON in `metrics/ground_truth/`, one file per table, with cell
text, spans, header paths and bboxes. Negative regions are labelled too — trap
rejection is part of the 12 detection points and cannot be measured without them.

### 7.2 The rasterisation trick — free OCR ground truth (F10)

No sample is scanned, but the held-out set will be. So: take a labelled digital page,
render it at 200 dpi, apply a seeded degradation (rotation ±3°, Gaussian blur, JPEG
quality 60, salt-and-pepper noise, a diagonal watermark), and run the **raster path**
over it. The text layer of the original page is exact ground truth for the OCR output.

This gives a genuinely adversarial, fully-labelled scanned test set at zero labelling
cost, and it is the only way we can honestly claim anything about the OCR path before
seeing the held-out documents. Degradation parameters are seeded, so this is a
reproducible test, not a demo.

---

## 8. Testing

- Unit tests per stage on synthetic fixtures (a hand-built 3×4 ruled table, a borderless
  one, a merged-header one).
- Golden-file tests on the 12 labelled tables.
- A determinism test (two runs, byte diff).
- A **no-network test** that monkeypatches `socket.socket` to raise, proving the offline
  constraint rather than asserting it.
- A budget test that fails if a 100-page run exceeds wall-clock or RSS thresholds.

---

## 9. Generalisation — the primary engineering risk

The three sample documents are evidence that certain *regimes* exist (horizontal-only
ruling, right-edge alignment, reading-order scrambling). They are **not** a target to
fit. The held-out set is explicitly different documents, and the realistic failure mode
for this assignment is not a missing feature — it is a pipeline that scores 90 on the
samples and 40 on anything else.

Three disciplines guard against that. They are load-bearing, not aspirational.

### 9.1 Self-calibration: no absolute constants anywhere

**The rule: no detection or structure module may contain a bare measurement literal.**
Every threshold is either a multiple of a page-derived statistic, or a named entry in
`config.yaml` with a comment stating what it means. This is enforced by a lint test that
greps the detection modules for float literals outside a whitelist.

The page statistics come from a **Docstrum-style nearest-neighbour analysis**
(O'Gorman 1993), computed once per page before anything else runs:

1. For every word token, find its k = 5 nearest neighbours by bbox-centre distance.
2. Histogram the neighbour **angles**. The dominant peak is the page's text angle —
   this is the deskew estimate for raster pages and the rotation check for digital ones,
   and it comes free.
3. Histogram the neighbour **distances**, restricted to near-horizontal pairs. The peak
   is `within_line_word_gap`.
4. Same for near-vertical pairs. The peak is `line_pitch`.

Everything downstream is expressed in those units:

| Quantity | Expressed as |
|---|---|
| Row-band split threshold | `1.6 × line_pitch` |
| Column gutter minimum width | `2.5 × within_line_word_gap` |
| Multi-line cell join tolerance | `1.2 × line_pitch` |
| Caption proximity | `3 × line_pitch` |
| Heading font z-score | vs. **modal** body size on that page |
| Symbol column max width | fraction of **table** width |
| Stitch column drift tolerance | fraction of **page** width |

Why this matters concretely: Apple's body text is 8.1pt, MRPL's is ~9pt in a much denser
layout, and a scanned 300 dpi page has no points at all. A threshold of "6 points" means
three different things across those. A threshold of "1.6 line pitches" means the same
thing in all of them, including after rasterisation.

Docstrum is the right primitive here specifically *because* it is self-parameterising —
it was designed for exactly this problem (segmenting documents of unknown font, scale and
skew) and it degrades gracefully rather than falling off a cliff when a page is unusual.

### 9.2 Structure-first detection: tables are defined, not recognised

**Architectural inversion from §S3.** Rather than proposing regions by appearance and
then recovering their grid, we compute alignment structure across the whole page and
define a table as a **maximal set of contiguous blocks sharing a consistent column
partition**.

```
for each page:
    1. words -> text lines            (Docstrum-calibrated, bottom-up)
    2. lines -> blocks                (horizontal rules where present, else line pitch)
    3. for each block, compute its column partition:
         - vertical rules             (exact where present)
         - maximal whitespace cover   (Breuel) -> candidate separators
         - dual-anchor projection     (L/R edge density, §S4.2)
    4. grow maximal runs of consecutive blocks whose column partitions agree
       within tolerance  ->  these runs ARE the table candidates
    5. classify each candidate: data table, or one of the named traps (§S3.4)
    6. recover ROWS inside each accepted candidate, using its column partition
```

> **A block is not a row.** This was corrected after building S2 and looking at
> the output. Apple p32 sets its income statement at ordinary leading with rules
> only under subtotals, so "Net sales:", "Products $294,866 …" and "Services
> 96,169 …" all land in one block — three table rows, one block. Nothing at
> stage 2 can separate them, because a second line *inside* a row (a wrapped
> cell) and a genuine new row are geometrically identical until you know where
> the columns are. So blocks are the **scope** for column analysis, and rows are
> recovered at step 6, after the partition is known. An earlier draft of this
> section called them "row bands", which would have misled every later reader.

Why this generalises better than the three-detector arbitration it replaces:

- **The ruled/borderless distinction disappears as an architectural seam.** Vertical rules
  become one more source of column-partition evidence, not a separate pipeline. A table
  ruled on its left half and borderless on its right — which exists, and which breaks
  every arbitration scheme — is handled without a special case.
- **Side-by-side tables separate for free.** Two independent column partitions coexisting
  in the same y-range simply produce two candidate runs.
- **Tables embedded in prose need no region proposal.** A three-row table inside a page of
  running text is a run of blocks whose column partition disagrees with the surrounding
  single-column blocks. Apple p30 is exactly this and it falls out with no detector tuned
  for it.
- **No appearance features are learned or tuned.** Column-partition agreement is a
  geometric predicate. There is nothing in it that can overfit to EDGAR output.

The trap classifier (§S3.4) still runs, and it gets *easier*: it now classifies a
well-formed structure rather than a fuzzy region, so "the right column is a monotone
integer sequence bounded by page count" is a clean test on actual reconstructed cells.

The three-detector framing from §S3 is retained internally as the **evidence sources**
feeding step 3, and `detector_votes_json` still records what each contributed — the
arbitration logic is what goes away, not the signals.

### 9.3 The classical stack, and what each part is for

All of these predate the deep-learning table literature, are deterministic, are cheap,
and are documented well enough to defend line by line in a live review.

| Method | Used for | Why this one |
|---|---|---|
| **Docstrum** (O'Gorman 1993) | per-page self-calibration, skew, line grouping | derives its own parameters from the page; the whole basis of §9.1 |
| **Maximal whitespace cover** (Breuel 2002) | column separators, side-by-side splitting | branch-and-bound over maximal empty rectangles; essentially parameter-free, and principled rather than heuristic |
| **Dual-anchor projection** (§S4.2, ours) | column boundary refinement | recovers right-aligned numeric columns that whitespace analysis smears (F2) |
| **Morphological line extraction / vector harvest** | ruled structure | exact on digital, standard on raster |
| **T-Recs-style word-block growing** (Kieninger 1998) | borderless column blocks, bottom-up | grows columns from word overlap with no global thresholds — a useful cross-check on step 3 |
| **Recursive XY-cut** (Nagy 1984) | coarse page segmentation | fast Manhattan-layout split; used only as a cheap pre-pass, since it fails on non-Manhattan |
| **Area Voronoi segmentation** (Kise 1998) | fallback for pages XY-cut mangles | robust to non-Manhattan and irregular spacing; heavier, so it runs only when XY-cut confidence is low |
| **Projection profiles** | row/column bands, ink density | the workhorse; only safe *because* §9.1 makes its thresholds scale-free |

Deliberately **not** in the stack: Hough transforms for rule detection (slower and less
accurate than morphology on axis-aligned rules, and rules in documents are axis-aligned by
construction), and RLSA (superseded by Docstrum for our purposes, and it needs absolute
smearing lengths — exactly the thing §9.1 bans).

### 9.4 Producer-stratified corpus and leave-one-out evaluation

**The layout family of a PDF is determined by its producer far more than by its industry.**
The three samples happen to be three different producers, which is better diversity than it
looks:

| Producer | Layout conventions it imposes |
|---|---|
| EDGAR HTML converter | horizontal rules only, `$` in separate cells, 8pt Arial, HTML-table-derived geometry |
| Word → iTextSharp | true ruled tables, multi-line cells, inline figures |
| Excel "Print to PDF" | frozen-pane reading-order scramble, cell-error literals, no rules at all |
| LaTeX (`booktabs`) | rules only above/below header and at bottom, generous row spacing |
| InDesign / annual-report design | tinted row bands instead of rules, custom fonts, non-Manhattan |
| Scanner → OCR'd PDF | skew, noise, unreliable text layer over an image |
| Tally / SAP / banking exports | trailing-sign negatives, fixed-width monospace, page-per-account |

So the corpus target is **~25 documents spanning at least 7 producers**, harvested now,
before building — SEC EDGAR filings, ICRA/CRISIL/CARE rating rationales, RBI and bank
annual reports, RERA quarterly filings, a LaTeX-typeset report, an InDesign annual report,
and at least three genuinely scanned documents. Collecting these is a day-one task that
runs in parallel with labelling and costs almost nothing.

**Evaluation is leave-one-producer-out.** Fit any tunable (the S3 scoring weights, the S9
calibration coefficients) on all producers but one, evaluate on the held-out producer,
rotate. The reported headline metric is the *mean across held-out producers*, not the
metric on the full set. A number produced this way is a genuine estimate of what happens
on Zenalyst's unseen documents; a number from fitting and evaluating on the same corpus is
not, and the gap between the two is itself worth reporting.

This also protects the calibration work directly (§S9): a confidence model fit and
evaluated on the same three documents will look beautifully calibrated and be worthless.
Leave-one-producer-out is the only honest way to claim the reliability diagram means
something.

### 9.5 Degradation, not failure

A general pipeline must have a defined behaviour on document families it has never seen.
Ours: when page statistics are unstable (bimodal font sizes, no dominant text angle, line
pitch histogram with no clear peak), the page is marked `LOW_CONFIDENCE_LAYOUT`, thresholds
fall back to document-level rather than page-level statistics, and **every cell extracted
from that page carries a confidence penalty**. An unfamiliar layout should produce flagged
output, not confident garbage — which is the same rule as everywhere else in this design,
applied one level up.

