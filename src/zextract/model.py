"""Pipeline state.

Every stage is a pure function ``(state, config) -> state'`` and every object
here round-trips through JSON, so any stage boundary can be dumped, diffed and
re-entered in isolation (ARCHITECTURE.md 2). That property is the debugging
instrument: "here is a page we get wrong" becomes a file you can bisect.

Ordering discipline: containers are lists in a defined order, never sets or
dicts keyed on floats. Sort keys are total -- they end in an index or a hash so
that two equal-looking elements can never swap between runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Sequence

from .geometry import Rect

# Rounding applied to every float that reaches disk. Six decimal places is far
# below PDF coordinate precision and far above float noise from parsing.
SERIALISE_ND = 6


class PageType(str, Enum):
    DIGITAL = "digital"
    RASTER = "raster"
    HYBRID = "hybrid"


class TokenSource(str, Enum):
    TEXT_LAYER = "text-layer"
    OCR = "ocr"


class ValueType(str, Enum):
    """What a cell holds after S7.

    EMPTY, DASH, NIL and NA are four DIFFERENT types, not four spellings of
    null. Shell.pdf's balance sheet uses "-" for nil-this-period in columns that
    also contain 0.00 for a measured zero; collapsing them destroys information
    no downstream sum can recover. ERROR is a fifth: a live spreadsheet error
    printed into the document, which is a fact about the source, not a failure
    to parse.
    """

    INTEGER = "integer"
    DECIMAL = "decimal"
    CURRENCY = "currency"
    PERCENT = "percent"
    DATE = "date"
    TEXT = "text"
    EMPTY = "empty"
    DASH = "dash"
    NIL = "nil"
    NA = "na"
    ERROR = "error"


@dataclass(frozen=True)
class Issue:
    severity: str  # "info" | "warning" | "error"
    code: str
    message: str
    row_idx: int | None = None
    col_idx: int | None = None


class RuleOrientation(str, Enum):
    HORIZONTAL = "h"
    VERTICAL = "v"


@dataclass(frozen=True)
class Token:
    """One word from the text layer, with its typography.

    ``order`` is the token's index in the producer's content stream. It is kept
    for one purpose only -- as the final tie-break in otherwise-equal geometric
    sorts, so ordering is total and therefore deterministic. It is never used to
    infer structure: Shell.pdf emits its table body before its header row
    (ARCHITECTURE.md F4).
    """

    text: str
    bbox: Rect
    font: str
    size: float
    bold: bool
    italic: bool
    color: int
    order: int
    source: TokenSource = TokenSource.TEXT_LAYER

    @property
    def sort_key(self) -> tuple[float, float, int]:
        return (self.bbox.y0, self.bbox.x0, self.order)


@dataclass(frozen=True)
class Rule:
    """A ruling line recovered from vector drawing primitives."""

    orientation: RuleOrientation
    bbox: Rect
    thickness: float

    @property
    def length(self) -> float:
        return self.bbox.width if self.orientation is RuleOrientation.HORIZONTAL else self.bbox.height

    @property
    def position(self) -> float:
        """The coordinate the rule sits at: y for horizontal, x for vertical."""
        return self.bbox.cy if self.orientation is RuleOrientation.HORIZONTAL else self.bbox.cx


@dataclass(frozen=True)
class PageStats:
    """Self-calibrated page geometry (ARCHITECTURE.md 9.1, DECISIONS.md D20).

    Every threshold in every downstream stage is a multiple of one of these.

    The two statistics are tracked separately because they fail separately and
    cost different things when they do. A page can have a perfectly sound line
    pitch and no measurable word gap at all -- a sparse table whose every cell
    is a single word has no inter-word spacing to measure, and the "gaps" the
    histogram sees are column gutters. Pitch drives lines, blocks and rules
    (S2); word gap drives column gutter thresholds (S3). Failing the whole page
    because one of them is unavailable would mark most table pages in a
    financial document low-confidence for a reason that does not affect their
    line work.

    An unstable statistic is inherited from the document median, never invented,
    and everything derived from a page with an unstable PITCH carries a
    confidence penalty (ARCHITECTURE.md 9.5).
    """

    line_pitch: float
    within_line_word_gap: float
    text_angle_deg: float
    modal_font_size: float
    token_count: int
    pitch_stable: bool
    word_gap_stable: bool
    source: str  # "page" | "document" | "fallback"
    notes: tuple[str, ...] = ()

    @property
    def stable(self) -> bool:
        """Both statistics measured from this page's own geometry."""
        return self.pitch_stable and self.word_gap_stable


@dataclass(frozen=True)
class TextLine:
    """Tokens sharing a horizontal writing line.

    Built by vertical-overlap clustering, not by the producer's own line
    grouping. A line may legitimately span several table columns -- that is what
    makes it usable as row evidence downstream.
    """

    index: int
    bbox: Rect
    token_indices: tuple[int, ...]
    text: str

    @property
    def sort_key(self) -> tuple[float, float, int]:
        return (self.bbox.y0, self.bbox.x0, self.index)


@dataclass(frozen=True)
class Block:
    """A run of text lines with no significant vertical break and no rule
    crossing it.

    A block is NOT a table row, and the distinction matters. Apple p32 sets its
    income statement at ordinary leading with rules only under subtotals, so the
    three lines "Net sales:", "Products $294,866 ...", "Services 96,169 ..." all
    fall in one block -- three table rows, one block. Nothing at this stage can
    separate them, because a second line inside a row (a wrapped cell) and a
    genuine new row look identical until you know where the columns are.

    So blocks are the SCOPE for column analysis, not the rows themselves: S3
    computes a column partition per block and grows tables from runs of blocks
    whose partitions agree (ARCHITECTURE.md 9.2); S4 then recovers rows within a
    table using that partition.
    """

    index: int
    bbox: Rect
    line_indices: tuple[int, ...]
    # Why this block ended where it did -- "gap", "rule", or "page".
    split_reason: str


class Alignment(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    CENTRE = "centre"
    MIXED = "mixed"


@dataclass
class Column:
    """One column of a table candidate, with the alignment of its content.

    Alignment is retained rather than discarded because it is the strongest
    available type hint before S7 runs: right-aligned columns are numeric in
    financial documents almost without exception (ARCHITECTURE.md F2).
    """

    index: int
    x0: float
    x1: float
    alignment: Alignment
    inferred_type: ValueType = ValueType.TEXT
    unit: str | None = None
    type_agreement: float = 0.0
    is_symbol: bool = False
    # Indices of the raw partition slots this column was built from. A currency
    # symbol column absorbed into its neighbour leaves its slot recorded here,
    # so the grid stays explainable (ARCHITECTURE.md S4.4).
    absorbed: tuple[int, ...] = ()
    header_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class ColumnPartition:
    columns: tuple[Column, ...]
    source: str  # "vertical-rules" | "gutters"
    # Fraction of the block's lines whose tokens fall cleanly inside these
    # columns. Low support means the partition is a poor description of the
    # block, which is the main evidence for refusing a table candidate.
    support: float


@dataclass
class Row:
    """One reconstructed table row.

    Mutable, like Cell and Column: S4.6 fills in the row-label hierarchy after
    the cells exist, because the label text it walks lives in the cells.
    """

    index: int
    bbox: Rect
    line_indices: tuple[int, ...]
    flags: tuple[str, ...] = ()
    # S4.6 -- the row-label hierarchy this row sits under, outermost first.
    # ["Revenue", "Domestic", "Product A"] for a doubly-indented line item.
    # Indentation is frequently the ONLY signal: no bullet, no numbering, no
    # bold, just leading whitespace (ARCHITECTURE.md S4.6).
    row_label_path: tuple[str, ...] = ()
    indent_level: int = 0
    # S4.6 -- what this row IS, not just where it sits. "section" opens a scope
    # and carries no figures; "total" closes one; "data" is a line item.
    # Indentation alone cannot tell them apart -- in Apple's statements
    # "Total net sales" is indented FURTHER RIGHT than the items it totals, so
    # a purely geometric hierarchy makes a total the child of its last sibling.
    row_kind: str = "data"
    # S4.7 -- index into ``Table.sections`` of the scope this row belongs to.
    section_index: int | None = None


@dataclass
class Cell:
    """One grid cell.

    ``raw_text`` is written once at S5 and never modified. Every normalisation
    field sits alongside it, so a reviewer is never looking at a value we have
    silently rewritten.
    """

    row_idx: int
    col_idx: int
    raw_text: str
    bbox: Rect | None
    token_indices: tuple[int, ...]
    source: TokenSource = TokenSource.TEXT_LAYER
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    page_no: int = 0
    footnote_ids: tuple[int, ...] = ()
    # --- S7 ---
    value_type: ValueType = ValueType.TEXT
    normalized_value: float | None = None
    normalized_text: str | None = None
    unit: str | None = None
    scale: str | None = None
    parse_rule: str = ""
    notes: tuple[str, ...] = ()
    # --- S9 ---
    confidence: float = 0.0
    codes: tuple[str, ...] = ()
    signals: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AssetLink:
    """A scored table→figure association (ARCHITECTURE.md S8.2)."""

    asset_index: int
    relation: str  # "visualises" | "co-located"
    confidence: float


@dataclass
class Asset:
    """A non-furniture image, cropped at persist time.

    Repeating logos are excluded here and recorded on the document as furniture
    templates (ARCHITECTURE.md F6). A unique figure on one page is an asset.
    """

    index: int
    page_no: int
    kind: str  # "figure"
    bbox: Rect
    xref: int
    caption: str | None = None
    file_path: str | None = None


@dataclass
class Section:
    """One scope of a table: a heading, its members, and the totals closing it.

    Derived in S4.7 from row kinds -- never from geometry. `row_indices` are the
    members; `subtotal_rows` are the rows that state a total OF those members
    and are deliberately NOT members themselves, so nothing double-counts a
    subtotal into the sum it summarises.
    """

    label: str
    level: int
    header_row: int | None
    parent_index: int | None
    row_indices: tuple[int, ...] = ()
    subtotal_rows: tuple[int, ...] = ()
    child_indices: tuple[int, ...] = ()
    # The members each subtotal actually covers, in reading order:
    # ((member_rows, subtotal_row), ...). A section may close more than once --
    # "Total deferred tax assets" covers the seven asset rows, and
    # "Total deferred tax assets, net" covers those AND the valuation allowance
    # beneath them -- so a single flat member list cannot express what any one
    # total is a total OF. Consumers reconcile against these groups rather than
    # rebuilding them.
    groups: tuple[tuple[tuple[int, ...], int], ...] = ()


@dataclass
class Table:
    """A table candidate: a run of blocks sharing a column partition.

    Candidates that the trap classifier refuses are kept, with ``rejected_as``
    set, rather than dropped. A silent rejection is indistinguishable from a
    detection miss when something goes wrong (DECISIONS.md D5).
    """

    index: int
    page_no: int
    bbox: Rect
    start_page: int = 0
    end_page: int = 0
    is_continuation: bool = False
    columns: list[Column] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)
    block_indices: tuple[int, ...] = ()
    partition_source: str = ""
    support: float = 0.0
    rejected_as: str | None = None
    flags: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    title: str | None = None
    caption: str | None = None
    section_path: str | None = None
    unit_scale_note: str | None = None
    unit_scale_source: str | None = None  # "local" | "inherited" | "header"
    asset_links: list["AssetLink"] = field(default_factory=list)
    # S4.7 -- the scope tree derived from row kinds. One shared answer to
    # "where does this scope begin and what closes it", consumed by S9's
    # cross-footing and by the exporters.
    sections: list["Section"] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.rejected_as is None

    def __post_init__(self) -> None:
        if not self.start_page:
            self.start_page = self.page_no
        if not self.end_page:
            self.end_page = self.page_no

    def cell_at(self, row_idx: int, col_idx: int) -> Cell | None:
        for c in self.cells:
            if c.row_idx == row_idx and c.col_idx == col_idx:
                return c
        return None


@dataclass
class Page:
    page_no: int  # 1-based
    width: float
    height: float
    rotation: int
    page_type: PageType
    image_coverage: float
    char_count: int
    tokens: list[Token] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    stats: PageStats | None = None
    lines: list[TextLine] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    # Lines identified as running header/footer furniture. Retained on the page
    # (a footer often carries the document's unit declaration) but excluded from
    # table candidates.
    furniture_line_indices: tuple[int, ...] = ()
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def rect(self) -> Rect:
        return Rect(0.0, 0.0, self.width, self.height)

    def horizontal_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.orientation is RuleOrientation.HORIZONTAL]

    def vertical_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.orientation is RuleOrientation.VERTICAL]


@dataclass
class Footnote:
    """A footnote block bound from a marker (ARCHITECTURE.md S8)."""

    index: int
    page_no: int
    marker: str
    text: str
    bbox: Rect | None = None


@dataclass
class Document:
    filename: str
    sha256: str
    page_count: int
    pages: list[Page] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------

def _encode(obj: Any) -> Any:
    if isinstance(obj, Rect):
        r = obj.rounded(SERIALISE_ND)
        return {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, float):
        return round(obj, SERIALISE_ND)
    if isinstance(obj, tuple):
        return [_encode(v) for v in obj]
    if isinstance(obj, list):
        return [_encode(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    return obj


def to_jsonable(obj: Any) -> Any:
    """Convert a state object to plain JSON types, deterministically."""
    if hasattr(obj, "__dataclass_fields__"):
        return _encode(asdict(obj))
    return _encode(obj)


def dumps(obj: Any) -> str:
    return json.dumps(
        to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def stable_sorted(items: Sequence[Any], key) -> list[Any]:
    """``sorted`` with an explicit name, used wherever ordering reaches output.

    Python's sort is stable, but relying on input order for ties is exactly the
    kind of implicit dependency that makes a second run differ. Callers pass
    keys that are total.
    """
    return sorted(items, key=key)
