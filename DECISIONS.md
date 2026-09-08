# DECISIONS

Every non-obvious choice, the alternatives considered, and why they were rejected.
Evidence references (F1–F10) point at measured properties of the sample corpus,
documented in `ARCHITECTURE.md` §0.

---

## D1 — Column detection uses dual-anchor (left *and* right edge) projection

**Decision.** Infer columns from two independent x-histograms — token left edges and
token right edges — and reconcile them, rather than from whitespace gutters.

**Rejected: whitespace-gutter projection alone** (the classic approach, and what the
brief's Figure 3B describes). Measured on Apple p32: the right-edge histogram gives three
sharp peaks with 19/18/18 votes; the left-edge histogram is flat noise (F2). Financial
tables right-align numbers, so gutter minima between numeric columns are *narrow and
unstable* — a number one digit longer moves the gutter. The right edge does not move.

**Rejected: fixed column templates per producer.** Would work well on EDGAR output and
fail on everything else. The held-out set is explicitly not the sample set.

**Cost.** Two histograms instead of one, plus a reconciliation rule where both fire. The
reconciliation is the fiddly part and will need care on tables mixing centred headers
over right-aligned data.

---

## D2 — Three detectors, arbitrated by evidence score, not two arbitrated by priority

**Decision.** Add a third "partial-rule / hybrid" detector that takes rows from horizontal
rules and columns from geometry, and pick between detectors by comparing evidence scores.

**Rejected: the brief's literal "two detectors and a rule for arbitrating."** I am
deliberately departing from the framing, and the reason is measurable: Apple has **zero**
vertical rules across 121 pages and Shell.pdf has zero (F1). A ruled detector needs
intersections it will never find; a borderless detector throws away perfectly exact row
boundaries the producer drew. Two of three sample documents are 100% in this third regime,
and the brief itself names it ("tables ruled horizontally but not vertically, which
neither approach handles alone") — it just doesn't give it a detector.

**Rejected: priority ordering (try ruled, fall back to borderless).** Priority hides
disagreement. Scoring surfaces it, and `detector_agreement` becomes a confidence feature
(S9) — a region only one detector likes is a region worth flagging.

**Cost.** Three code paths to maintain, and a scoring function whose weights need tuning
against the labelled set rather than being obviously right.

---

## D3 — All structure is derived from coordinates; stream order is never trusted

**Decision.** Reading order is used only as a final tie-break in sorts, for determinism.

**Rejected: reading-order-based table assembly** (what `pdfplumber.extract_table` and most
tutorials do). Shell.pdf p2 emits the whole P&L body *before* its header row, because
Excel's print driver writes frozen panes last (F4). Any "first line is the header" rule
promotes `Revenue from Operations / 16,195.63 / 6,680.45` to a column header. This is not
a rare pathology; it is what one of three provided samples does on its main statement page.

**Cost.** More geometry code, and multi-line cell assembly becomes an explicit problem
(S4.1) rather than something reading order gives you for free.

---

## D4 — Symbol columns are absorbed, not emitted

**Decision.** A narrow column that is ≥ 80% single symbols (`$`, `₹`, `%`, `*`) merges into
its neighbour, with the merge recorded on the column.

**Rejected: emitting them as data columns.** Apple p32 would produce a 7-column table where
the document means 4, TEDS-Struct would score it as badly wrong, and every downstream sum
would need to know which columns are decorative.

**Rejected: dropping symbol tokens at extraction time.** Destructive — it loses the currency
identity, which S8 needs for `unit`, and it loses footnote markers, which S8 needs for
cell-level footnote binding. Absorption keeps the information and moves it.

**Cost.** The 80% / 2.5%-width thresholds are tuned, not principled. A genuine one-character
data column (a single-letter grade column, say) would be wrongly absorbed. Mitigation: the
rule requires the symbol set membership, not just short width, so `A`/`B`/`C` survives.

---

## D5 — Trap rejection is a named-reason classifier, not a score threshold

**Decision.** Six explicit rules (`TRAP_TOC`, `TRAP_INDEX`, `TRAP_PROSE_2COL`,
`TRAP_CALLOUT`, `TRAP_UNDERLINE`, `TRAP_KEYVALUE`), each writing its reason code into
`issues` even when the region is rejected.

**Rejected: a single "tableness" score with a cutoff.** A TOC scores *high* on tableness —
it is beautifully regular, two clean columns, perfect right-alignment of page numbers
(F7, Apple p3). Recall-tuned detectors love it. No threshold separates a TOC from a
two-column data table; only a semantic-ish rule does (the right column is a monotone-ish
integer sequence bounded by the page count).

**Rejected: rejecting silently.** If a rejection is wrong, and it's silent, it is
indistinguishable from a detection miss during the live review. A rejection with a code is
debuggable in one grep.

**Cost.** Six hand-written rules is exactly the kind of thing that overfits to the sample
corpus. Mitigation: each rule is stated in terms of a document-general property, not a
sample-specific string, and each is unit-tested on a synthetic positive and negative.

---

## D6 — Stitching requires signature **and** continuity, and refuses when they disagree

**Decision.** Merge only on both; on partial match, keep tables separate and write
`STITCH_AMBIGUOUS`.

**Rejected: merging on header repetition.** The brief says the header often does not
repeat, and MRPL's rating-history table crosses a page break with no repeated header and
no `(Contd.)` marker.

**Rejected: merging on `(Contd.)` markers.** Same reason — the brief explicitly warns they
are usually absent. Marker presence is a bonus vote only.

**Rejected: merging aggressively and splitting later.** A wrong merge corrupts
`row_label_path` and every cross-foot in the combined table; an unnecessary split costs
8 stitching points but leaves every cell correct. Under "the one rule" (a wrong cell
shipped confidently is the only unforgivable failure), the asymmetry is clear.

**Cost.** We will under-merge on tables whose column widths genuinely drift more than 1.5%
of page width across pages. That is a known, measured, reported loss rather than a silent one.

---

## D7 — Confidence is calibrated with isotonic regression, and the fitted model is frozen into the repo

**Decision.** Fit logistic + isotonic offline on the labelled set; commit the coefficients
as `calib/model_v1.json`; runtime only evaluates.

**Rejected: hand-tuned confidence weights.** They produce a number that ranks cells but
does not *mean* anything. The brief asks for a reliability diagram, which is precisely a
test of whether 0.9 means 90%. Hand-tuned weights fail that test and the 18 points with it.

**Rejected: fitting at runtime.** Breaks determinism (the fit depends on the document) and
arguably breaks the spirit of the offline constraint.

**Rejected: Platt scaling instead of isotonic.** Platt assumes a sigmoid link; our score
distribution is lumpy because several features are near-binary (`type_conformity`,
`crossfoot_delta == 0`). Isotonic is non-parametric and handles the lumps. Cost: isotonic
needs more labelled data and can overfit, so we bin conservatively and report the labelled
set size honestly.

**Note on the constraint.** A frozen logistic + isotonic model is not a language model and
does not read document content — it consumes numeric features computed from geometry and
arithmetic. It satisfies §3.1. If a reviewer disagrees, `--no-calibration` falls back to a
documented hand-weighted score, and the metrics for both are reported.

---

## D8 — We never repair the document

**Decision.** `#REF!` ships as `#REF!` with type `error`. A subtotal that doesn't reconcile
is flagged, not corrected. A subtotal that depends on an `error` cell raises
`CROSSFOOT_UNVERIFIABLE`, distinct from `CROSSFOOT_FAIL`.

**Rejected: coercing error literals to null or zero.** Both are lies with different
flavours. Zero corrupts sums; null loses the fact that the source is broken, which is
exactly what a financial reviewer needs to see.

**Rejected: "fixing" a failing cross-foot by re-reading the cells.** This is the trap the
brief sets deliberately ("one that does not reconcile, because the source has a genuine
typo. Flag it; do not silently fix it"), and Shell.pdf p3 contains the naturally-occurring
version (F5).

---

## D9 — Five-way distinction between empty, `-`, `–`, `Nil`, `NA`, and `0`

**Decision.** Six surface forms, five semantic types, all preserved.

**Rejected: normalising all of them to null.** Shell.pdf's balance sheet uses `-` for
"nil this period" in columns where `0.00` also appears for measured zero. Collapsing them
means a reviewer cannot tell "we had none" from "we didn't measure" from "we measured zero."
No downstream process can recover the distinction once it's gone.

**Cost.** Downstream SQL is slightly more verbose (`WHERE value_type IN ('nil','dash')`).
Worth it.

---

## D10 — Header semantics without a language model

**Decision.** `header_path` is the literal spanned text, in tree order. We do **not** attempt
to canonicalise "Revenue" / "Total income" / "Net sales" to a common concept.

**Rejected: an offline embedding model or synonym table for header normalisation.** The
brief's clarification §12 answers this directly and absolutely: "Can I use a language model
offline, just for column-header semantics? No." An embedding model is a language model.

**Rejected: a hand-built financial synonym dictionary.** Not forbidden, but it is a
disguised knowledge base that would be tuned to the samples, would fail on the held-out
documents, and would introduce exactly the kind of confident-wrong mapping the assignment
is built to punish. The schema instead makes literal search easy (D11), which is what the
brief's litmus query actually asks for.

---

## D11 — `header_path` is stored *both* as JSON and as a normalised child table

**Decision.** Keep `columns.header_path` (JSON, for round-tripping) and add
`column_header_tokens(column_id, level, token)`.

**Rejected: JSON only.** The brief's stated litmus test — "every cell whose column header
path contains 'Revenue', with its page number and unit" — becomes
`WHERE header_path LIKE '%Revenue%'`, which is a full scan, is unindexable, and matches
"Revenue" inside "Deferred Revenue Adjustment" without being able to say at which header
level it matched. The brief says: "If that query is awkward, your schema is wrong."

**Rejected: normalised only.** Loses ordering fidelity on round-trip and makes the manifest
dump awkward.

**Cost.** Duplicate storage, and a consistency invariant to test (there is a unit test that
regenerates the child table from the JSON and asserts equality).

---

## D12 — Vector-first line extraction, rasterise only when forced

**Decision.** On digital pages, harvest rules from `page.get_drawings()` vector primitives.
Rasterise only raster/hybrid pages, only at OCR-required DPI.

**Rejected: rasterise everything and use OpenCV morphology uniformly** (the standard
recipe). On Apple's 121 pages at 200 dpi that is ~121 × 1700×2200 px of morphology, which
is most of the 20-minute budget spent recovering lines that are already in the file as
exact coordinates. It is also *less* accurate — rasterising a 0.5pt hairline and then
opening it can lose it entirely.

**Cost.** Two implementations of line extraction. Mitigated by having both emit the same
`h_lines[] / v_lines[]` structure, so everything downstream is shared and the raster path is
exercised by the rasterisation-trick tests (§7.2).

---

## D13 — Learned models are optional and ablated by default in reporting

**Decision.** Table Transformer (or similar) may join as one vote in S3 arbitration, weights
vendored in-image. Every metric is reported **twice**: with and without. The pure-classical
configuration is the headline number.

**Rejected: making the detector the primary signal.** §3.1 forbids it being the sole basis
for structure, and the +4 bonus explicitly rewards a pure-classical path within 10 points of
the best configuration. Building classical-first and adding the model as a vote gets both;
building model-first and trying to remove it later gets neither.

**Rejected: skipping learned models entirely.** They are genuinely good at borderless region
*proposal*, which is our weakest classical area. As one vote with an ablation switch, the
cost is bounded.

---

## D14 — Table→figure association is a scored relation with the score exposed

**Decision.** Additive score over same-page / shared-numbering / textual-reference /
adjacency / series-count; `relation_confidence` = the score; two relation strengths
(`visualises` ≥ 0.55, `co-located` ≥ 0.35) rather than one.

**Rejected: nearest-figure-on-page.** MRPL pages 1–6 each carry three logo images (F6);
nearest-neighbour would attach a footer logo to every table in the document.

**Rejected: numbering-only (Table 4.2 ↔ Figure 4.1).** Requires both to be numbered. MRPL's
p7 chart has no figure number at all.

**Cost.** Weights are chosen by judgement, not fitted — there is not enough labelled
association data in a 3-document corpus to fit them. This is stated in `LIMITATIONS.md`.

---

## D15 — Page furniture is detected cross-document, before anything else

**Decision.** Repeating headers, footers *and images* are identified by cross-page
frequency (same normalised text or same image xref at IoU ≥ 0.95 on ≥ 40% of pages) and
excluded from both table candidates and assets — but retained in the page model.

**Rejected: fixed top/bottom margin cropping.** Loses genuine content on pages where a table
starts high, and keeps furniture on pages where the footer is tall.

**Rejected: discarding furniture entirely.** Footers and headers frequently carry the
document-level unit or scale declaration and the "Sensitivity Label" style classification
that a reviewer may care about. We exclude it from extraction, not from the record.

---

## D16 — Confidence features are persisted per cell

**Decision.** `cell_features(cell_id, feature_name, value)` is written for every cell.

**Rejected: storing the score only.** During the live review the question will be "why did
you flag this cell?" — and "the model said 0.42" is not an answer. With features persisted,
the answer is a one-row query. It also makes the calibration work debuggable at 2 a.m. on
day four, which is the actual reason.

**Cost.** Row count. A 121-page document with ~15k cells × ~12 features is ~180k rows —
trivial for SQLite, but it does mean the feature table needs an index on `cell_id` and
should be the first thing dropped if the budget gets tight (it is behind a config flag).

---

## D17 — Excel writes native types only above the calibrated 0.95 band

**Decision.** Above the band, write a real number/date; below it, write the raw string and
tint the cell.

**Rejected: always writing native types.** Produces a spreadsheet that looks
authoritative everywhere, which is precisely the silent-error failure mode transplanted
into Excel.

**Rejected: always writing strings.** Safe but useless — the file can't be summed, and the
brief asks for native types "where you are confident."

**Cost.** A file with mixed types in one column. That is the honest representation of a
document we are not certain about, and the Issues sheet explains every instance.

---

## D18 — Ground truth is built before the extractor, and includes negatives

**Decision.** Day 1 is 12 hand-labelled tables (§7 of `ARCHITECTURE.md`), including
labelled *non*-tables, plus the seeded rasterisation harness for OCR ground truth.

**Rejected: labelling after building, on whatever the extractor produces.** That measures
self-consistency, not correctness, and it biases the label set toward the things the
extractor already finds — the exact failure that makes a calibration curve look great and
mean nothing.

**Rejected: labelling only positive tables.** Trap rejection is a scored criterion; without
labelled negatives there is no denominator for false-positive rate.

---

## D19 — Structure-first detection replaces detector arbitration

**Decision.** Compute column-alignment structure across the whole page, then define a
table as a maximal run of contiguous blocks sharing a consistent column partition
(`ARCHITECTURE.md` §9.2). The three detectors of D2 survive as *evidence sources* feeding
the column partition; the arbitration logic between them does not.

**This supersedes D2's arbitration mechanism.** D2's finding stands — vertical rules are
absent from real financial PDFs, so a ruled-vs-borderless split is the wrong seam. D19 is
the stronger version of the same conclusion: don't arbitrate between detectors, dissolve
the distinction. Vertical rules become one input to a column partition rather than the
trigger for a separate pipeline.

**Rejected: region-proposal-then-structure** (the standard pipeline, and D2's version).
Region proposal is the stage where appearance features live, and appearance features are
where overfitting to a three-document corpus happens. A tuned "tableness" score learned on
EDGAR output tells you very little about an InDesign annual report with tinted row bands
and no rules at all.

**Rejected: a learned region detector as the primary proposer.** Forbidden as sole basis
by §3.1, and it relocates rather than solves the generalisation problem — now the question
is what the model was trained on.

**Cost.** More expensive: we compute column partitions for every block on every page,
including pages with no tables. Mitigated because the partition computation is cheap on the
text layer and most pages resolve to "one column" in a few microseconds. Also: a genuinely
irregular table whose column partition shifts mid-table will now be split into two runs
rather than detected as one messy region. That is a real loss, it is measurable, and the
`STITCH_AMBIGUOUS`-style refusal path already covers reporting it.

---

## D20 — No absolute measurement constants; all thresholds are Docstrum-relative

**Decision.** Every threshold in detection and structure code is a multiple of a
page-derived statistic (`line_pitch`, `within_line_word_gap`, modal font size, page width)
or a named config entry. Enforced by a lint test.

**Rejected: point-valued thresholds tuned on the samples.** Apple is 8.1pt Arial; MRPL is
denser; a 300 dpi scan has no points at all. "6 points" means three different things across
those; "1.6 line pitches" means one thing everywhere, including post-rasterisation.

**Rejected: per-producer threshold profiles.** Tempting — the producer string is available
and predictive — but it is a lookup table that silently does nothing on the first producer
we haven't seen, which is precisely the held-out case. Producer is used for *stratifying
evaluation* (D21), never for selecting runtime behaviour.

**Why Docstrum specifically** (O'Gorman 1993) rather than a simpler line-gap estimate: the
nearest-neighbour distance histogram is bimodal in a way that separates within-line word
spacing from between-line spacing without being told which is which, and the angle
histogram yields skew from the same computation. One pass gives three parameters, and it
was designed for documents of unknown font, scale and skew — which is our exact situation
on the held-out set.

**Cost.** Docstrum is O(n log n) per page over word tokens and must run before anything
else, on every page. Measured cost goes in `metrics.json`. Pages where the histograms have
no clear peak fall back to document-level statistics and carry a confidence penalty (§9.5)
rather than silently using a default.

---

## D21 — Leave-one-producer-out evaluation, and a producer-stratified corpus

**Decision.** Build a ~25-document corpus spanning ≥ 7 PDF producers. Fit every tunable
(S3 weights, S9 calibration) on all producers but one; evaluate on the held-out producer;
rotate; report the mean across held-out producers as the headline number.

**Rejected: reporting metrics on the labelled set the model was fit on.** It measures
memorisation. For the calibration work (18 points) it is actively misleading: a confidence
model fit and evaluated on three documents produces a beautiful reliability diagram that
predicts nothing about the held-out set.

**Rejected: a random train/test split of the labelled tables.** Tables from the same
document share producer, font, layout conventions and often literal column structure, so a
random split leaks almost everything. The document — really the *producer* — is the correct
unit of independence.

**Rejected: more documents from the same three sources.** Adds labelling cost without
adding layout diversity, which is the axis that actually varies in the held-out set.

**Cost.** Labelling budget spread thinner across more documents, so fewer tables labelled
per document. Accepted deliberately: breadth of producer coverage buys more here than depth
within one filing. The gap between same-corpus and leave-one-out metrics is itself reported —
it is the most honest single number about how this will behave on documents we've never seen.

---

## Open questions to resolve during build

1. **Reconciliation when L-anchor and R-anchor disagree on column count.** Current plan is
   sharper-peak-wins with the loser retained as evidence. Needs testing against MRPL p4's
   two-level header, where header cells are centred over right-aligned data.
2. **Indent-level clustering when a table has exactly two levels but ragged label lengths.**
   The largest-gap heuristic for choosing k may over-split. Fallback: cap at the number of
   distinct left-edge values within 2pt tolerance.
3. **Whether `cell_features` survives the resource budget** on a 400-page document. Flagged,
   measured, and behind a config switch.
4. **Rupee-glyph recovery (F9).** We can detect that the glyph is wrong; recovering the
   intended symbol from the font's `ToUnicode` map may or may not work on Shell.pdf. If it
   doesn't, the unit is reported as ambiguous rather than guessed.
