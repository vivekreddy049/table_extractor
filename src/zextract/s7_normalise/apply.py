"""S7 -- apply value parsing and column type inference to a table."""

from __future__ import annotations

from ..config import Config
from ..model import Table
from .values import infer_column_type, parse_value


def normalise_table(table: Table, cfg: Config) -> None:
    """Populate every cell's normalised fields, then vote each column's type."""
    min_agreement = cfg.f("normalise.min_type_agreement")

    parsed_by_col: dict[int, list] = {c.index: [] for c in table.columns}

    for cell in table.cells:
        p = parse_value(cell.raw_text)
        cell.value_type = p.value_type
        cell.normalized_value = p.normalized_value
        cell.normalized_text = p.normalized_text
        cell.unit = p.unit
        cell.scale = p.scale
        cell.parse_rule = p.rule
        cell.notes = p.notes
        if cell.col_idx in parsed_by_col:
            parsed_by_col[cell.col_idx].append(p)

    for col in table.columns:
        vtype, unit, agreement = infer_column_type(
            parsed_by_col.get(col.index, []), min_agreement
        )
        col.inferred_type = vtype
        col.unit = unit
        col.type_agreement = agreement
