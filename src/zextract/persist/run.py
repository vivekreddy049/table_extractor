"""Write a run's artefacts: DB, Excel, viewer, queue, log, assets.

CLI and the local web UI both call ``persist_run`` so a new output file cannot
land in one path and be forgotten in the other.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pymupdf

from ..audit.viewer import render_viewer, tables_json
from ..config import Config
from ..model import Document, dumps
from ..s9_confidence import review_queue
from .db import write_database
from .excel import write_workbook


def persist_run(
    doc: Document,
    cfg: Config,
    out: Path,
    source_path: Path,
    started_at: str,
    finished_at: str,
) -> dict:
    """Write the output contract into ``out``. Returns the metrics mapping."""
    out = Path(out)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    (out / "state").mkdir(parents=True, exist_ok=True)
    (out / "assets").mkdir(parents=True, exist_ok=True)

    write_assets(doc, cfg, source_path, out / "assets")

    manifest = {
        "filename": doc.filename,
        "sha256": doc.sha256,
        "page_count": doc.page_count,
        "source_path": str(Path(source_path).resolve()),
        "config_sha256": cfg.sha256(),
        "notes": doc.notes,
        "stages_complete": [
            "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11"
        ],
        "stages_missing": [],
    }
    _write(out / "manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    _write(out / "state" / "layout.json", dumps(doc) + "\n")
    _write(out / "state" / "tables.json", tables_json(doc) + "\n")
    render_viewer(doc, out / "viewer.html")

    write_database(doc, cfg, out / "extraction.db", started_at, finished_at)

    logical = 0
    for page in doc.pages:
        for table in page.tables:
            if table.accepted:
                write_workbook(doc, page, table, logical, cfg, out / "tables")
                logical += 1

    queue = review_queue(doc.pages, cfg)
    _write_csv(
        out / "review_queue.csv",
        ("priority", "page_no", "table_index", "row_idx", "col_idx",
         "raw_text", "confidence", "reason_codes"),
        [
            (f"{i.priority:.4f}", i.page_no, i.table_index, i.row_idx, i.col_idx,
             i.raw_text, f"{i.confidence:.4f}", "|".join(i.codes))
            for i in queue
        ],
    )

    write_run_log(doc, out / "logs" / "run.jsonl")

    all_cells = [c for p_ in doc.pages for t in p_.tables if t.accepted for c in t.cells]
    metrics = {
        "document": doc.filename,
        "pages": doc.page_count,
        "tables_accepted": logical,
        "tables_rejected": sum(
            1 for p_ in doc.pages for t in p_.tables if not t.accepted
        ),
        "cells": len(all_cells),
        "cells_flagged": len(queue),
        "flagged_share": round(len(queue) / len(all_cells), 4) if all_cells else 0.0,
        "assets": len(doc.assets),
        "titles_extracted": sum(
            1 for p in doc.pages for t in p.tables if t.accepted and t.title
        ),
        "unit_notes_extracted": sum(
            1 for p in doc.pages for t in p.tables if t.accepted and t.unit_scale_note
        ),
        "confidence_is_calibrated": cfg.b("confidence.apply_calibration"),
        "measured_against_ground_truth": False,
        "stages_complete": [
            "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11"
        ],
        "stages_missing": [],
        "note": (
            "Counts, not scores. Scored numbers live in metrics/results.json "
            "after `python metrics/eval.py`. Confidence applies frozen "
            "calib/model_v1.json (n=418 labelled cells). S5 OCR fires only on "
            "raster/hybrid pages; the samples are digital. See LIMITATIONS.md."
        ),
    }
    _write(out / "metrics.json", json.dumps(metrics, sort_keys=True, indent=2) + "\n")
    return metrics


def write_run_log(doc: Document, path: Path) -> None:
    """Stage events, no wall-clock. Two runs of the same PDF are byte-identical."""
    events: list[dict] = [
        {
            "event": "intake",
            "filename": doc.filename,
            "sha256": doc.sha256,
            "pages": doc.page_count,
        }
    ]
    for page in doc.pages:
        events.append(
            {
                "event": "page",
                "page_no": page.page_no,
                "page_type": page.page_type.value,
                "tokens": len(page.tokens),
                "tables": len(page.tables),
            }
        )
        for t in page.tables:
            events.append(
                {
                    "event": "table",
                    "page_no": page.page_no,
                    "index": t.index,
                    "accepted": t.accepted,
                    "rejected_as": t.rejected_as,
                    "n_rows": len(t.rows),
                    "n_cols": len(t.columns),
                    "title": t.title,
                    "unit_scale_note": t.unit_scale_note,
                }
            )
    for a in doc.assets:
        events.append(
            {
                "event": "asset",
                "page_no": a.page_no,
                "index": a.index,
                "xref": a.xref,
                "kind": a.kind,
            }
        )
    lines = [json.dumps(e, sort_keys=True, ensure_ascii=False, separators=(",", ":")) for e in events]
    _write(path, "\n".join(lines) + ("\n" if lines else ""))


def write_assets(doc: Document, cfg: Config, source: Path, out_dir: Path) -> None:
    """Crop non-furniture figures to PNG. Empty directory if there are none."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dpi = cfg.f("assets.crop_dpi")
    scale = dpi / 72.0
    index: list[dict] = []
    if not doc.assets:
        _write(out_dir / "index.json", "[]\n")
        return

    handle = pymupdf.open(str(source))
    try:
        for asset in doc.assets:
            name = f"p{asset.page_no:03d}__xref{asset.xref}.png"
            dest = out_dir / name
            page = handle[asset.page_no - 1]
            clip = pymupdf.Rect(asset.bbox.x0, asset.bbox.y0, asset.bbox.x1, asset.bbox.y1)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
            pix.save(str(dest))
            asset.file_path = f"assets/{name}"
            index.append(
                {
                    "index": asset.index,
                    "page_no": asset.page_no,
                    "xref": asset.xref,
                    "file": name,
                    "caption": asset.caption,
                }
            )
    finally:
        handle.close()
    _write(out_dir / "index.json", json.dumps(index, sort_keys=True, indent=2) + "\n")


def _write_csv(path: Path, header, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
