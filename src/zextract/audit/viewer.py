"""S11 -- browsable HTML report of every table candidate.

Deliberately shows REJECTED candidates alongside accepted ones, with the reason
code. A viewer that only shows successes hides exactly the failure this
assignment is scored on: the difference between "we did not find it" and "we
found it and threw it away for a stated reason" is the whole point of the trap
classifier (DECISIONS.md D5).

Self-contained: no external CSS, JS or fonts, so it opens from the filesystem in
a network-isolated container.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path
from typing import Sequence

from ..model import Document, Table

_CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a18;--mut:#6b6b63;--line:#e2e1dc;--card:#fff;
--ok:#1f7a4d;--ok-bg:#e8f5ee;--no:#a8442a;--no-bg:#fbeee9;--warn:#8a6d1f;--warn-bg:#fdf6e3;
--num:#24405e;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:22px 28px;border-bottom:1px solid var(--line);background:var(--card)}
h1{margin:0 0 4px;font-size:19px;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px}
.wrap{max-width:1180px;margin:0 auto;padding:20px 28px 60px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:9px 14px;min-width:104px}
.stat b{display:block;font-size:19px;font-variant-numeric:tabular-nums}
.stat span{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.controls{margin:14px 0 22px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{font:inherit;padding:6px 13px;border:1px solid var(--line);background:var(--card);
border-radius:6px;cursor:pointer;color:var(--fg)}
button[aria-pressed="true"]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.t{background:var(--card);border:1px solid var(--line);border-radius:9px;margin:0 0 18px;overflow:hidden}
.th{padding:11px 15px;border-bottom:1px solid var(--line);display:flex;gap:10px;
align-items:baseline;flex-wrap:wrap}
.th h2{margin:0;font-size:14px;font-weight:600}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.b-ok{background:var(--ok-bg);color:var(--ok)}
.b-no{background:var(--no-bg);color:var(--no)}
.b-w{background:var(--warn-bg);color:var(--warn)}
.meta{color:var(--mut);font-size:12px;margin-left:auto;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}
td,th{border:1px solid var(--line);padding:4px 8px;vertical-align:top;text-align:left;
white-space:pre-wrap;max-width:340px}
th{background:#f5f5f2;font-weight:600;font-size:11px;color:var(--mut);
text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
td.right{text-align:right;font-variant-numeric:tabular-nums;color:var(--num)}
td.empty{background:#fcfcfb}
tr.flagged td:first-child{box-shadow:inset 3px 0 0 var(--warn)}
.rowno{color:#b9b9b2;font-variant-numeric:tabular-nums;text-align:right;
width:1%;background:#fafaf8;font-size:11px}
.none{color:var(--mut);padding:30px;text-align:center;background:var(--card);
border:1px dashed var(--line);border-radius:9px}
footer{color:var(--mut);font-size:12px;padding:0 28px 40px;max-width:1180px;margin:0 auto}
code{background:#f0efec;padding:1px 5px;border-radius:4px;font-size:11.5px}
"""

_JS = """
document.querySelectorAll('[data-filter]').forEach(function(btn){
  btn.addEventListener('click', function(){
    var mode = btn.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(function(b){
      b.setAttribute('aria-pressed', String(b === btn));
    });
    document.querySelectorAll('.t').forEach(function(t){
      var ok = t.dataset.state === 'accepted';
      t.hidden = !(mode === 'all' || (mode === 'accepted') === ok);
    });
  });
});
"""


def _grid(table: Table) -> list[list[str]]:
    g = [["" for _ in table.columns] for _ in table.rows]
    for c in table.cells:
        if 0 <= c.row_idx < len(g) and 0 <= c.col_idx < len(table.columns):
            g[c.row_idx][c.col_idx] = c.raw_text
    return g


def _table_html(doc: Document, table: Table) -> str:
    e = html.escape
    grid = _grid(table)
    state = "accepted" if table.accepted else "rejected"

    badges = [
        f'<span class="badge b-ok">accepted</span>'
        if table.accepted
        else f'<span class="badge b-no">{e(table.rejected_as or "rejected")}</span>'
    ]
    for f in table.flags:
        badges.append(f'<span class="badge b-w">{e(f)}</span>')

    head = "".join(
        f'<th title="x {c.x0:.0f}–{c.x1:.0f}, {c.alignment.value}-aligned">'
        f"c{c.index} · {e(c.alignment.value)}</th>"
        for c in table.columns
    )

    body = []
    for r, row in enumerate(grid):
        flagged = " flagged" if table.rows[r].flags else ""
        title = e(", ".join(table.rows[r].flags)) if table.rows[r].flags else ""
        tds = []
        for c, text in enumerate(row):
            cell = table.cell_at(r, c)
            cls = []
            if not text.strip():
                cls.append("empty")
            elif table.columns[c].alignment.value == "right":
                cls.append("right")
            prov = ""
            if cell is not None and cell.bbox is not None:
                b = cell.bbox
                prov = (
                    f"page {table.page_no} · bbox "
                    f"{b.x0:.0f},{b.y0:.0f},{b.x1:.0f},{b.y1:.0f} · {cell.source.value}"
                )
            tds.append(
                f'<td class="{" ".join(cls)}" title="{e(prov)}">{e(text)}</td>'
            )
        body.append(
            f'<tr class="{flagged.strip()}" title="{title}">'
            f'<td class="rowno">{r}</td>{"".join(tds)}</tr>'
        )

    heading = f"page {table.page_no} · table {table.index}"
    if table.title:
        heading += f" · {e(table.title)}"
    ctx = []
    if table.unit_scale_note:
        ctx.append(e(table.unit_scale_note))
    if table.caption:
        ctx.append(e(table.caption))
    ctx_html = f'<div class="meta" style="margin-left:0;width:100%">{ " · ".join(ctx)}</div>' if ctx else ""

    return f"""<section class="t" data-state="{state}">
  <div class="th">
    <h2>{heading}</h2>
    {"".join(badges)}
    <span class="meta">{len(table.rows)}&times;{len(table.columns)} ·
      {e(table.partition_source)} · support {table.support:.2f}</span>
    {ctx_html}
  </div>
  <div class="scroll"><table>
    <thead><tr><th class="rowno"></th>{head}</tr></thead>
    <tbody>{"".join(body)}</tbody>
  </table></div>
</section>"""


def render_viewer(doc: Document, out_path: Path | str) -> Path:
    """Write a self-contained HTML report of every table candidate."""
    e = html.escape
    tables: list[Table] = [t for p in doc.pages for t in p.tables]
    accepted = [t for t in tables if t.accepted]
    rejected = [t for t in tables if not t.accepted]
    reasons = Counter(t.rejected_as for t in rejected)
    cells = sum(len(t.cells) for t in accepted)
    flagged = sum(1 for t in accepted if t.flags)

    reason_list = (
        " · ".join(f"<code>{e(str(k))}</code> {v}" for k, v in sorted(reasons.items()))
        or "none"
    )

    sections = "\n".join(_table_html(doc, t) for t in tables) or (
        '<p class="none">No table candidates found in this document.</p>'
    )

    body = f"""<header>
  <h1>{e(doc.filename)}</h1>
  <div class="sub">{doc.page_count} pages · sha256 {e(doc.sha256[:16])}… ·
    stages S1&ndash;S4 (no normalisation, stitching, context or confidence yet)</div>
</header>
<div class="wrap">
  <div class="stats">
    <div class="stat"><b>{len(accepted)}</b><span>accepted</span></div>
    <div class="stat"><b>{len(rejected)}</b><span>rejected</span></div>
    <div class="stat"><b>{cells}</b><span>cells</span></div>
    <div class="stat"><b>{flagged}</b><span>flagged tables</span></div>
    <div class="stat"><b>{doc.page_count}</b><span>pages</span></div>
  </div>
  <p class="sub">Rejections: {reason_list}</p>
  <div class="controls">
    <button data-filter="accepted" aria-pressed="true">Accepted</button>
    <button data-filter="rejected" aria-pressed="false">Rejected</button>
    <button data-filter="all" aria-pressed="false">All</button>
  </div>
  {sections}
</div>
<footer>
  Hover a cell for its page and bounding box. Columns are labelled with their
  measured alignment; right-aligned columns are the numeric ones. An amber bar
  marks a row whose wrap status was ambiguous and was left unmerged rather than
  guessed.
</footer>"""

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>zextract · {e(doc.filename)}</title>
<style>{_CSS}</style></head>
<body>
{body}
<script>{_JS}</script>
</body></html>
"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)
    return out


def tables_json(doc: Document) -> str:
    """Flat dump of every table, for diffing and for the determinism check."""
    payload = []
    for p in doc.pages:
        for t in p.tables:
            payload.append(
                {
                    "page_no": t.page_no,
                    "index": t.index,
                    "n_rows": len(t.rows),
                    "n_cols": len(t.columns),
                    "accepted": t.accepted,
                    "rejected_as": t.rejected_as,
                    "flags": sorted(t.flags),
                    "partition_source": t.partition_source,
                    "support": round(t.support, 6),
                    "alignments": [c.alignment.value for c in t.columns],
                    "title": t.title,
                    "caption": t.caption,
                    "section_path": t.section_path,
                    "unit_scale_note": t.unit_scale_note,
                    "grid": _grid(t),
                    # Geometry, so a reviewer can put the reconstruction next to
                    # the pixels it came from without re-running the pipeline.
                    "bbox": [
                        round(t.bbox.x0, 3), round(t.bbox.y0, 3),
                        round(t.bbox.x1, 3), round(t.bbox.y1, 3),
                    ],
                    "page_width": round(p.width, 3),
                    "page_height": round(p.height, 3),
                    "row_bboxes": [
                        [round(r.bbox.x0, 3), round(r.bbox.y0, 3),
                         round(r.bbox.x1, 3), round(r.bbox.y1, 3)]
                        for r in t.rows
                    ],
                    "col_x": [[round(c.x0, 3), round(c.x1, 3)] for c in t.columns],
                    "row_label_paths": [list(r.row_label_path) for r in t.rows],
                    "confidences": [
                        [round(t.cell_at(r, c).confidence, 3)
                         if t.cell_at(r, c) else None
                         for c in range(len(t.columns))]
                        for r in range(len(t.rows))
                    ],
                }
            )
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
