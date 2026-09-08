"""S9 -- deterministic validation signals, per-cell confidence, review queue.

READ THIS BEFORE TRUSTING A NUMBER FROM THIS MODULE.

The confidence produced here is **ranked, not calibrated**. The brief asks for a
calibrated signal with a reliability diagram -- a 0.9 that means "9 of every 10
such cells are correct" -- and that requires fitting against a hand-labelled set
which does not exist yet. What this module produces is a defensible ORDERING of
cells by how suspicious they are, built from deterministic signals, with the
weights stated in config rather than learned.

That distinction is the whole point of the assignment, so it is surfaced rather
than buried: every artefact this module writes is stamped ``uncalibrated``, and
LIMITATIONS.md says the same. Reporting a made-up probability as if it were
calibrated would be precisely the silent-error failure the brief is built around.

Signals implemented here (all deterministic, all cheap):

  * type conformity -- a string sitting in a numeric column
  * source error values -- ``#REF!`` and friends
  * parse ambiguity -- comma grouping matching neither convention
  * unit glyph ambiguity -- a currency symbol the font encoded wrongly
  * cross-foot -- do a run of line items sum to the subtotal below them
  * structural flags inherited from S3/S4 (low partition support, wrap ambiguity)
  * split-header fragments -- a 4-digit year under a cell ending in a comma,
    which every other signal treats as a clean integer
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from ..config import Config
from ..model import Cell, Issue, Page, Table, ValueType
from ..s7_normalise.values import BLANK_TYPES, NUMERIC_TYPES

# Row labels that assert a total. Deliberately generic: no string here comes
# from any sample document.
_TOTAL_WORDS = ("total", "sub-total", "subtotal", "aggregate", "net ", "grand")
_YEAR_FRAGMENT = re.compile(r"^\d{4}$")


def _is_line_item(label) -> bool:
    """Does this row's label column name a line item?

    A component of a subtotal always has a name. A header row does not -- its
    label column holds another column heading, typically a year. Apple's segment
    table headers "2024 | Change | 2023 | ..." were being swept into the run
    beneath them, and every failure came back with a residual of exactly -2024,
    -2023 or -2022: the year itself, added to the sum of the segments.

    Requiring a non-numeric label is general (a line item is named) and it makes
    the residual meaningful again.
    """
    if label is None:
        return False
    if not label.raw_text.strip():
        return False
    return label.value_type not in NUMERIC_TYPES


def _reconciliation_delta(total: float, parts: list[float]) -> Decimal:
    """Smallest residual across the readings a subtotal might be using.

    A financial statement mixes ADDITIVE subtotals ("Total income (I+II)") with
    SUBTRACTIVE ones ("Profit before tax (III-IV)", "Net income" = income less
    tax). Testing only the sum turns every subtraction into a CROSSFOOT_FAIL --
    on Apple that was the single largest source of flags, and every one of them
    was a false positive against arithmetic the document performs correctly.

    Both readings are tried and the better residual wins. A run that reconciles
    under neither is genuinely worth a human's attention; a run that reconciles
    under either is not, and quietly consuming a reviewer's 8% budget with
    subtractions would bury the cells that are actually wrong.
    """
    t = Decimal(str(total))
    d = [Decimal(str(p)) for p in parts]
    additive = t - sum(d)
    # First item less the rest: the shape of "opening balance less outflows".
    subtractive = t - (d[0] - sum(d[1:])) if len(d) >= 2 else additive
    return min(additive, subtractive, key=abs)


def _is_section_heading(table: Table, row_idx: int) -> bool:
    """A labelled row that carries no figure in ANY column.

    "Deferred tax assets:" and "Operating expenses:" are scope boundaries, not
    line items. The test is deliberately whole-row: a row missing a value in
    ONE column is an ordinary line item with a gap (the exhibit index is full of
    them) and must not break a run, whereas a row with no figures anywhere is a
    heading.
    """
    labelled = False
    for c in table.cells:
        if c.row_idx != row_idx:
            continue
        if c.col_idx == 0 and c.raw_text.strip():
            labelled = True
        if c.col_idx != 0 and c.value_type in NUMERIC_TYPES:
            return False
    return labelled


def _is_total_label(text: str) -> bool:
    low = text.strip().lower()
    return any(w in low for w in _TOTAL_WORDS)


@dataclass
class ReviewItem:
    page_no: int
    table_index: int
    row_idx: int
    col_idx: int
    raw_text: str
    confidence: float
    codes: tuple[str, ...]

    @property
    def priority(self) -> float:
        return 1.0 - self.confidence


def _cross_foot(table: Table, cfg: Config) -> list[Issue]:
    """Reconcile each scope of the table against the totals that close it.

    The scopes come from S4.7's tree, not from a running list rebuilt here.
    That matters beyond tidiness: the two worst false-positive families in this
    pipeline were both S9 disagreeing with S4 about what a scope is. An
    unverifiable subtotal was carried forward as though it were a line item of
    the NEXT sum, and an unvalued heading did not end the run, so a section was
    reconciled against the one above it. Apple's deferred-tax note -- whose
    every subtotal is exact -- had all 299 of its figures flagged.

    A PASS raises confidence on every cell in the reconciled set, not just the
    total: arithmetic agreement is evidence about all the numbers that produced
    it. A FAIL lowers all of them and is reported -- never repaired. Shell p3
    holds a subtotal that genuinely cannot reconcile because the source has live
    ``#REF!`` cells, and that gets its own code so it is not confused with an
    extraction error (ARCHITECTURE.md F5).
    """
    tol = Decimal(str(cfg.f("confidence.crossfoot_tolerance")))
    issues: list[Issue] = []
    by_rc = {(c.row_idx, c.col_idx): c for c in table.cells}

    for col in table.columns:
        if col.inferred_type not in NUMERIC_TYPES:
            continue
        if col.inferred_type is ValueType.PERCENT:
            # Percentages do not sum. Apple's segment tables interleave a
            # "Change %" column between the year columns, and cross-footing it
            # asks whether 3% + 7% + (8)% + 3% + 4% equals the 2% shown against
            # the total -- a question with no meaning, whose answer was flagging
            # every growth figure in the document.
            continue

        for sec in table.sections:
            for member_rows, subtotal_row in sec.groups:
                if len(member_rows) < 2:
                    # One component is not a reconciliation, it is a restatement.
                    continue
                total_cell = by_rc.get((subtotal_row, col.index))
                if total_cell is None or total_cell.normalized_value is None:
                    continue
                if total_cell.value_type in BLANK_TYPES:
                    continue

                members = [by_rc.get((r, col.index)) for r in member_rows]
                if any(c is not None and c.value_type is ValueType.ERROR for c in members):
                    issues.append(
                        Issue(
                            severity="info",
                            code="CROSSFOOT_UNVERIFIABLE",
                            message=(
                                f"col {col.index} section {sec.label!r}: "
                                "a component cell holds a source error value"
                            ),
                            row_idx=subtotal_row,
                            col_idx=col.index,
                        )
                    )
                    continue

                parts = [
                    c.normalized_value
                    for c in members
                    if c is not None and c.normalized_value is not None
                ]
                if len(parts) < 2:
                    continue

                delta = _reconciliation_delta(total_cell.normalized_value, parts)
                agreed = abs(delta) <= tol
                for r in member_rows + (subtotal_row,):
                    c = by_rc.get((r, col.index))
                    if c is not None:
                        c.signals["crossfoot"] = 1.0 if agreed else -1.0
                if not agreed:
                    issues.append(
                        Issue(
                            severity="warning",
                            code="CROSSFOOT_FAIL",
                            message=(
                                f"col {col.index} section {sec.label!r} rows "
                                f"{member_rows[0]}-{subtotal_row}: "
                                f"sum differs from stated total by {delta}"
                            ),
                            row_idx=subtotal_row,
                            col_idx=col.index,
                        )
                    )
    return issues


def _looks_like_header_row(table: Table) -> bool:
    first = [c for c in table.cells if c.row_idx == 0 and c.raw_text.strip()]
    if not first:
        return False
    non_numeric = sum(1 for c in first if c.value_type not in NUMERIC_TYPES)
    return non_numeric >= len(first) - 1


def _mark_split_headers(table: Table) -> None:
    """Flag the split-header shape the other signals cannot see.

    A row of 4-digit years sitting directly under cells that end in a comma
    ("September 28," / "2024") is a header the extractor failed to join. Every
    other signal -- type conformity, parse, arithmetic -- treats the year as a
    clean integer and would ship it at the base score. This is the silent-error
    shape measured on the labelled set: three cells wrong, all at 0.95, none
    flagged. The wrap rule in S4 is supposed to join them; this is the
    safety net for when it does not.
    """
    if len(table.columns) < 2 or len(table.rows) < 2:
        return
    by_rc = {(c.row_idx, c.col_idx): c for c in table.cells}
    for r in range(1, len(table.rows)):
        hits: list[int] = []
        for c in range(1, len(table.columns)):
            upper = by_rc.get((r - 1, c))
            lower = by_rc.get((r, c))
            u = upper.raw_text.strip() if upper else ""
            v = lower.raw_text.strip() if lower else ""
            if u.endswith(",") and _YEAR_FRAGMENT.match(v):
                hits.append(c)
        if len(hits) < 2:
            continue
        for c in hits:
            for cell in (by_rc.get((r - 1, c)), by_rc.get((r, c))):
                if cell is not None:
                    cell.signals["header_split"] = 1.0


def score_table(table: Table, page: Page, cfg: Config) -> None:
    """Attach per-cell confidence, issues and flags to one table, in place."""
    w = {
        "type_nonconformity": cfg.f("confidence.penalty_type_nonconformity"),
        "source_error": cfg.f("confidence.penalty_source_error"),
        "parse_ambiguous": cfg.f("confidence.penalty_parse_ambiguous"),
        "unit_ambiguous": cfg.f("confidence.penalty_unit_ambiguous"),
        "crossfoot_fail": cfg.f("confidence.penalty_crossfoot_fail"),
        "table_flag": cfg.f("confidence.penalty_table_flag"),
        "low_layout": cfg.f("confidence.penalty_low_confidence_layout"),
        "crossfoot_pass": cfg.f("confidence.bonus_crossfoot_pass"),
        "header_split": cfg.f("confidence.penalty_header_split"),
    }
    base = cfg.f("confidence.base")
    floor = cfg.f("confidence.floor")

    issues = _cross_foot(table, cfg)
    _mark_split_headers(table)

    # A TABLE-level flag must not push every cell in the table below the review
    # threshold. One ambiguous wrapped label does not make 200 clean figures
    # suspect, and flagging them all would blow the brief's 8% flag budget on
    # noise -- burying the cells that genuinely need a human. Table flags lower
    # the TABLE's confidence; row flags gate that row; cell signals gate the
    # cell. Only the page-level layout penalty is genuinely per-cell, because a
    # page that could not calibrate its own geometry affects every cell on it.
    table_penalty = 0.0
    if "LOW_CONFIDENCE_LAYOUT" in page.notes:
        table_penalty += w["low_layout"]

    types = {c.index: c.inferred_type for c in table.columns}

    for cell in table.cells:
        codes: list[str] = []
        score = base - table_penalty

        col_type = types.get(cell.col_idx, ValueType.TEXT)
        if (
            not cell.is_header
            and col_type in NUMERIC_TYPES
            and cell.value_type not in NUMERIC_TYPES
            and cell.value_type not in BLANK_TYPES
        ):
            score -= w["type_nonconformity"]
            codes.append("TYPE_NONCONFORMITY")

        for note in cell.notes:
            if note == "SOURCE_ERROR_VALUE":
                score -= w["source_error"]
                codes.append(note)
            elif note == "PARSE_AMBIGUOUS":
                score -= w["parse_ambiguous"]
                codes.append(note)
            elif note == "UNIT_GLYPH_AMBIGUOUS":
                score -= w["unit_ambiguous"]
                codes.append(note)
            elif note == "DATE_ORDER_AMBIGUOUS":
                score -= w["parse_ambiguous"]
                codes.append(note)

        cf = cell.signals.get("crossfoot")
        if cf == 1.0:
            score += w["crossfoot_pass"]
        elif cf == -1.0:
            score -= w["crossfoot_fail"]
            codes.append("CROSSFOOT_FAIL")

        if cell.signals.get("header_split"):
            score -= w["header_split"]
            codes.append("HEADER_SPLIT")

        if table.rows and cell.row_idx < len(table.rows):
            if table.rows[cell.row_idx].flags:
                codes.extend(table.rows[cell.row_idx].flags)
                score -= w["table_flag"]

        cell.confidence = max(floor, min(1.0, score))
        cell.codes = tuple(sorted(set(codes)))

    # EXTEND, never replace. S6 runs before S9 and records STITCH_AMBIGUOUS
    # against tables it refused to merge; assigning here destroyed every one of
    # them, so a refusal the pipeline had correctly noticed never reached the
    # database or the review queue. A silently discarded refusal is
    # indistinguishable from never having looked.
    table.issues.extend(issues)
    for cell in table.cells:
        if cell.value_type is ValueType.ERROR:
            table.issues.append(
                Issue(
                    severity="error",
                    code="SOURCE_ERROR_VALUE",
                    message=f"cell holds the literal {cell.raw_text!r} from the source",
                    row_idx=cell.row_idx,
                    col_idx=cell.col_idx,
                )
            )


def review_queue(pages: Sequence[Page], cfg: Config) -> list[ReviewItem]:
    """Rank cells for human verification, most suspicious first."""
    threshold = cfg.f("confidence.review_threshold")
    items: list[ReviewItem] = []
    for p in pages:
        for t in p.tables:
            if not t.accepted:
                continue
            for c in t.cells:
                if c.confidence >= threshold and not c.codes:
                    continue
                items.append(
                    ReviewItem(
                        page_no=p.page_no,
                        table_index=t.index,
                        row_idx=c.row_idx,
                        col_idx=c.col_idx,
                        raw_text=c.raw_text,
                        confidence=c.confidence,
                        codes=c.codes,
                    )
                )
    # Total order: priority, then position, so two runs rank identically.
    items.sort(
        key=lambda i: (-i.priority, i.page_no, i.table_index, i.row_idx, i.col_idx)
    )
    return items
