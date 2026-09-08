# LIMITATIONS

Written to be read by someone deciding whether to trust this pipeline's output.
Everything here is a known gap, stated plainly, with what it would take to close.

---

## 0. Measured results (17 labelled tables)

A ground-truth set exists (`metrics/ground_truth/`, 17 labels: 15 positives and
2 negatives) and `metrics/eval.py` scores against it. Results in
`metrics/results.json`. Worth reading before anything else in this file:

| | |
|---|---|
| Detection, positives matched | **12 of 15** |
| Detection, negatives wrongly accepted | 0 of 2 (TOC and the MRPL figure page) |
| Cell exact match, on matched tables | **80.8%** (750 cells) |
| Apple p32 (income statement) | **25x4, structure 1.00, content 100%** |
| Cells flagged for review | 75 of 750 (10.0%) |
| **Flag precision** | **0.16** |
| **Flag recall** | **0.07** |
| **Silent errors** | **132 (17.6%)** |

**Read the trend, not the level.** Cell accuracy fell from 89.8% to 77.8% while
detection rose 11/15 -> 13/15, and the fall is an artifact of the metric, not a
regression. Three tables that were previously too broken to score at all now
match and are scored: Apple p57 at 43% (its `Form` column is still merged) and
MRPL p1 at 66.7% pull ~190 partial cells into a mean that previously contained
only the tables good enough to match. Over the same changes MRPL p4's complexity
table and MRPL p5's annexure both went to **structure 1.00, content 100%**, and
every table not named here is byte-identical. A per-table diff is the honest
view; `metrics/results.json` holds it.

Silent errors are concentrated where the *shape* disagrees with the label:
Shell p2 emitted as 7 columns against a 4-column label (2.8% content), Shell p3's
cash flow as 5 against 3, and p57's 4 against 5. Wrong cells in those matches are
usually right numbers sitting in the wrong column, shipped at 0.95 because type
and arithmetic both look fine. That is the failure mode the brief calls
unforgivable, and it is stated here rather than buried.

Splitting by who wrote the label matters, because most labels are not
independent of the extractor (§0b):

| label author | n cells | exact | silent errors |
|---|---|---|---|
| self (same author as extractor) | 394 | 85.8% | 12.2% |
| llm-assisted | 297 | 60.9% | 37.7% |
| user | 51 | **100.0%** | 0.0% |

The gap between `self` and `llm-assisted` is the clearest evidence in this repo
that self-authored labels flatter the extractor.

**Detection remaining.** 2 of 15 positives unmatched: Apple p1 (securities
registered, over-merged cover page, §4b) and MRPL p4 (rating history), still the
corpus's hardest structure -- spanning headers over Date/Rating pairs, which
needs S4.3 and is unbuilt (§4). Its content overlap improved 0.24 -> 0.43 once
ruled row boundaries were honoured, but the header remains flattened.

## 1. The labelled set is still too small to calibrate against

**13 labels, covering the 12 planned in `ARCHITECTURE.md` §7.**

Everything in §0 is computed over 418 cells from 7 matched tables. That is
still too small to fit a calibration model, and the per-document counts
elsewhere in this repo remain counts, not scores:

- "65 tables accepted" means 65 candidates survived the trap classifier. It does
  not mean 65 correct tables.
- "2,021 cells" is cells emitted, not cells correct.
- **Detection is scored by cell-content overlap, not IoU ≥ 0.7 as the brief
  specifies.** No ground-truth bounding boxes were hand-drawn, and deriving them
  from the extractor's own output would make the metric circular.
- `structure_similarity` is TEDS-Struct-*like*, not TEDS: it ignores spans,
  because the extractor does not produce spans.

`out/*/metrics.json` still carries `"measured_against_ground_truth": false`,
because a per-document run is not scored against labels — only `metrics/eval.py`
is.

## 2. Confidence is ranked, not calibrated

The brief asks for a calibrated signal: a 0.9 that means "9 of every 10 such
cells are correct", demonstrated with a reliability diagram. **What exists is a
ranking**, built from deterministic signals with weights *stated in `config.yaml`
rather than fitted*. §0 shows what that ranking is currently worth: flag
precision 0.13, flag recall 0.22, and 51 silent errors. The labelled set is
large enough to diagnose the problem and still too small to fit a calibration
model against.

Every artefact says so when `--no-calibration` is passed. With the default
frozen `calib/model_v1.json`, `metrics.json` carries
`"confidence_is_calibrated": true`. That model is a near-identity logistic plus
a mild isotonic curve fit on 418 labelled cells. A full PAV fit on that set
collapses 0.40–1.00 into one 0.855 bin and would flag every cell; that collapse
is overfitting, not calibration, and is not shipped. **Do not read a confidence
of 0.85 as an 85% probability of correctness on a held-out document.**

The design for closing this is in `DECISIONS.md` D7 (logistic + isotonic fitted
offline, coefficients frozen into the repo so calibration coexists with
byte-identical output). It is not implemented.

## 3. The review queue is far over budget

The brief targets **≤ 8% of cells flagged, covering ≥ 95% of actual errors.**
Currently **21.5% of cells are flagged** (Apple 22.5%, Shell.pdf 15.3%, MRPL
23.9%). On the labelled subset the flags covered **0%** of real errors (§0), so
the rate is not merely too high — it is high *and* uninformative.

The dominant reason code is `CROSSFOOT_FAIL`. Four separate over-firing causes
were found and fixed this session (header years being swept into sums;
subtractive subtotals treated as failures; percentage columns being summed;
table-level flags penalising every cell). It is still over-firing on financial
statements whose subtotals reconcile in ways that are neither plain addition nor
first-minus-rest — "Gross margin", "Profit before tax (III-IV)" and similar.

Until §1 and §2 are done, the flag rate cannot honestly be tuned: lowering it
without measuring error coverage would just be moving a number to look better.

## 4. Whole stages are missing

| Stage | Consequence for a user of the output |
|---|---|
| **S5 OCR** | Raster/hybrid pages with a thin text layer call Tesseract via PyMuPDF. **Digital pages are never re-OCR'd.** If Tesseract is not installed the page is marked `OCR_UNAVAILABLE` and extraction continues with the text layer (usually empty). No sample is scanned, so this path has **no accuracy measurement**. |
| **S6 stitching** | Consecutive page-tables merge when column-count + normalised right-edges match AND the continuation sits near the page top. A repeated header row is dropped. Ambiguous pairs stay separate (`STITCH_AMBIGUOUS`). MRPL p4 vs p5 annexure have different columns and must not stitch. |
| **S8 context** | Title, caption, section path, unit/scale, unique figures, scored table→figure links, and footnote-marker binding (current page + next two) are implemented. Context is not scored against the labels. |
| **S4 spans / headers** | `row_span` / `col_span` and `header_path` are produced geometrically. Multi-level spanning headers (MRPL p4) may still fail detection *before* spans run, if S3 emits two overlapping candidates. |

The SQLite schema populates `assets`, `table_assets`, `footnotes` and
`cell_footnotes` when those objects exist.

## 4b. Cover pages are over-merged (Apple p1)

Page 1 of a 10-K carries a form-style address block ("California / 94-2404110 /
One Apple Park Way / Cupertino, California / 95014") directly above a genuine
3-column table, "Securities registered pursuant to Section 12(b) of the Act".
The extractor merges the two and emits one 14x4 grid, so the cover block ships
as table data.

This is an **over-merge, not a plain false positive** -- the page really does
contain a table, and refusing the whole page would lose it. It is now labelled
(`apple_p001_securities_registered`) and scores 0.47 content overlap, just under
the 0.5 match threshold, so it counts as a detection miss rather than passing
silently.

**No rejection threshold was added, deliberately.** Neither candidate signal
separates this page from real tables on the labelled set:

| signal | this page | labelled TRUE tables |
|---|---|---|
| sparsest-column fill | 21% | 27% (Shell p3), 0% (Apple p30) |
| column alignments | one CENTRE, three MIXED | Shell p3 also has two MIXED |
| numeric share | 3% | MRPL's complexity table is 0% and real |

Any cutoff between 21% and 27% would be fitted to three documents and would
fail on the held-out set. The fix belongs at the merge stage in S3 -- a
form-style block of labelled fields is not a continuation of the table beneath
it -- not in a new trap threshold.

## 5. Known-bad output

**MRPL p4 (rating history) is wrong.** It is the hardest table in the sample
corpus — a two-level spanning header over `Date`/`Rating` pairs with almost every
cell wrapped over three or four lines — and it comes out as two overlapping
candidates of 11 and 10 columns with the header flattened. It needs span
detection and multi-line cell merging (§4). It is visible, unhidden, in
`viewer.html`.

**Row/wrap ambiguity is left unresolved by design.** A line that puts content
only in the label column, with no hanging indent, could be a wrapped label or a
section heading. We keep it as its own row and flag `ROW_WRAP_AMBIGUOUS` rather
than merge. Splitting a wrapped label is visible and recoverable; silently
merging two data rows destroys a number and is not. This inflates row counts on
tables with wrapped labels.

**`TRAP_TOO_FEW_ROWS` is the most common rejection on Apple (50 of 79).** Some of
those are certainly real tables of one or two rows that we are throwing away.
Without ground truth we cannot say how many, which is exactly why the rejection
is recorded with its reason rather than dropped.

**Apple p57 exhibit index: the `Form` column is still swallowed.** The stitched
p57-58 index extracts 50x4 against a labelled 51x5: the row defect is fixed, the
column defect is not. `Form` values (`8-K`, `10-Q`, `S-8`) are appended to the
`Description` cell. Pinned by `metrics/ground_truth/apple_p057_exhibit_index.json`.

The 12 missing rows this label also exposed are **fixed**, and were never a
recall failure -- the text was extracted, then destroyed by row segmentation.
The index ends with thirteen exhibits marked "filed herewith" (`10.18*`,
`19.1**`, `97.1*, **`), which carry no Form/Exhibit/Date values and so occupy
only two columns. S4's wrap test read every one of them as a continuation
line, because `_is_closed_value("10.18")` was True while
`_is_closed_value("10.18*")` was False -- a trailing footnote marker made a
finished value look like a fragment. All thirteen fused into the table's last
row: 18 physical lines, y=417..596, every exhibit number space-joined into one
cell. Markers are now stripped before the closedness test and nowhere else
(raw_text keeps them), which recovers 38 -> 50 rows and changes no other table
in the corpus. Regressed by
`test_footnote_marked_rows_are_not_fused_into_one_wrapped_row`.

Note what this cost in the headline numbers, because it is a measurement
artifact worth understanding: recovering the rows made p57 *match* its label
for the first time, which pulled 176 new cells into the comparison at 43%
content (it is still 4 columns against 5). Detection rose 11/15 -> 12/15 while
corpus cell accuracy fell 89.8% -> 76.8%. Every other labelled table is
byte-identical before and after. A table that was previously too broken to be
scored at all now gets scored, and the mean drops -- the extractor did not get
worse.

The column defect is *diagnosed*, and the obvious fix is measured and rejected.
`_columns_from_gutters` pads every token by `token_pad_unit_factor` (0.6 word
gaps each side) before marking coverage, then requires the surviving gap to
clear `min_gutter_unit_factor` (2.2 word gaps). Those compound: the raw gutter
must actually reach 3.4 word gaps, not the 2.2 the config names. Apple p57's
Description/Form gutter is 7.5pt against a 2.32pt word gap - 3.24 word gaps,
and empty on 100% of the page's 50 lines - so it loses by 0.38pt and the two
columns merge.

Removing the double-count (adding the padding back before the comparison, so
`min_gutter_unit_factor` means what it says) does recover the column: p57 goes
to 38x5, matches its label, and scores 99.0% content, lifting the corpus to
90.8% cell accuracy with silent errors down from 8.7% to 7.9%. It was still
reverted, because the same change introduces a spurious extra column in dense
financial statements and the run-merge test requires a stable column count:
Apple p40 splits 17x8 -> 6x9 + 11x8 (97.8% -> 89.8%) and Shell's balance sheet
splits 25x4 -> 20x5 + 22x4, dropping from a match at 98.0% content to no match
at all. The compounded threshold is doing real work; the improved headline was
p57's 68 newly-matched cells outweighing two broken balance sheets in the mean.

The right fix is an additional column signal rather than a looser whitespace
threshold - stable token-edge alignment, which is what actually distinguishes
p57's `Form` column (50 short tokens on one x) from p40's spurious gutter. That
is a structural change to S3, not a threshold move, and it is not attempted
here.

**Page titles no longer fracture the statement beneath them.** This was the
worst-behaved family in the corpus and it is now closed. A centred title lays
its lines at several different indents, and those indents partition into
columns: Apple p35 (shareholders' equity) came out SEVEN columns where the
statement has four. Because a merge may not lose columns, every block below was
then refused -- the page split into three tables, and the closing "Dividends
declared per share or RSU" row was stranded as a one-row candidate and rejected
as TRAP_TOO_FEW_ROWS despite plainly belonging to the statement.

Centred, digit-free lines are now held out of the column geometry (S3
`_heading_lines`). The test is per LINE rather than per block, because block
grouping does not respect the distinction -- on p35 the title's three lines are
grouped with "Years ended", which belongs to the column header, and that single
line pulls the block's bounding box far enough off-centre to defeat any
block-level test. Nothing is lost: S8 binds the same text as the table's
section context.

Measured: p35 21x4 in one table (was three), p33 14x4 (was three), and Apple
p34's balance sheet -- listed here for months as "split into two tables" --
came back as a single 37x3 against a 36-row label, structure 0.81 -> 0.93. No
other labelled table moved.

**A ruling line no longer outranks proximity when assigning a line to a block.**
A rule under a column header is drawn BELOW the header, so splitting the page at
it puts the header with whatever sits above -- usually the paragraph that
introduces the table. Apple p38 is the shape: "2024 2023 2022" sits 2.6pt above
the iPhone row and 6.0pt below the last line of the Note 2 prose, but the three
underlines at y=472.5 severed it from its own table and stranded it inside a
one-column prose block. The extracted table began at "iPhone" and carried no
years at all -- a header silently missing, which is worse than a visible error.

Where a RULE made the boundary, the last line of the block above now moves down
if it is nearer to the block below than to the rest of its own block. Comparing
two gaps needs no threshold and no page constant.

One extra condition earns its place. The moved line must contain a gutter-sized
internal gap -- it must be a ROW, not a phrase. Without that test the same rule
drags Apple p32's "Years ended" (two words at ordinary word spacing, spanning
three date columns) into the grid, where it has nowhere to go because spanning
headers are unrepresented (S4.3, §4) and it appends itself to a cell: a perfect
table's "September 30, 2023" became "Years ended September 30, 2023". With the
test, p38 gains its years and p32 stays exact.

Measured: cell accuracy 77.8% -> 80.8%, silent errors 20.7% -> 17.6%. Detection
reads 12/15 rather than 13/15, and that one is not a loss: Shell p2 previously
"matched" its label on region overlap while being **2.8% correct**, and now
falls below the overlap threshold instead. It is the same broken table either
way (§5) -- the metric stopped flattering it.

**Cross-footing now respects a statement's own scopes.** It was the single
largest source of false flags in the corpus -- 299 of 320 flagged cells (93%)
carried `CROSSFOOT_FAIL`, and Apple's deferred-tax note, whose every subtotal is
exact, had every figure on the page tinted.

Two defects, both general to sectioned statements rather than to any one page:

* A subtotal that could not be verified (fewer than two components above it)
  fell through to the line-item branch and was appended to the run, so it was
  double-counted against the NEXT total. "Total deferred tax assets, net" was
  carried into the liabilities section as though it were a line item.
* An unvalued heading did not end the run. "Deferred tax liabilities:" left the
  asset rows in scope, so the liabilities total was asked to reconcile against
  both sections: 23,946 against a stated 6,805.

A total row now always closes its run and is never a component of another, and a
row carrying a label but no figure in ANY column closes the scope. The whole-row
test matters -- a row missing a value in one column is an ordinary line item with
a gap (the exhibit index is full of them) and must not break a run.

Measured: flagged cells 18.8% -> 12.4%, flag precision 0.085 -> **0.129**, and
flag recall **unchanged at 0.083**. Every flag removed was a false positive; not
one genuine catch was lost, and Shell p3's real `#REF!` reconciliation failure
still reports. Apple p44 went from every numeric cell flagged to none.

This does not make the confidence signal calibrated (§2) -- it removes noise
from the flags, it does not add discrimination. Silent errors are unchanged at
132.

**Rows now carry a KIND, and the hierarchy uses it.** The row-label hierarchy
was built from indentation alone -- left-edge clustering of the label column --
and that is not enough to say what a row IS. A financial statement routinely
sets its totals FURTHER RIGHT than the items they total, so a geometric
hierarchy makes the total a child of the last line item above it:
`Net sales: > Services > Total net sales` claims the total belongs to Services.
Measured on Apple's 10-K, **45 of 67 total rows hung from a data row**.

Every row is now classified before any hierarchy is built:

* `section` -- labelled, carries no figure in ANY column ("Net sales:")
* `total`   -- its label announces that it closes a scope
* `data`    -- a line item

A total pops the data levels off the stack before attaching, whatever the
indentation says, so it lands as a SIBLING of the items it totals:
`Net sales: > Total net sales`. Measured: **45 wrong -> 0**, and no labelled
table moved (the eval scores cell content, not paths).

The kind is stored on the row rather than re-derived, because it is the same
distinction several stages need: S9's cross-foot scopes, an Excel or JSON export
that has to indent a statement, and any review UI that wants to show structure.
That is the first step of a typed table tree; the tree itself (Section / Data /
Subtotal / Footnotes as nodes, rather than a flat row list carrying a kind) is
NOT built, and remains the right shape for this pipeline to grow into.

**The table is now a tree of scopes (S4.7), and S9 consumes it.** Cross-footing
no longer rebuilds scopes from a running list; it iterates `table.sections`.
Two bugs surfaced immediately that the flat code could not even express:

* A line item that dedents out of a sub-scope closes it. Shell's balance sheet
  puts "(f) Deferred Tax Assets" at the same indent as the "(e) Financial
  Assets" heading above it -- a sibling, not a member -- and it was being
  swallowed, taking the section's total with it.
* A parent's total covers its CHILD SCOPES, not only its direct members.
  "Total Current Assets" covers Inventories and everything under the
  "(b) Financial Assets" sub-heading; reconciled against direct members alone
  it was out by thousands.

Measured across the corpus: flagged cells 12.4% -> **10.0%**, flag precision
0.129 -> **0.16**, recall unchanged at 0.083. Shell's cross-foot failures fell
from 38 flagged cells to 1.

**How much of this assumes a financial document.** Four of the five row kinds are
structural and say nothing about the domain: `section` (a labelled row with no
figures), `header` (figures with no label), `blank`, and `data`. Only `total`
rests on vocabulary, and that vocabulary now lives in `config.yaml` rather than
in code, in two tiers:

* `total_words_strong` -- "total", "subtotal", "aggregate". These mean a closed
  scope in essentially any table.
* `total_words_weak` -- "net ", "gross ", "profit ", "loss ", "balance at".
  These mean it in a financial statement and something else entirely elsewhere:
  "Net weight" and "Gross mass" on a specification sheet are line items.

Weak terms are honoured ONLY in a table that also contains a strong one. A
statement that computes a "Gross margin" states a "Total" somewhere too; a spec
sheet states neither, and is left alone. Pinned by
`test_a_non_financial_table_is_not_read_as_a_statement`.

The residual assumption is honest and worth stating: the lexicon is English, and
a non-English document gets no `total` rows at all. It degrades to structure
only -- sections and data still nest correctly, and cross-footing simply never
fires, which is the right failure for a table whose totals we cannot identify.

## 6. Generalisation: what is defended, and what is not

The held-out set is different documents, and the realistic failure mode is a
pipeline that scores well here and collapses there. Three defences are in place:

- **No absolute measurement constants.** Every threshold is a multiple of a
  page-derived Docstrum statistic or a named `config.yaml` entry, enforced by an
  AST lint test that fails the build on any float literal outside
  `{0, 0.5, 1, 2, inf}` in S2/S3/S4 code.
- **No sample-specific strings.** No string from any sample document appears in
  any detection rule. The trap classifier, furniture detection and cross-foot
  logic are all stated as document-general properties.
- **Structure-first detection.** Tables are defined by column-partition
  consistency rather than recognised by appearance, so there is no appearance
  model to overfit.

**What is not defended:** the pipeline has only ever been run against three
documents from three producers. `DECISIONS.md` D21 specifies a ~25-document
corpus across ≥ 7 producers with leave-one-producer-out evaluation; that corpus
has not been collected. Layout families with no representation here — LaTeX
`booktabs`, InDesign annual reports using tinted row bands instead of rules,
Tally/SAP exports, and anything scanned — are **untested**, and I would not
predict their behaviour.

Several thresholds (`gutter_persistence: 0.80`, `min_gutter_unit_factor: 2.20`,
`symbol_min_purity: 0.80`, the confidence weights) are judgement calls, not
fitted values. They are all in one file with stated units, which makes them
auditable, but it does not make them right.

## 7. Determinism and budget — what is actually proven

**Proven:** two runs produce byte-identical `state/`, `viewer.html`, `tables.json`,
`logs/run.jsonl` and debug PNGs. Verified by `diff -r` on the 121-page Apple
document, and by a test in the suite. Offline operation is proven by a test that
makes `socket.socket` raise for the duration of a full run — not merely asserted.

**Not proven:** the resource budget has been measured on this machine (12.5 s and
41 MB traced heap for 121 pages) but **not on the specified 4 vCPU / 8 GB
container**, and not with the OCR path, which does not exist and which will
dominate the cost on scanned documents. The current headroom is large — roughly
10 s against a 1200 s budget — but that headroom is for a pipeline still missing
OCR, stitching, spans and calibration.

`Dockerfile` builds and runs with no network, but has not been exercised in a
genuinely network-isolated environment.

## 8. Not done at all

- `metrics/` evaluation harness — no IoU matching, no TEDS-Struct implementation.
- Baseline comparison against camelot / tabula / pdfplumber, which the brief
  encourages and which would show where this approach wins and loses.
- The learned-model ensemble path (Table Transformer as one vote) and its
  ablation — the pure-classical path is all that exists, which happens to satisfy
  the +4 bonus condition trivially but has no ensemble to be compared against.
- Chart data recovery (+3) and the active-learning loop (+3).
- The screen recording.
