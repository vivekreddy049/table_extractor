"""Command-line interface.

The brief's required surface is ``run`` / ``audit`` / ``debug``. ``run`` writes
the output contract; ``audit`` summarises a previous run; ``debug`` overlays
one page. Neither command changes shape as later stages land.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import Config
import datetime as _dt

from .audit.viewer import render_viewer
from .persist import persist_run
from .pipeline import LOW_CONFIDENCE_LAYOUT, run_layout


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    p.add_argument(
        "--offline",
        action="store_true",
        help="assert no network use; the pipeline never opens a socket, so this "
        "is a declaration the test suite enforces rather than a mode switch",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="zextract", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the pipeline over a PDF")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument(
        "--no-calibration",
        action="store_true",
        help="use the uncalibrated S9 ranking score; skip frozen model_v1.json",
    )
    _add_common(run)

    audit = sub.add_parser("audit", help="print the metrics summary for a run")
    audit.add_argument("--out", type=Path, required=True)
    _add_common(audit)

    view = sub.add_parser("view", help="rebuild the HTML table viewer for a run")
    view.add_argument("--out", type=Path, required=True)
    view.add_argument("--input", type=Path, default=None)
    _add_common(view)

    srv = sub.add_parser(
        "serve", help="local web UI: upload a PDF and see its stats"
    )
    srv.add_argument("--runs", type=Path, default=Path("runs"))
    srv.add_argument("--port", type=int, default=8000)
    srv.add_argument("--host", default="127.0.0.1")
    _add_common(srv)

    dbg = sub.add_parser("debug", help="write an overlay render for one page")
    dbg.add_argument("--out", type=Path, required=True)
    dbg.add_argument("--page", type=int, required=True)
    dbg.add_argument("--input", type=Path, default=None, help="PDF, if not recorded in the run")
    _add_common(dbg)

    return ap


def cmd_run(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    if getattr(args, "no_calibration", False):
        import copy

        data = copy.deepcopy(dict(cfg.data))
        data["confidence"]["apply_calibration"] = False
        cfg = Config(data)
    doc = run_layout(args.input, cfg)

    out: Path = args.out
    # The run timestamp is the ONE thing in the output that a wall clock would
    # decide, and it defeats the two-run byte diff the brief asks for: every
    # extracted value, and even run_id and doc_id (both content-derived), come
    # out identical while extraction.db differs on `started_at` alone.
    #
    # Reproducible-build practice applies. SOURCE_DATE_EPOCH pins the stamp when
    # it is set; otherwise the run records the epoch, so a second run over the
    # same document reproduces the database byte for byte. The real wall-clock
    # time is not lost -- it goes to the run log, which is where "when did this
    # run" belongs, rather than into the artefact whose reproducibility is the
    # claim being tested.
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    stamp = _dt.datetime.fromtimestamp(
        int(epoch) if epoch and epoch.isdigit() else 0, _dt.timezone.utc
    ).isoformat(timespec="seconds")
    metrics = persist_run(doc, cfg, out, args.input, stamp, stamp)

    logical = metrics["tables_accepted"]
    print(
        f"{doc.filename}: {doc.page_count} pages, "
        f"{logical} tables accepted / {metrics['tables_rejected']} rejected -> {out}"
    )
    print(f"  viewer: {out / 'viewer.html'}  db: {out / 'extraction.db'}")
    print(
        f"  {logical} workbooks in {out / 'tables'}, "
        f"{metrics['cells_flagged']} cells queued for review, "
        f"{metrics['assets']} assets"
    )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    out: Path = args.out
    state_path = out / "state" / "layout.json"
    if not state_path.exists():
        print(f"no run found at {out} (missing {state_path})", file=sys.stderr)
        return 2

    state = json.loads(state_path.read_text(encoding="utf-8"))
    pages = state["pages"]
    n = len(pages)

    by_type: dict[str, int] = {}
    low = []
    tokens = lines = blocks = h_rules = v_rules = 0
    for p in pages:
        by_type[p["page_type"]] = by_type.get(p["page_type"], 0) + 1
        if LOW_CONFIDENCE_LAYOUT in p["notes"]:
            low.append(p["page_no"])
        tokens += len(p["tokens"])
        lines += len(p["lines"])
        blocks += len(p["blocks"])
        h_rules += sum(1 for r in p["rules"] if r["orientation"] == "h")
        v_rules += sum(1 for r in p["rules"] if r["orientation"] == "v")

    print(f"document      {state['filename']}  ({n} pages)")
    print(f"page types    {dict(sorted(by_type.items()))}")
    print(f"tokens        {tokens}  ({tokens / n:.0f}/page)")
    print(f"lines         {lines}  ({lines / n:.0f}/page)")
    print(f"blocks        {blocks}  ({blocks / n:.0f}/page)")
    print(f"rules         h={h_rules} v={v_rules}")
    print(f"low-confidence layout  {len(low)}/{n} pages {low[:20]}")

    tables_path = out / "state" / "tables.json"
    if tables_path.exists():
        tabs = json.loads(tables_path.read_text(encoding="utf-8"))
        acc = [t for t in tabs if t["accepted"]]
        rej = [t for t in tabs if not t["accepted"]]
        reasons: dict[str, int] = {}
        for t in rej:
            reasons[t["rejected_as"]] = reasons.get(t["rejected_as"], 0) + 1
        print(f"tables        {len(acc)} accepted, {len(rej)} rejected")
        print(f"  rejections  {dict(sorted(reasons.items()))}")
        print(f"  cells       {sum(t['n_rows'] * t['n_cols'] for t in acc)}")
        flagged = [t for t in acc if t["flags"]]
        print(f"  flagged     {len(flagged)} accepted tables carry a flag")
    if not v_rules and h_rules:
        # Worth saying out loud: it is the regime that shaped the architecture.
        print("note          horizontally ruled, no vertical rules (partial-rule regime)")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    out: Path = args.out
    source = args.input
    if source is None:
        manifest = out / "manifest.json"
        if not manifest.exists():
            print(f"no run at {out}; pass --input", file=sys.stderr)
            return 2
        source = Path(json.loads(manifest.read_text(encoding="utf-8"))["source_path"])
    doc = run_layout(source, cfg)
    path = render_viewer(doc, out / "viewer.html")
    print(f"wrote {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .webapp import serve

    return serve(args.runs, Config.load(args.config), args.host, args.port)


def cmd_debug(args: argparse.Namespace) -> int:
    from .audit.render import render_page

    cfg = Config.load(args.config)
    out: Path = args.out
    manifest_path = out / "manifest.json"

    source = args.input
    if source is None:
        if not manifest_path.exists():
            print(f"no run at {out}; pass --input", file=sys.stderr)
            return 2
        source = Path(json.loads(manifest_path.read_text(encoding="utf-8"))["source_path"])

    doc = run_layout(source, cfg)
    if not (1 <= args.page <= doc.page_count):
        print(f"page {args.page} out of range 1..{doc.page_count}", file=sys.stderr)
        return 2

    png = out / "debug" / f"page_{args.page:03d}.png"
    render_page(source, doc.pages[args.page - 1], cfg, png)
    print(f"wrote {png}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "run": cmd_run,
        "audit": cmd_audit,
        "view": cmd_view,
        "serve": cmd_serve,
        "debug": cmd_debug,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
