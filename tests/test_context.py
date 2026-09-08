"""S8 context capture and the persist-side output contract.

A title, a unit in the header, and a Source caption are document-general
patterns, not sample strings. The persist tests check the two artefacts the
CLI used to omit: logs/run.jsonl and assets/index.json.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from zextract.persist import persist_run
from zextract.pipeline import run_layout

from conftest import (
    RULED_COL_X,
    RULED_FONT,
    RULED_PITCH,
    RULED_ROWS,
    RULED_TOP,
    make_pdf,
)


def _page(doc: pymupdf.Document, width: float = 612.0, height: float = 792.0):
    return doc.new_page(width=width, height=height)


@pytest.fixture(scope="session")
def context_pdf(tmp_path_factory) -> Path:
    """Ruled table with an Annexure title, a unit in the header, a Source line.

    Title and caption sit just outside S3's join window (3 pitches) so they
    are not swallowed as table rows, and inside S8's bind window (4 pitches).
    """
    out = tmp_path_factory.mktemp("fixtures") / "context.pdf"

    def build(doc: pymupdf.Document) -> None:
        page = _page(doc)
        page.insert_text(
            (72.0, 52.0), "Annexure I", fontsize=11.0, fontname="hebo"
        )
        rows = [list(r) for r in RULED_ROWS]
        rows[0][1] = "FY25 (in millions)"
        for r, row in enumerate(rows):
            y = RULED_TOP + r * RULED_PITCH
            for c, cell in enumerate(row):
                page.insert_text(
                    (RULED_COL_X[c], y), cell, fontsize=RULED_FONT, fontname="helv"
                )
        rule_y = RULED_TOP + 0.5 * RULED_PITCH
        page.draw_line(
            pymupdf.Point(72.0, rule_y), pymupdf.Point(500.0, rule_y), width=0.6
        )
        # 3.2 pitches below the last baseline: outside the table, inside caption bind.
        last_y = RULED_TOP + (len(rows) - 1) * RULED_PITCH
        page.insert_text(
            (180.0, last_y + 3.2 * RULED_PITCH),
            "Source: company filings.",
            fontsize=8.0,
            fontname="heit",
        )
        for i in range(6):
            page.insert_text(
                (72.0, 400.0 + i * RULED_PITCH),
                "The company reported higher revenue across every reporting segment.",
                fontsize=RULED_FONT,
                fontname="helv",
            )

    return make_pdf(out, build)


def test_binds_title_unit_and_source_caption(context_pdf, cfg):
    doc = run_layout(context_pdf, cfg)
    accepted = [t for p in doc.pages for t in p.tables if t.accepted]
    assert accepted, "fixture produced no accepted table"
    table = accepted[0]
    assert table.title and "Annexure" in table.title
    assert table.unit_scale_note and "million" in table.unit_scale_note.lower()
    assert table.caption and table.caption.lower().startswith("source")


def test_persist_writes_run_log_and_empty_assets_index(ruled_pdf, cfg, tmp_path):
    stamp = "2020-01-01T00:00:00+00:00"
    out = tmp_path / "run"
    doc = run_layout(ruled_pdf, cfg)
    persist_run(doc, cfg, out, ruled_pdf, stamp, stamp)

    log = out / "logs" / "run.jsonl"
    assert log.exists()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("{")
    assert '"event":"intake"' in lines[0]
    assert not any('"started_at"' in line or '"finished_at"' in line for line in lines)

    index = out / "assets" / "index.json"
    assert index.read_text(encoding="utf-8") == "[]\n"
    assert doc.assets == []


def test_database_writes_an_asset_row(ruled_pdf, cfg, tmp_path):
    """Catch a column-count mismatch on INSERT INTO assets (schema has 10)."""
    import sqlite3

    from zextract.geometry import Rect
    from zextract.model import Asset
    from zextract.persist.db import write_database

    doc = run_layout(ruled_pdf, cfg)
    doc.assets = [
        Asset(
            index=0,
            page_no=1,
            kind="figure",
            bbox=Rect(10.0, 10.0, 100.0, 80.0),
            xref=7,
            file_path="assets/p001__xref7.png",
        )
    ]
    db = tmp_path / "extraction.db"
    write_database(doc, cfg, db, "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00")
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT kind, file_path, caption FROM assets"
        ).fetchone()
        n_links = con.execute("SELECT COUNT(*) FROM table_assets").fetchone()[0]
    finally:
        con.close()
    assert row == ("figure", "assets/p001__xref7.png", None)
    assert n_links == 0


def test_two_runs_produce_identical_run_jsonl(ruled_pdf, cfg, tmp_path):
    stamp = "2020-01-01T00:00:00+00:00"
    texts = []
    for name in ("a", "b"):
        doc = run_layout(ruled_pdf, cfg)
        out = tmp_path / name
        persist_run(doc, cfg, out, ruled_pdf, stamp, stamp)
        texts.append((out / "logs" / "run.jsonl").read_bytes())
    assert texts[0] == texts[1]
