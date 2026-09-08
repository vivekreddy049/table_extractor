"""Audit the ground-truth labels against the PDF text layer.

WHY THIS EXISTS

The labels in `ground_truth/` were written by the same author as the extractor.
That is what the brief asks for ("hand-label a subset yourself"), but it means
the evaluation is not independent, and a reader is right to ask how the labels
themselves are checked.

This script checks the one thing that CAN be checked mechanically: whether every
string a label claims is on the page is actually in that page's text layer. On a
digital PDF the text layer is exact -- it is the bytes the producer wrote -- so
a label cell that does not appear there was invented or mistyped, regardless of
who wrote it.

WHAT THIS DOES NOT CHECK, AND CANNOT

Content is verifiable; STRUCTURE is judgement. Nothing here can confirm that a
table really has 4 columns rather than 5, that "Years ended" is a spanning
header rather than a row, or that a section heading belongs inside the table or
outside it. Those are exactly the decisions where an author labelling their own
extractor's output could share a blind spot with it, and they are also most of
what criteria 1 and 2 are scoring.

So: passing this audit means the labels are not fabricated. It does not mean
they are right. Independent labelling by someone who has not seen the extractor
is the only thing that would establish that, and it has not been done.

Run:  python metrics/verify_labels.py --samples "<dir of sample PDFs>"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import pymupdf

GT_DIR = Path(__file__).with_name("ground_truth")
_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """Whitespace- and case-insensitive, NFKC-folded."""
    s = unicodedata.normalize("NFKC", text or "")
    for ch in (" ", " ", " "):
        s = s.replace(ch, " ")
    return _WS.sub(" ", s).strip().casefold()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, required=True)
    ap.add_argument("--show", type=int, default=6)
    args = ap.parse_args()

    total_cells = 0
    total_missing = 0
    skipped: list[str] = []
    report: list[dict] = []

    for path in sorted(GT_DIR.glob("*.json")):
        lab = json.loads(path.read_text(encoding="utf-8"))
        if not lab.get("is_table"):
            continue
        if lab.get("label_type") == "semantic":
            # A semantic label deliberately records normalised labels
            # ("Net sales - Products") rather than the page's literal text
            # ("Products" under a "Net sales:" parent). Checking it for
            # verbatim presence tests the wrong thing and reports a
            # transcription error where there is none.
            skipped.append(lab["label_id"])
            continue
        pdf = args.samples / lab["document"]
        if not pdf.exists():
            print(f"  SKIP {lab['label_id']}: {pdf} not found", file=sys.stderr)
            continue

        # A stitched table legitimately spans pages, so the text of its whole
        # page RANGE is the corpus to check against -- not just its first page.
        # apple_p057_exhibit_index runs 57-58; without this, every row that
        # lives on the second page reads as fabricated.
        first = lab["page_no"]
        last = int(lab.get("end_page_no", first))
        doc = pymupdf.open(str(pdf))
        try:
            page_text = norm(
                " ".join(
                    doc[i - 1].get_text()
                    for i in range(first, last + 1)
                    if 0 < i <= doc.page_count
                )
            )
        finally:
            doc.close()

        missing: list = []
        hyphenated: list = []
        cells = 0
        for r, row in enumerate(lab["grid"]):
            for c, cell in enumerate(row):
                v = norm(cell)
                if not v:
                    continue
                cells += 1
                if v in page_text:
                    continue
                # A word hyphenated at a line break ("Non-" + "fund") is on the
                # page but not as one string. That is an extractor gap (S5 text
                # hygiene), not a fabricated label, and conflating the two would
                # hide the distinction this audit exists to draw.
                dehyphenated = page_text.replace("- ", "")
                if v.replace("-", "").replace(" ", "") in dehyphenated.replace(" ", ""):
                    hyphenated.append((r, c, cell))
                else:
                    missing.append((r, c, cell))

        total_cells += cells
        total_missing += len(missing)
        report.append(
            {"label_id": lab["label_id"], "cells": cells,
             "missing": missing, "hyphenated": hyphenated}
        )

    print("GROUND-TRUTH LABEL AUDIT")
    print("Does every labelled cell string actually appear in the page text layer?\n")
    ok = True
    for r in report:
        n = len(r["missing"])
        status = "OK  " if n == 0 else "FAIL"
        if n:
            ok = False
        print(f"  {status} {r['label_id']:36} {r['cells']:>4} cells, {n} not on page")
        for row, col, text in r["missing"][: args.show]:
            print(f"         NOT ON PAGE  r{row},c{col}: {text!r}")
        for row, col, text in r["hyphenated"][: args.show]:
            print(f"         hyphenated at a line break  r{row},c{col}: {text!r}")

    print(
        f"\n  {total_cells - total_missing}/{total_cells} labelled cell strings "
        f"verified present in the source text layer"
    )
    print(
        "\n  NOTE: this proves the labels are not fabricated. It does NOT prove the\n"
        "  STRUCTURE is right -- column counts, header rows, and where a table\n"
        "  starts and stops are judgement calls made by the same author as the\n"
        "  extractor, and are unverified."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
