"""S4.7 -- the table as a tree of scopes, derived from row kinds.

Every stage downstream of S4 needs the same three facts: where a scope begins,
which rows belong to it, and which row closes it. Before this module each stage
worked them out again from geometry, and each one had its own idea of what a
"total" is -- S4 inferred hierarchy from indentation, S9 inferred arithmetic
scopes from a running list it reset on total-looking labels, and the exporters
tested ``row.kind`` inline. Four private definitions of the same concept is how
two unrelated bugs (a total nested under its own sibling, and an arithmetic
scope leaking across a section heading) turn out to have one cause.

So the semantics live in ONE place. ``Row.row_kind`` (S4.6) says what a row is;
this module says how those rows nest. Nothing here re-reads a bounding box.

The tree is deliberately shallow and total. Every row belongs to exactly one
section, including rows that precede any heading -- those go to an implicit
root section rather than being dropped, because a statement that opens straight
into line items ("Cash", "Receivables", "Total assets") is common and must
still reconcile.
"""

from __future__ import annotations

from ..model import Section, Table


def _section(label: str, level: int, header_row: int | None, parent: int | None) -> Section:
    return Section(
        label=label,
        level=level,
        header_row=header_row,
        parent_index=parent,
        row_indices=(),
        subtotal_rows=(),
        child_indices=(),
    )


def build_tree(table: Table) -> None:
    """Populate ``table.sections`` in place from the rows' kinds.

    A ``section`` row opens a scope at its indent level and closes every scope
    at or deeper than that level. A ``total`` row closes the innermost open
    scope -- it is recorded as that scope's subtotal, not as one of its members,
    because a subtotal is a statement ABOUT the members rather than one of them.
    Everything else is a member of the innermost open scope.

    A scope may hold more than one total: "Total deferred tax assets" and
    "Total deferred tax assets, net" both close the asset scope, the second
    restating the first after an adjustment. Both are kept, in order, so a
    consumer can reconcile against whichever it means.
    """
    table.sections = []
    if not table.rows:
        return

    root = _section(label="", level=-1, header_row=None, parent=None)
    sections: list[Section] = [root]
    open_stack: list[int] = [0]  # indices into `sections`, outermost first

    for row in table.rows:
        if row.row_kind == "section":
            level = row.indent_level
            # A heading closes every scope at or deeper than its own level.
            while len(open_stack) > 1 and sections[open_stack[-1]].level >= level:
                open_stack.pop()
            parent = open_stack[-1]
            sec = _section(
                label=row.row_label_path[-1] if row.row_label_path else "",
                level=level,
                header_row=row.index,
                parent=parent,
            )
            sections.append(sec)
            idx = len(sections) - 1
            sections[parent].child_indices += (idx,)
            open_stack.append(idx)
            continue

        if row.row_kind in ("header", "blank"):
            # Neither a member nor a scope: a column header, or an empty line.
            continue

        if row.row_kind == "total":
            # A total closes the scope at ITS OWN level, so only scopes nested
            # deeper than it are popped first. Shell's balance sheet is the
            # case: "Total Non-Current Assets" is set at level 0 while the
            # "(e) Financial Assets" sub-scope is still open at level 1, and it
            # totals the whole of non-current assets rather than that subgroup.
            while len(open_stack) > 1 and sections[open_stack[-1]].level > row.indent_level:
                open_stack.pop()
            sections[open_stack[-1]].subtotal_rows += (row.index,)
            continue

        # A line item that dedents out of a sub-scope closes it. "(f) Deferred
        # Tax Assets" is a SIBLING of "(e) Financial Assets", not a member of
        # it, and without this it was swallowed by the scope above purely
        # because no further heading happened to intervene -- taking the
        # section's total down with it.
        #
        # But "dedents" cannot mean "is not deeper than the heading", because
        # plenty of tables do not indent members under a heading at all; there
        # the first line item would close the scope it belongs to. The scope is
        # only closed when it demonstrably HAS a deeper level to leave: one of
        # its members already sits further right than this row. A scope whose
        # members are flush with its heading is never entered and so never
        # wrongly exited.
        while len(open_stack) > 1:
            top = sections[open_stack[-1]]
            if top.level < row.indent_level:
                break
            if not any(
                table.rows[r].indent_level > row.indent_level
                for r in top.row_indices
                if 0 <= r < len(table.rows)
            ):
                break
            open_stack.pop()
        sections[open_stack[-1]].row_indices += (row.index,)

    # Partition each scope's members by the subtotal that closes them. A
    # subtotal covers everything since the previous one, and -- crucially --
    # the previous subtotal itself when the statement restates a total after an
    # adjustment ("Total X" then "Less: allowance" then "Total X, net").
    def _contributions(idx: int) -> list[int]:
        """The rows a scope's own total is a total OF.

        Its direct members, plus each nested scope -- represented by that
        scope's own total where it has one, and by its members where it does
        not. A parent that counted only its direct members would miss every
        line item sitting under a sub-heading: Shell's "Total Current Assets"
        covers Inventories AND everything under "(b) Financial Assets", and
        reconciling it against Inventories alone is off by thousands.

        Using the child's TOTAL when it exists is what keeps the sum honest --
        adding the child's members as well would count them twice.
        """
        sec = sections[idx]
        out = list(sec.row_indices)
        for child in sec.child_indices:
            if sections[child].subtotal_rows:
                out.extend(sections[child].subtotal_rows)
            else:
                out.extend(_contributions(child))
        return out

    for i, sec in enumerate(sections):
        members = sorted(_contributions(i))
        groups: list[tuple[tuple[int, ...], int]] = []
        for sub in sorted(sec.subtotal_rows):
            covered = tuple(r for r in members if r < sub)
            if groups:
                # Everything the earlier total summarised is already folded
                # into that total, so carry the TOTAL forward rather than its
                # members: otherwise the adjustment is added to a sum that
                # already contains it.
                prev_rows, prev_sub = groups[-1]
                covered = (prev_sub,) + tuple(r for r in covered if r not in prev_rows)
            if covered:
                groups.append((covered, sub))
        sec.groups = tuple(groups)

    # The root only earns a place when it actually holds something; a statement
    # that opens with a heading should not carry an empty wrapper.
    if not root.row_indices and not root.subtotal_rows:
        keep = sections[1:]
        remap = {old: new for new, old in enumerate(range(1, len(sections)))}
        for sec in keep:
            sec.parent_index = (
                remap.get(sec.parent_index) if sec.parent_index not in (None, 0) else None
            )
            sec.child_indices = tuple(remap[c] for c in sec.child_indices if c in remap)
        table.sections = keep
    else:
        table.sections = sections

    for row in table.rows:
        row.section_index = None
    for i, sec in enumerate(table.sections):
        for r in sec.row_indices + sec.subtotal_rows:
            if 0 <= r < len(table.rows):
                table.rows[r].section_index = i
