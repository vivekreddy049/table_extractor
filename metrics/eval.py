"""Evaluation harness -- score the pipeline against hand-labelled ground truth.

Run:  python metrics/eval.py --samples "<dir of sample PDFs>" --out metrics/results.json

What is measured, and how honestly:

  DETECTION      For a labelled positive: did an accepted table on that page
                 match the label? For a labelled negative: did we wrongly accept
                 anything on that page?

                 Matching is by CELL CONTENT OVERLAP, not by IoU@0.7 as the
                 brief specifies. IoU needs hand-drawn ground-truth bounding
                 boxes, and none were drawn -- fabricating them from the
                 extractor's own output would make the metric circular and
                 meaningless. This is a stated substitution, not an oversight.

  STRUCTURE      Exact row and column counts, plus a structure similarity in the
                 spirit of TEDS-Struct: the normalised edit distance between the
                 two grids' shape signatures (per-row cell-occupancy patterns).
                 This is NOT the published TEDS metric -- it ignores spans,
                 because the extractor does not produce spans.

  CONTENT        Exact match on cell text after alignment. Rows are aligned by
                 their label column with difflib, because the extractor and the
                 label frequently disagree on row COUNT (a wrapped header split
                 in two, say) and naive index alignment would then score every
                 subsequent row as wrong for a single upstream mistake.

  CALIBRATION    The one that matters most. Ground truth tells us which
                 predicted cells are actually WRONG; the review queue tells us
                 which we FLAGGED. From those two we get the flagging precision
                 and recall the brief asks for, plus reliability bins showing
                 whether a confidence of 0.9 means anything at all.

Every number this produces is computed over the labelled set in
``ground_truth/``. That set is still small, and the output says so.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zextract.config import Config  # noqa: E402
from zextract.pipeline import run_layout  # noqa: E402

GT_DIR = Path(__file__).with_name("ground_truth")
_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """Compare cell text ignoring whitespace and case only.

    Deliberately NOT normalising punctuation, currency symbols or digit
    grouping: those are exactly the things the extractor could get wrong, and
    normalising them away would hide real errors.
    """
    return _WS.sub(" ", (text or "").replace(" ", " ")).strip().casefold()


def load_labels() -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(GT_DIR.glob("*.json"))
    ]


def grid_of(table) -> list[list[str]]:
    g = [["" for _ in table.columns] for _ in table.rows]
    for c in table.cells:
        if 0 <= c.row_idx < len(g) and 0 <= c.col_idx < len(table.columns):
            g[c.row_idx][c.col_idx] = c.raw_text
    return g


def content_overlap(a: list[list[str]], b: list[list[str]]) -> float:
    """Jaccard over the multiset of non-empty normalised cell strings."""
    sa = [norm(x) for row in a for x in row if norm(x)]
    sb = [norm(x) for row in b for x in row if norm(x)]
    if not sa or not sb:
        return 0.0
    from collections import Counter

    ca, cb = Counter(sa), Counter(sb)
    inter = sum((ca & cb).values())
    union = sum((ca | cb).values())
    return inter / union if union else 0.0


def row_signature(row: list[str]) -> str:
    """Occupancy pattern of a row: which cells hold content."""
    return "".join("1" if norm(c) else "0" for c in row)


def structure_similarity(pred: list[list[str]], gt: list[list[str]]) -> float:
    """Normalised edit distance between the two grids' shape signatures."""
    a = [row_signature(r) for r in pred]
    b = [row_signature(r) for r in gt]
    if not a and not b:
        return 1.0
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    return sm.ratio()


def align_rows(pred: list[list[str]], gt: list[list[str]]) -> list[tuple[int, int]]:
    """Pair predicted rows to label rows by their label column."""
    a = [norm(r[0]) if r else "" for r in pred]
    b = [norm(r[0]) if r else "" for r in gt]
    pairs: list[tuple[int, int]] = []
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            pairs.extend((i1 + k, j1 + k) for k in range(i2 - i1))
        elif tag == "replace":
            # Pair positionally within the replaced block; unmatched rows on
            # either side simply go unscored and are counted as misses.
            for k in range(min(i2 - i1, j2 - j1)):
                pairs.append((i1 + k, j1 + k))
    return pairs



_NUM_CLEAN = re.compile(r"[^\d.\-]")


def to_number(text: str):
    """Parse a label's cell into a float, or None.

    Only used in SEMANTIC mode, where the label carries plain figures and the
    page carries formatted ones. Handles the two conventions a hand-written
    label actually uses: thousands separators, and parentheses for negatives.
    """
    t = (text or "").strip()
    if not t:
        return None
    negative = t.startswith("(") and t.endswith(")")
    t = _NUM_CLEAN.sub("", t)
    if not t or t in {"-", ".", "-."}:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if negative else v


def semantic_row_key(table, row_idx: int) -> str:
    """How a predicted row is identified when matching a semantic label.

    The label flattens the row hierarchy ("Net sales - Products"); the extractor
    keeps it as row_label_path (["Net sales:", "Products"]). Joining the path is
    what lets the two meet without either side being penalised for a
    representation the other did not choose.
    """
    row = table.rows[row_idx] if row_idx < len(table.rows) else None
    if row is not None and row.row_label_path:
        return norm(" ".join(row.row_label_path))
    cell = table.cell_at(row_idx, 0)
    return norm(cell.raw_text) if cell else ""


def align_rows_semantic(table, gt: list[list[str]]) -> list[tuple[int, int]]:
    """Pair predicted rows to semantic-label rows BY THEIR FIGURES.

    Matching on label wording does not work here, and the failure is instructive:
    with a text-similarity key, the label row "Total net sales" (which carries
    figures) was paired to the predicted SECTION row "Net sales:" (which carries
    none), because the two strings resemble each other. "Gross margin" was then
    left with no partner, and "Basic EPS" never matched "earnings per share:
    basic" at any sane threshold. The result was a 71.4% content score on a
    table the extractor had actually got right -- the metric was wrong, not the
    pipeline.

    A semantic label is a statement about NUMBERS, so the numbers are the key.
    A row's figure vector is close to unique inside one table, and it is immune
    to however either side chose to word or nest the label. Rows with no figures
    (section headings, the header row) fall back to label similarity, which is
    all that is available and all that they need.
    """
    pred_nums: list[list[float]] = []
    for r in range(len(table.rows)):
        vals = []
        for c in range(len(table.columns)):
            cell = table.cell_at(r, c)
            if cell is not None and cell.normalized_value is not None:
                vals.append(cell.normalized_value)
        pred_nums.append(vals)

    pairs: list[tuple[int, int]] = []
    used: set[int] = set()

    # Pass 1 -- rows that carry figures, matched on those figures.
    for gi, grow in enumerate(gt):
        want = [v for v in (to_number(c) for c in grow[1:]) if v is not None]
        if not want:
            continue
        best, best_hits = None, 0
        for pi, got in enumerate(pred_nums):
            if pi in used or not got:
                continue
            hits = sum(1 for w in want if any(abs(w - g) < 0.005 for g in got))
            if hits > best_hits:
                best, best_hits = pi, hits
        # A majority of the label row's figures must be present; one incidental
        # shared number is coincidence, not a row.
        if best is not None and best_hits >= max(1, (len(want) + 1) // 2):
            pairs.append((best, gi))
            used.add(best)

    # Pass 2 -- figureless rows, on label wording, which is all there is.
    for gi, grow in enumerate(gt):
        if any(gi == g for _, g in pairs):
            continue
        target = norm(grow[0] if grow else "")
        if not target:
            continue
        best, best_score = None, 0.0
        for pi in range(len(table.rows)):
            if pi in used:
                continue
            score = difflib.SequenceMatcher(
                a=semantic_row_key(table, pi), b=target, autojunk=False
            ).ratio()
            if score > best_score:
                best, best_score = pi, score
        if best is not None and best_score >= 0.55:
            pairs.append((best, gi))
            used.add(best)

    return pairs


def numeric_overlap(table, gt: list[list[str]]) -> float:
    """Match overlap for a SEMANTIC label: compare figures, not strings.

    The string-based gate rejects a semantic label before it can be scored --
    "294,866" and "$ 294,866" share no characters at the comparison level, so a
    perfectly extracted table looked like a 0.47 overlap and counted as a
    detection miss. Semantic labels are about figures, so the gate must be too.
    """
    want = {
        v for row in gt for v in (to_number(c) for c in row) if v is not None
    }
    if not want:
        return 0.0
    got = {
        c.normalized_value
        for c in table.cells
        if c.normalized_value is not None
    }
    if not got:
        return 0.0
    hit = sum(1 for w in want if any(abs(w - g) < 0.005 for g in got))
    return hit / len(want)


def evaluate(samples_dir: Path, cfg: Config) -> dict:
    labels = load_labels()
    by_doc: dict[str, list[dict]] = {}
    for lab in labels:
        by_doc.setdefault(lab["document"], []).append(lab)

    detection = {"positives": 0, "matched": 0, "negatives": 0, "false_positives": 0}
    per_table: list[dict] = []
    cell_records: list[dict] = []

    for doc_name, doc_labels in sorted(by_doc.items()):
        pdf = samples_dir / doc_name
        if not pdf.exists():
            raise SystemExit(f"sample not found: {pdf}")
        doc = run_layout(pdf, cfg)

        for lab in doc_labels:
            page = doc.pages[lab["page_no"] - 1]
            accepted = [t for t in page.tables if t.accepted]

            if not lab["is_table"]:
                detection["negatives"] += 1
                if accepted:
                    detection["false_positives"] += 1
                per_table.append(
                    {
                        "label_id": lab["label_id"],
                        "kind": "negative",
                        "accepted_tables_on_page": len(accepted),
                        "correct": not accepted,
                        "rejection_codes": sorted(
                            {t.rejected_as for t in page.tables if t.rejected_as}
                        ),
                    }
                )
                continue

            detection["positives"] += 1
            gt = lab["grid"]
            is_semantic = lab.get("label_type") == "semantic"
            best, best_ov = None, 0.0
            for t in accepted:
                ov = (
                    numeric_overlap(t, gt) if is_semantic
                    else content_overlap(grid_of(t), gt)
                )
                if ov > best_ov:
                    best, best_ov = t, ov

            if best is None or best_ov < 0.5:
                per_table.append(
                    {
                        "label_id": lab["label_id"],
                        "kind": "positive",
                        "matched": False,
                        "best_content_overlap": round(best_ov, 4),
                    }
                )
                continue

            detection["matched"] += 1
            pred = grid_of(best)
            semantic = is_semantic
            pairs = (
                align_rows_semantic(best, gt) if semantic else align_rows(pred, gt)
            )

            exact = total = 0
            label_pairs: list[tuple[str, str]] = []
            for pi, gi in pairs:
                for c in range(min(len(pred[pi]), len(gt[gi]))):
                    total += 1
                    if semantic:
                        # Compare MEANING, not bytes: '$ 294,866' and '294,866'
                        # are the same figure, and the label was written by
                        # someone reading the page rather than its text layer.
                        want = to_number(gt[gi][c])
                        cell = best.cell_at(pi, c)
                        got = cell.normalized_value if cell else None
                        if want is not None:
                            ok = got is not None and abs(got - want) < 0.005
                        elif c == 0:
                            # The label column is scored SEPARATELY, not here.
                            #
                            # A semantic label flattens the row hierarchy
                            # ("Net sales - Products") where the extractor keeps
                            # it as a path (["Net sales:", "Products"]). Both
                            # carry the same fact. Comparing them as exact
                            # strings measures which representation the labeller
                            # picked, not whether the extraction is right -- so
                            # it would report an error where there is none.
                            #
                            # It is not simply dropped either: agreement is
                            # measured as a similarity and reported as
                            # row_label_similarity, so a genuinely wrong row
                            # label still shows up.
                            total -= 1
                            label_pairs.append(
                                (semantic_row_key(best, pi), norm(gt[gi][c]))
                            )
                            continue
                        else:
                            ok = norm(pred[pi][c]) == norm(gt[gi][c])
                    else:
                        ok = norm(pred[pi][c]) == norm(gt[gi][c])
                    exact += ok
                    cell = best.cell_at(pi, c)
                    if cell is not None:
                        cell_records.append(
                            {
                                "label_id": lab["label_id"],
                                "author": lab.get("author", "self"),
                                "correct": bool(ok),
                                "confidence": cell.confidence,
                                "flagged": bool(
                                    cell.confidence < cfg.f("confidence.review_threshold")
                                    or cell.codes
                                ),
                            }
                        )

            per_table.append(
                {
                    "label_id": lab["label_id"],
                    "kind": "positive",
                    "matched": True,
                    "best_content_overlap": round(best_ov, 4),
                    "pred_shape": [len(pred), len(best.columns)],
                    "gt_shape": [len(gt), len(gt[0]) if gt else 0],
                    "shape_exact": [len(pred), len(best.columns)]
                    == [len(gt), len(gt[0]) if gt else 0],
                    "structure_similarity": round(structure_similarity(pred, gt), 4),
                    "cells_compared": total,
                    "scored_as": "normalized_value" if semantic else "raw_text",
                    "row_label_similarity": (
                        round(
                            sum(
                                difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()
                                for a, b in label_pairs
                            )
                            / len(label_pairs),
                            4,
                        )
                        if label_pairs
                        else None
                    ),
                    "cell_exact_match": round(exact / total, 4) if total else 0.0,
                }
            )

    by_author: dict[str, list[dict]] = {}
    for rec in cell_records:
        by_author.setdefault(rec["author"], []).append(rec)

    return {
        "labelled_tables": len(labels),
        "label_authors": {
            a: sum(1 for l in labels if l.get("author", "self") == a)
            for a in sorted({l.get("author", "self") for l in labels})
        },
        "cells_by_author": {a: _cell_metrics(r) for a, r in sorted(by_author.items())},
        "detection": detection,
        "per_table": per_table,
        "cells": _cell_metrics(cell_records),
        "caveats": [
            f"{len(labels)} labelled tables. Too few to generalise from.",
            "Detection matched by cell-content overlap, not IoU>=0.7: no "
            "ground-truth bounding boxes were hand-drawn, and deriving them "
            "from the extractor's own output would be circular.",
            "structure_similarity is TEDS-Struct-like, not TEDS: spans are "
            "ignored because the extractor does not produce spans.",
            "Confidence is uncalibrated; the reliability bins below show what "
            "it is currently worth, which is the point of measuring it.",
            "Labels are authored by the same party as the extractor unless "
            "marked otherwise; see label_authors and cells_by_author.",
            "label_type 'semantic' labels are scored on normalized_value with "
            "rows matched on the joined row_label_path; 'raw_text' labels are "
            "scored on the literal cell string. The brief asks for both.",
        ],
    }


def _cell_metrics(records: list[dict]) -> dict:
    if not records:
        return {"n": 0}
    n = len(records)
    wrong = [r for r in records if not r["correct"]]
    flagged = [r for r in records if r["flagged"]]
    caught = [r for r in wrong if r["flagged"]]

    # The silent errors: wrong, and shipped without a flag. The brief calls
    # these the only unforgivable failure.
    silent = [r for r in wrong if not r["flagged"]]

    bins: list[dict] = []
    for lo in (0.0, 0.2, 0.4, 0.6, 0.8, 0.9):
        hi = 1.01 if lo == 0.9 else lo + (0.1 if lo == 0.8 else 0.2)
        inb = [r for r in records if lo <= r["confidence"] < hi]
        if inb:
            bins.append(
                {
                    "confidence_range": [lo, round(hi, 2)],
                    "n": len(inb),
                    "mean_confidence": round(
                        sum(r["confidence"] for r in inb) / len(inb), 4
                    ),
                    "observed_accuracy": round(
                        sum(r["correct"] for r in inb) / len(inb), 4
                    ),
                }
            )

    return {
        "n": n,
        "exact_match": round(sum(r["correct"] for r in records) / n, 4),
        "wrong": len(wrong),
        "flagged": len(flagged),
        "flagged_share": round(len(flagged) / n, 4),
        "flag_precision": round(len(caught) / len(flagged), 4) if flagged else None,
        "flag_recall": round(len(caught) / len(wrong), 4) if wrong else None,
        "silent_errors": len(silent),
        "silent_error_rate": round(len(silent) / n, 4),
        "reliability_bins": bins,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path(__file__).with_name("results.json"))
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()

    result = evaluate(args.samples, Config.load(args.config))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(result, indent=2, sort_keys=True) + "\n")

    d = result["detection"]
    c = result["cells"]
    print(f"labelled tables      {result['labelled_tables']}  "
          f"by author: {result['label_authors']}")
    print(f"detection            {d['matched']}/{d['positives']} positives matched, "
          f"{d['false_positives']}/{d['negatives']} negatives wrongly accepted")
    if c.get("n"):
        print(f"cells compared       {c['n']}")
        print(f"cell exact match     {c['exact_match']:.1%}")
        print(f"flagged              {c['flagged']} ({c['flagged_share']:.1%})")
        print(f"flag precision       {c['flag_precision']}")
        print(f"flag recall          {c['flag_recall']}")
        print(f"SILENT ERRORS        {c['silent_errors']} ({c['silent_error_rate']:.1%})")
    if len(result["cells_by_author"]) > 1:
        print("\n  split by who wrote the label:")
        for a, m in result["cells_by_author"].items():
            if m.get("n"):
                print(f"    {a:14} n={m['n']:>4}  exact={m['exact_match']:.1%}  "
                      f"silent_errors={m['silent_error_rate']:.1%}")
        print("    ('self' = same author as the extractor, so not independent"
              " -- LIMITATIONS.md 0b)")

    for t in result["per_table"]:
        if t["kind"] == "positive" and t.get("matched"):
            print(f"  {t['label_id']:<36} shape {t['pred_shape']} vs {t['gt_shape']}  "
                  f"struct {t['structure_similarity']:.2f}  content {t['cell_exact_match']:.1%}")
        else:
            print(f"  {t['label_id']:<36} {t}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
