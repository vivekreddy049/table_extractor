"""Import independently-authored ground truth into `ground_truth/`.

WHY THIS MATTERS MORE THAN THE CODE IN IT

Up to now the extractor and its labels share an author, so the evaluation is not
independent (LIMITATIONS.md 0b). Labels that come from someone who has not seen
the extractor remove that objection entirely -- they are worth more than any
number of self-authored ones, and this exists purely to remove the friction of
supplying them.

ACCEPTED INPUTS

  .csv   one table per file
  .xlsx  one table per SHEET, so a whole document's labels fit in one workbook
  .json  already in the target schema (validated and copied)

WHERE THE METADATA COMES FROM

Either from the filename:

    Apple Form 10-K__p32__income_statement.csv
    <document>__p<page>__<slug>.(csv|xlsx)

or, for a workbook, from the sheet name:

    p32__income_statement

or from explicit --document / --page flags.

A NEGATIVE (a page that must yield NO table) is a file or sheet whose only
content is the single cell NOT_A_TABLE, optionally followed by an expected
rejection code:

    NOT_A_TABLE,TRAP_TOC

CONVENTIONS THE LABELLER SHOULD KNOW

  * Record what the PAGE SAYS, cell by cell, including the header row.
  * Leave a cell blank when the page leaves it blank. Blank is meaningful --
    it is scored, and it is different from "-" or "Nil".
  * Merged cells: put the text in its top-left position and leave the rest of
    the span blank.
  * Transcribe the characters the document uses. Where a glyph is ambiguous,
    prefer what the text layer holds -- `metrics/verify_labels.py` will tell you
    if a cell string is not on the page, which is the fastest way to catch a
    transcription slip.

After importing, run:

    python metrics/verify_labels.py --samples <dir>     # are the labels real
    python metrics/eval.py          --samples <dir>     # how does it score
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

GT_DIR = Path(__file__).with_name("ground_truth")
_SLUG = re.compile(r"[^a-z0-9]+")
_NEGATIVE = "NOT_A_TABLE"


def slug(text: str) -> str:
    return _SLUG.sub("_", text.lower()).strip("_") or "table"


def parse_name(stem: str) -> tuple[str | None, int | None, str]:
    """Pull (document, page, name) out of `<doc>__p<page>__<slug>`."""
    parts = stem.split("__")
    doc = page = None
    rest: list[str] = []
    for part in parts:
        m = re.fullmatch(r"[pP](\d+)", part)
        if m and page is None:
            page = int(m.group(1))
        elif doc is None and page is None:
            doc = part
        else:
            rest.append(part)
    return doc, page, slug("_".join(rest) or stem)


def rows_from_csv(path: Path) -> list[list[str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return [[(c or "").strip() for c in row] for row in csv.reader(fh)]


def rows_from_sheet(ws) -> list[list[str]]:
    out = []
    for row in ws.iter_rows(values_only=True):
        out.append(["" if v is None else str(v).strip() for v in row])
    return out


def tidy(rows: list[list[str]]) -> list[list[str]]:
    """Drop wholly-empty trailing rows and columns, and pad to a rectangle."""
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    while width > 1 and all(not r[width - 1].strip() for r in rows):
        rows = [r[: width - 1] for r in rows]
        width -= 1
    return rows


def build(rows: list[list[str]], document: str, page: int, name: str, source: str) -> dict:
    # The eval resolves labels by `samples_dir / document`, so the extension has
    # to be there. Filenames commonly lose it when used as a prefix.
    if not document.lower().endswith(".pdf"):
        document = document + ".pdf"
    first = rows[0][0].strip().upper() if rows and rows[0] else ""
    if first == _NEGATIVE:
        codes = [c.strip() for c in rows[0][1:] if c.strip()]
        return {
            "label_id": f"{slug(document)}_p{page:03d}_{name}",
            "document": document,
            "page_no": page,
            "is_table": False,
            "expected_rejection_codes": codes,
            "authored_from": source,
            "notes": [
                "Independently authored: the labeller did not work from the "
                "extractor's output. This page must yield NO accepted table."
            ],
        }
    return {
        "label_id": f"{slug(document)}_p{page:03d}_{name}",
        "document": document,
        "page_no": page,
        "is_table": True,
        "authored_from": source,
        "notes": [
            "Independently authored: the labeller did not work from the "
            "extractor's output. This label is worth more than a self-authored "
            "one precisely because of that (LIMITATIONS.md 0b)."
        ],
        "grid": rows,
    }


def emit(label: dict, force: bool) -> bool:
    out = GT_DIR / f"{label['label_id']}.json"
    if out.exists() and not force:
        print(f"  SKIP  {out.name} already exists (use --force to overwrite)")
        return False
    GT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(label, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    shape = (
        f"{len(label['grid'])}x{len(label['grid'][0])}"
        if label.get("is_table")
        else "NEGATIVE"
    )
    print(f"  wrote {out.name:52} {shape}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help=".csv / .xlsx / .json")
    ap.add_argument("--document", help="PDF filename these labels belong to")
    ap.add_argument("--page", type=int, help="page number (single-table inputs)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    written = 0
    for path in args.inputs:
        if not path.exists():
            print(f"  MISSING {path}", file=sys.stderr)
            continue
        stem_doc, stem_page, stem_name = parse_name(path.stem)
        document = args.document or stem_doc
        source = f"independently hand-labelled, imported from {path.name}"

        if path.suffix.lower() == ".json":
            label = json.loads(path.read_text(encoding="utf-8"))
            for required in ("document", "page_no", "is_table"):
                if required not in label:
                    print(f"  {path.name}: missing '{required}'", file=sys.stderr)
                    break
            else:
                label.setdefault("label_id", path.stem)
                written += emit(label, args.force)
            continue

        if path.suffix.lower() == ".csv":
            page = args.page if args.page is not None else stem_page
            if not document or page is None:
                print(f"  {path.name}: need --document and --page (or "
                      f"'<doc>__p<page>__<name>.csv')", file=sys.stderr)
                continue
            rows = tidy(rows_from_csv(path))
            if not rows:
                print(f"  {path.name}: empty", file=sys.stderr)
                continue
            written += emit(build(rows, document, page, stem_name, source), args.force)
            continue

        if path.suffix.lower() in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook

            wb = load_workbook(path, data_only=True)
            for ws in wb.worksheets:
                sheet_doc, sheet_page, sheet_name = parse_name(ws.title)
                doc_name = document or sheet_doc
                page = sheet_page if sheet_page is not None else args.page
                if not doc_name or page is None:
                    print(f"  {path.name}[{ws.title}]: need a page in the sheet "
                          f"name ('p32__income_statement') or --page",
                          file=sys.stderr)
                    continue
                rows = tidy(rows_from_sheet(ws))
                if not rows:
                    continue
                written += emit(
                    build(rows, doc_name, page, sheet_name,
                          f"{source}, sheet {ws.title!r}"),
                    args.force,
                )
            continue

        print(f"  {path.name}: unsupported type", file=sys.stderr)

    print(f"\n{written} label(s) written to {GT_DIR}")
    if written:
        print("\nNext:")
        print('  python metrics/verify_labels.py --samples "<dir of sample PDFs>"')
        print('  python metrics/eval.py          --samples "<dir of sample PDFs>"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
