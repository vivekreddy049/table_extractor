"""S10 -- SQLite writer.

Identifiers are DERIVED, not generated: a cell's id is a hash of the document
sha256 plus its position, so two runs produce byte-identical rows. A UUID here
would break the two-run diff the brief tests for, which is why none appears.

Timestamps are confined to the single ``extraction_runs`` row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from ..config import Config
from ..model import Document, TokenSource

_SCHEMA = Path(__file__).with_name("schema.sql")


def _id(*parts: object) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def write_database(
    doc: Document,
    cfg: Config,
    out_path: Path | str,
    started_at: str,
    finished_at: str,
    git_sha: str | None = None,
) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()  # a run rebuilds its database; it never appends

    con = sqlite3.connect(str(path))
    try:
        con.executescript(_SCHEMA.read_text(encoding="utf-8"))
        doc_id = _id(doc.sha256)
        run_id = _id(doc.sha256, cfg.sha256(), "run")

        con.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?)",
            (doc_id, doc.filename, doc.sha256, doc.page_count, started_at),
        )
        con.execute(
            "INSERT INTO extraction_runs VALUES (?,?,?,?,?,?)",
            (run_id, doc_id, started_at, finished_at, git_sha, cfg.to_json()),
        )

        page_ids: dict[int, str] = {}
        for p in doc.pages:
            pid = _id(doc_id, "page", p.page_no)
            page_ids[p.page_no] = pid
            st = p.stats
            con.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    doc_id,
                    p.page_no,
                    p.width,
                    p.height,
                    p.page_type.value,
                    p.rotation,
                    0.0,
                    st.line_pitch if st else None,
                    st.within_line_word_gap if st else None,
                    st.source if st else None,
                ),
            )

        for fn in doc.footnotes:
            fid = _id(doc_id, "fn", fn.index)
            con.execute(
                "INSERT INTO footnotes VALUES (?,?,?,?,?)",
                (
                    fid,
                    doc_id,
                    page_ids.get(fn.page_no),
                    fn.marker,
                    fn.text,
                ),
            )

        logical = 0
        for p in doc.pages:
            for t in p.tables:
                tid = _id(doc_id, "table", p.page_no, t.index)
                # Every candidate is stored, accepted or refused. A rejection
                # that vanishes is indistinguishable from a detection miss.
                conf = (
                    sum(c.confidence for c in t.cells) / len(t.cells)
                    if t.cells
                    else None
                )
                con.execute(
                    "INSERT INTO tables VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tid,
                        doc_id,
                        run_id,
                        logical,
                        t.start_page or p.page_no,
                        t.end_page or p.page_no,
                        len(t.rows),
                        len(t.columns),
                        t.title,
                        t.caption,
                        t.section_path,
                        t.unit_scale_note,
                        1 if t.is_continuation else 0,
                        json.dumps(
                            {"source": t.partition_source, "support": round(t.support, 6)},
                            sort_keys=True,
                        ),
                        conf,
                        t.rejected_as,
                        json.dumps(sorted(t.flags)),
                    ),
                )
                logical += 1

                con.execute(
                    "INSERT INTO table_regions VALUES (?,?,?,?,?,?,?)",
                    (
                        _id(tid, "region"),
                        tid,
                        page_ids[p.page_no],
                        t.bbox.x0,
                        t.bbox.y0,
                        t.bbox.x1,
                        t.bbox.y1,
                    ),
                )

                for col in t.columns:
                    cid = _id(tid, "col", col.index)
                    header = list(col.header_path) if col.header_path else _header_tokens(t, col.index)
                    con.execute(
                        "INSERT INTO columns VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            cid,
                            tid,
                            col.index,
                            json.dumps(header, ensure_ascii=False),
                            col.inferred_type.value,
                            col.unit,
                            1,
                            col.alignment.value,
                            round(col.type_agreement, 6),
                        ),
                    )
                    for level, tok in enumerate(header):
                        con.execute(
                            "INSERT INTO column_header_tokens VALUES (?,?,?)",
                            (cid, level, tok),
                        )

                row_paths = {r.index: list(r.row_label_path) for r in t.rows}
                for r in t.rows:
                    for level, tok in enumerate(r.row_label_path):
                        con.execute(
                            "INSERT INTO row_label_tokens VALUES (?,?,?,?)",
                            (tid, r.index, level, tok),
                        )

                cell_ids: dict[tuple[int, int], str] = {}
                for c in t.cells:
                    cid = _id(tid, "cell", c.row_idx, c.col_idx)
                    cell_ids[(c.row_idx, c.col_idx)] = cid
                    b = c.bbox
                    pid = page_ids.get(c.page_no) or page_ids[p.page_no]
                    con.execute(
                        "INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            cid,
                            tid,
                            c.row_idx,
                            c.col_idx,
                            c.row_span,
                            c.col_span,
                            1 if c.is_header else 0,
                            c.raw_text,
                            c.normalized_value,
                            c.value_type.value,
                            json.dumps(
                                row_paths.get(c.row_idx, []), ensure_ascii=False
                            ),
                            pid,
                            b.x0 if b else None,
                            b.y0 if b else None,
                            b.x1 if b else None,
                            b.y1 if b else None,
                            c.source.value
                            if isinstance(c.source, TokenSource)
                            else str(c.source),
                            round(c.confidence, 6),
                            c.normalized_text,
                            c.unit,
                            c.scale,
                            c.parse_rule,
                            json.dumps(list(c.codes)),
                        ),
                    )
                    for fn_i in c.footnote_ids:
                        con.execute(
                            "INSERT INTO cell_footnotes VALUES (?,?)",
                            (cid, _id(doc_id, "fn", fn_i)),
                        )

                for n, issue in enumerate(t.issues):
                    cell_id = (
                        cell_ids.get((issue.row_idx, issue.col_idx))
                        if issue.row_idx is not None and issue.col_idx is not None
                        else None
                    )
                    con.execute(
                        "INSERT INTO issues VALUES (?,?,?,?,?,?)",
                        (
                            _id(tid, "issue", n, issue.code),
                            tid,
                            cell_id,
                            issue.severity,
                            issue.code,
                            issue.message,
                        ),
                    )

        for asset in doc.assets:
            aid = _id(doc_id, "asset", asset.index)
            con.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    doc_id,
                    page_ids.get(asset.page_no),
                    asset.kind,
                    asset.file_path,
                    asset.caption,
                    asset.bbox.x0,
                    asset.bbox.y0,
                    asset.bbox.x1,
                    asset.bbox.y1,
                ),
            )
            for page in doc.pages:
                for t in page.tables:
                    if not t.accepted:
                        continue
                    tid = _id(doc_id, "table", page.page_no, t.index)
                    for link in t.asset_links:
                        if link.asset_index != asset.index:
                            continue
                        con.execute(
                            "INSERT INTO table_assets VALUES (?,?,?,?)",
                            (tid, aid, link.relation, link.confidence),
                        )

        con.commit()
    finally:
        con.close()
    return path


def _header_tokens(table, col_idx: int) -> list[str]:
    """Header path for a column.

    Multi-level headers are not reconstructed yet (S4.5 unbuilt), so this is
    the first row's text for that column when the first row looks like a header,
    and empty otherwise. It is a one-element path, not a fabricated hierarchy.
    """
    if not table.rows:
        return []
    first = table.cell_at(0, col_idx)
    if first is None or not first.raw_text.strip():
        return []
    if first.value_type.value in ("integer", "decimal", "currency", "percent"):
        return []  # a numeric first row is data, not a header
    return [first.raw_text.strip()]
