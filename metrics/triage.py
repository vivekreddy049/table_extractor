"""Rank accepted tables by how unlike a data table they look.

Purpose: turn "there are many more like this" into a ranked worklist. Finding
false positives one screenshot at a time does not scale and does not tell you
how bad the problem is.

This is a TRIAGE AID, not a classifier. Nothing here rejects anything, and none
of these signals is wired into the pipeline. That is deliberate: on the current
labelled set none of them separates cleanly (a labelled true table has a 0%-fill
column; another is 0% numeric), so promoting any of them to a rejection rule
would be fitting a threshold to three documents. The score exists to decide
WHAT TO LABEL NEXT, so the decision can be measured instead of guessed.

Signals, each a dimensionless ratio:

  sparse_cols     share of columns filled on under a third of rows
  prose           mean words per filled cell, normalised
  no_numbers      1 - numeric share of filled cells
  unstable_align  share of columns with no stable left/right/centre edge
  label_repeat    share of rows whose label column repeats an earlier one
  ragged          share of rows whose filled-column pattern is unique

Run:  python metrics/triage.py --out out/apple [--top 20]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

_WS = re.compile(r"\s+")
_NUMERIC = {"integer", "decimal", "currency", "percent"}


def _load(db: Path) -> list[dict]:
    con = sqlite3.connect(str(db))
    try:
        tables = []
        for tid, pg, nr, nc, title in con.execute(
            "SELECT table_id,start_page,n_rows,n_cols,title FROM tables "
            "WHERE rejected_as IS NULL ORDER BY start_page"
        ):
            cells = con.execute(
                "SELECT row_idx,col_idx,raw_text,value_type FROM cells "
                "WHERE table_id=? ORDER BY row_idx,col_idx",
                (tid,),
            ).fetchall()
            aligns = [
                a for (a,) in con.execute(
                    "SELECT alignment FROM columns WHERE table_id=? ORDER BY col_idx",
                    (tid,),
                )
            ]
            tables.append(
                {"tid": tid, "page": pg, "n_rows": nr, "n_cols": nc,
                 "title": title, "cells": cells, "aligns": aligns}
            )
        return tables
    finally:
        con.close()


def score(t: dict) -> tuple[float, dict]:
    nr, nc = max(t["n_rows"], 1), max(t["n_cols"], 1)
    cells = t["cells"]
    filled = [(r, c, x, v) for r, c, x, v in cells if x and x.strip()]
    if not filled:
        return 1.0, {"empty": True}

    fill = collections.Counter(c for _, c, _, _ in filled)
    sparse_cols = sum(1 for c in range(nc) if fill[c] / nr < 1 / 3) / nc

    words = [len(_WS.split(x.strip())) for _, _, x, _ in filled]
    mean_words = sum(words) / len(words)
    prose = min(1.0, mean_words / 8.0)

    numeric = sum(1 for _, _, _, v in filled if v in _NUMERIC)
    no_numbers = 1.0 - numeric / len(filled)

    unstable = sum(1 for a in t["aligns"] if a == "mixed") / max(len(t["aligns"]), 1)

    labels = [x.strip() for r, c, x, _ in filled if c == 0]
    dupes = len(labels) - len(set(labels))
    label_repeat = dupes / max(len(labels), 1)

    patterns = collections.Counter()
    per_row = collections.defaultdict(set)
    for r, c, _, _ in filled:
        per_row[r].add(c)
    for r, cols in per_row.items():
        patterns["".join("1" if i in cols else "0" for i in range(nc))] += 1
    ragged = sum(1 for p, n in patterns.items() if n == 1) / max(len(per_row), 1)

    parts = {
        "sparse_cols": sparse_cols,
        "prose": prose,
        "no_numbers": no_numbers,
        "unstable_align": unstable,
        "label_repeat": label_repeat,
        "ragged": ragged,
    }
    # Unweighted mean: these are a worklist ordering, not a fitted model, and
    # inventing weights would imply a confidence the labelled set cannot support.
    return sum(parts.values()) / len(parts), parts


def preview(t: dict, width: int = 88) -> str:
    per_row = collections.defaultdict(dict)
    for r, c, x, _ in t["cells"]:
        if x and x.strip():
            per_row[r][c] = x.strip()
    out = []
    for r in sorted(per_row)[:2]:
        out.append(" | ".join(per_row[r][c][:26] for c in sorted(per_row[r]))[:width])
    return "  //  ".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True, help="a run directory")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    db = args.out / "extraction.db"
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2

    tables = _load(db)
    scored = []
    for t in tables:
        s, parts = score(t)
        scored.append({"page": t["page"], "n_rows": t["n_rows"], "n_cols": t["n_cols"],
                       "suspicion": round(s, 3), "signals": {k: round(v, 2) for k, v in parts.items()},
                       "preview": preview(t)})
    scored.sort(key=lambda x: (-x["suspicion"], x["page"]))

    n = len(scored)
    high = [x for x in scored if x["suspicion"] >= 0.6]
    print(f"{n} accepted tables · {len(high)} scoring >= 0.60 suspicion "
          f"({len(high)/n:.0%})\n")
    print(f"{'susp':>5}  {'page':>4}  {'shape':>7}  preview")
    for x in scored[: args.top]:
        print(f"{x['suspicion']:>5.2f}  {x['page']:>4}  "
              f"{x['n_rows']:>3}x{x['n_cols']:<3}  {x['preview'][:86]}")

    if args.json:
        args.json.write_text(
            json.dumps(scored, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
