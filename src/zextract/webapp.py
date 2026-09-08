"""Local web interface: upload a PDF, run the pipeline, see the stats.

Standard library only -- no Flask, no FastAPI, no CDN assets. Adding a web
framework to satisfy the brief's offline constraint would mean vendoring it into
the image for a page that renders four tables, and the CSS is inlined for the
same reason: the page has to render inside a network-isolated container.

Binds to 127.0.0.1 by default. This is a local inspection tool, not a service:
it executes the pipeline on whatever file is posted to it, so it must not be
exposed to a network without authentication in front of it.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from .config import Config
from .persist import persist_run
from .pipeline import LOW_CONFIDENCE_LAYOUT, run_layout

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_UPLOAD = 200 * 1024 * 1024  # a 400-page PDF is comfortably under this

_CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a18;--mut:#6b6b63;--line:#e2e1dc;--card:#fff;
--ok:#1f7a4d;--ok-bg:#e8f5ee;--no:#a8442a;--no-bg:#fbeee9;--warn:#8a6d1f;--warn-bg:#fdf6e3}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:20px 28px;border-bottom:1px solid var(--line);background:var(--card)}
h1{margin:0;font-size:18px;letter-spacing:-.01em}
h1 a{color:inherit;text-decoration:none}
.sub{color:var(--mut);font-size:13px;margin-top:3px}
.wrap{max-width:1000px;margin:0 auto;padding:22px 28px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:18px 20px;margin:0 0 18px}
h2{margin:0 0 12px;font-size:14px;font-weight:600}
input[type=file]{font:inherit;max-width:100%}
button{font:inherit;padding:8px 16px;border:1px solid var(--fg);background:var(--fg);
color:var(--bg);border-radius:6px;cursor:pointer}
button:disabled{opacity:.5;cursor:wait}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:4px 0 0}
.stat{border:1px solid var(--line);border-radius:7px;padding:9px 14px;min-width:104px}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.stat span{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left}
th{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
td.n{text-align:right;font-variant-numeric:tabular-nums}
a{color:#24405e}
.links a{display:inline-block;margin:0 14px 8px 0}
.warn{background:var(--warn-bg);border:1px solid #e8d9a8;border-radius:7px;
padding:12px 14px;margin:0 0 18px;font-size:13px}
.err{background:var(--no-bg);border:1px solid #e8c4b6;border-radius:7px;padding:14px;
white-space:pre-wrap;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
code{background:#f0efec;padding:1px 5px;border-radius:4px;font-size:12px}
.muted{color:var(--mut);font-size:12.5px}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.b-ok{background:var(--ok-bg);color:var(--ok)}
.b-no{background:var(--no-bg);color:var(--no)}
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
@media (max-width:900px){.cmp{grid-template-columns:1fr}}
.cmp img{width:100%;border:1px solid var(--line);border-radius:7px;background:#fff}
.grid{overflow:auto;max-height:78vh;border:1px solid var(--line);border-radius:7px}
.grid table{border-collapse:collapse;width:100%;font-size:12px}
.grid td,.grid th{border:1px solid var(--line);padding:3px 7px;vertical-align:top;
white-space:pre-wrap;max-width:260px}
.grid th{background:#f5f5f2;position:sticky;top:0;font-size:11px;color:var(--mut);
text-transform:uppercase;letter-spacing:.04em}
.grid td.r{text-align:right;font-variant-numeric:tabular-nums;color:#24405e}
.dl{margin-top:10px;font-size:13px;color:#6b6b62}
.dl a{display:inline-block;margin-left:8px;padding:4px 10px;border:1px solid #cfcfc6;
  border-radius:5px;background:#fff;text-decoration:none;color:#24405e}
.dl a:hover{background:#f3f3ee}
.grid td.low{background:#fdf3e3}
.rowno{color:#b9b9b2;text-align:right;background:#fafaf8;font-size:11px;width:1%}
.pathcol{color:var(--mut);font-size:11px;max-width:200px}
.pager{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.pager a{display:inline-block;padding:4px 10px;border:1px solid var(--line);
border-radius:6px;background:var(--card);text-decoration:none;font-size:12.5px}
.pager a.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>zextract · {html.escape(title)}</title><style>{_CSS}</style></head>"
        "<body><header><h1><a href='/'>zextract</a></h1>"
        "<div class='sub'>deterministic table extraction · no language model "
        "touches the document</div></header>"
        f"<div class='wrap'>{body}</div></body></html>"
    ).encode("utf-8")


def _safe_name(name: str) -> str:
    return _SAFE.sub("_", Path(name).name).strip("._") or "upload.pdf"


def _parse_multipart(body: bytes, content_type: str) -> tuple[str, bytes] | None:
    """Pull the single uploaded file out of a multipart/form-data body.

    Hand-rolled because `cgi.FieldStorage` is deprecated and removed in 3.13,
    and this needs exactly one field. Returns (filename, content) or None.
    """
    m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
    if not m:
        return None
    boundary = (m.group(1) or m.group(2)).strip().encode("latin-1")
    parts = body.split(b"--" + boundary)
    for part in parts:
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, _, content = part.partition(b"\r\n\r\n")
        headers = raw_headers.decode("latin-1", "replace")
        fn = re.search(r'filename="([^"]*)"', headers)
        if not fn or not fn.group(1):
            continue
        # Trailing CRLF belongs to the boundary, not the file.
        if content.endswith(b"\r\n"):
            content = content[:-2]
        return fn.group(1), content
    return None


def _run_pipeline(pdf_path: Path, out_dir: Path, cfg: Config) -> dict:
    """Run everything and write the same artefacts the CLI writes."""
    t0 = time.perf_counter()
    doc = run_layout(pdf_path, cfg)
    elapsed = time.perf_counter() - t0

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metrics = persist_run(doc, cfg, out_dir, pdf_path, stamp, stamp)

    tables = [t for p in doc.pages for t in p.tables]
    reasons: dict[str, int] = {}
    for t in tables:
        if t.rejected_as:
            reasons[t.rejected_as] = reasons.get(t.rejected_as, 0) + 1
    page_types: dict[str, int] = {}
    for p in doc.pages:
        page_types[p.page_type.value] = page_types.get(p.page_type.value, 0) + 1

    stats = {
        **metrics,
        "filename": doc.filename,
        "sha256": doc.sha256,
        "seconds": round(elapsed, 2),
        "ms_per_page": round(elapsed / doc.page_count * 1000) if doc.page_count else 0,
        "page_types": page_types,
        "low_confidence_pages": [
            p.page_no for p in doc.pages if LOW_CONFIDENCE_LAYOUT in p.notes
        ],
        "rejection_codes": dict(sorted(reasons.items())),
        "workbooks": metrics["tables_accepted"],
        "h_rules": sum(len(p.horizontal_rules()) for p in doc.pages),
        "v_rules": sum(len(p.vertical_rules()) for p in doc.pages),
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return stats


def _stat(value, label) -> str:
    return f"<div class='stat'><b>{value}</b><span>{html.escape(label)}</span></div>"


def _stats_html(run_id: str, s: dict) -> str:
    e = html.escape
    codes = (
        "".join(
            f"<tr><td><code>{e(k)}</code></td><td class='n'>{v}</td></tr>"
            for k, v in s["rejection_codes"].items()
        )
        or "<tr><td colspan='2' class='muted'>none</td></tr>"
    )
    types = ", ".join(f"{k} {v}" for k, v in sorted(s["page_types"].items()))
    low = s["low_confidence_pages"]
    low_txt = f"{len(low)} page(s): {low[:15]}" if low else "none"

    return f"""
<div class='warn'>
  <b>These are counts, not accuracy.</b> Nothing on this page is scored against
  ground truth. “{s['tables_accepted']} tables accepted” means that many
  candidates survived the trap classifier, not that many are correct. The
  confidence behind the flag count is <b>uncalibrated</b> — see
  <code>LIMITATIONS.md</code>.
</div>

<div class='card'>
  <h2>{e(s['filename'])}</h2>
  <div class='stats'>
    {_stat(s['pages'], 'pages')}
    {_stat(s['tables_accepted'], 'tables accepted')}
    {_stat(s['tables_rejected'], 'candidates rejected')}
    {_stat(s['cells'], 'cells')}
    {_stat(f"{s['flagged_share']*100:.1f}%", 'cells flagged')}
    {_stat(f"{s['seconds']}s", 'wall clock')}
    {_stat(s.get('assets', 0), 'assets')}
    {_stat(s.get('titles_extracted', 0), 'titles')}
  </div>
  <p class='muted' style='margin-top:14px'>
    {s['ms_per_page']} ms/page · page types: {e(types)} ·
    rules h={s['h_rules']} v={s['v_rules']} ·
    low-confidence layout: {e(low_txt)}<br>
    sha256 {e(s['sha256'][:24])}…
  </p>
</div>

<div class='card'>
  <h2>Why candidates were rejected</h2>
  <table><tr><th>reason code</th><th>count</th></tr>{codes}</table>
  <p class='muted' style='margin-top:10px'>
    Rejections are recorded rather than dropped: “never found it” and “found it
    and refused it for a stated reason” are different outcomes.
  </p>
</div>

<div class='card'>
  <h2>Artefacts</h2>
  <div class='links'>
    <a href='/run/{run_id}/compare/0'><b>Compare against the original →</b></a>
    <a href='/run/{run_id}/viewer.html'>table viewer</a>
    <a href='/run/{run_id}/review_queue.csv'>review_queue.csv</a>
    <a href='/run/{run_id}/metrics.json'>metrics.json</a>
    <a href='/run/{run_id}/extraction.db'>extraction.db</a>
    <a href='/run/{run_id}/state/tables.json'>tables.json</a>
    <a href='/run/{run_id}/logs/run.jsonl'>run.jsonl</a>
    <a href='/run/{run_id}/assets/index.json'>assets/index.json</a>
  </div>
  <p class='muted'>{s['workbooks']} Excel workbooks written to
    <code>runs/{e(run_id)}/tables/</code> (one per accepted table, four sheets each).</p>
</div>
"""


def _compare_html(run_id: str, tables: list, idx: int) -> str:
    """Side-by-side: the source page as rendered, and what we reconstructed.

    The only way to tell a good extraction from a plausible-looking one is to
    put it next to the pixels it came from. Cells below the confidence band are
    tinted, and the row-label hierarchy recovered from indentation gets its own
    column -- a grid can look perfectly right and still have lost the parent
    every line item hangs from.
    """
    e = html.escape
    t = tables[idx]
    grid = t["grid"]
    conf = t.get("confidences") or []
    paths = t.get("row_label_paths") or []
    aligns = t.get("alignments") or []

    pager = "".join(
        "<a class='{on}' href='/run/{rid}/compare/{i}'>p{pg}&middot;t{ti}{bad}</a>".format(
            on="on" if i == idx else "",
            rid=run_id,
            i=i,
            pg=x["page_no"],
            ti=x["index"],
            bad="" if x["accepted"] else " \u2717",
        )
        for i, x in enumerate(tables)
    )

    head = "<th class='rowno'></th><th class='pathcol'>row label path</th>" + "".join(
        "<th>c{}</th>".format(c) for c in range(t["n_cols"])
    )

    body = []
    for r, row in enumerate(grid):
        tds = []
        for c, text in enumerate(row):
            cls = []
            if c < len(aligns) and aligns[c] == "right":
                cls.append("r")
            cv = conf[r][c] if r < len(conf) and c < len(conf[r]) else None
            if text.strip() and cv is not None and cv < 0.9:
                cls.append("low")
            title = "confidence {}".format(cv) if cv is not None else ""
            tds.append(
                "<td class='{}' title='{}'>{}</td>".format(" ".join(cls), title, e(text))
            )
        path = " \u203a ".join(paths[r]) if r < len(paths) else ""
        body.append(
            "<tr><td class='rowno'>{}</td><td class='pathcol'>{}</td>{}</tr>".format(
                r, e(path), "".join(tds)
            )
        )

    ctx = []
    for key, label in (
        ("title", "title"),
        ("unit_scale_note", "unit/scale"),
        ("caption", "caption"),
        ("section_path", "section"),
    ):
        if t.get(key):
            ctx.append("<b>{}:</b> {}".format(label, e(str(t[key]))))

    badge = (
        "<span class='badge b-ok'>accepted</span>"
        if t["accepted"]
        else "<span class='badge b-no'>{}</span>".format(e(str(t["rejected_as"])))
    )

    return """
<div class='pager'>{pager}</div>
<div class='card'>
  <h2>page {pg} &middot; table {ti} {badge}</h2>
  <p class='muted'>{nr}&times;{nc} &middot; {src} &middot; support {sup:.2f}{ctx}</p>
  <p class='dl'>Download side-by-side comparison:
    <a href='/run/{rid}/comparepdf/{idx}'>this table</a>
    <a href='/run/{rid}/comparepdf/accepted'>all accepted</a>
    <a href='/run/{rid}/comparepdf/all'>everything, incl. rejected</a>
  </p>
</div>
<div class='cmp'>
  <div>
    <p class='muted'>Source page {pg} &mdash; the extraction region is outlined in red</p>
    <img src='/run/{rid}/pageimg/{pg}/{idx}.png' alt='source page {pg}'>
  </div>
  <div>
    <p class='muted'>Reconstructed grid &mdash; amber cells fall below the
      confidence band; hover any cell for its score</p>
    <div class='grid'><table><thead><tr>{head}</tr></thead>
      <tbody>{body}</tbody></table></div>
  </div>
</div>
""".format(
        pager=pager,
        pg=t["page_no"],
        ti=t["index"],
        badge=badge,
        nr=t["n_rows"],
        nc=t["n_cols"],
        src=e(t["partition_source"]),
        sup=t["support"],
        ctx=(" &middot; " + " &middot; ".join(ctx)) if ctx else "",
        rid=run_id,
        idx=idx,
        head=head,
        body="".join(body),
    )


def _render_page_png(pdf_path, page_no: int, bbox, dpi: float = 130.0) -> bytes:
    """Render one page, outlining the region a table was taken from.

    Drawn rather than cropped: seeing the region in the context of the whole
    page is what makes a WRONG region obvious. A crop of the wrong area just
    looks like a different table.
    """
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        page = doc[page_no - 1]
        if bbox:
            page.draw_rect(
                pymupdf.Rect(*bbox), color=(0.85, 0.15, 0.1), width=1.2, overlay=True
            )
        scale = dpi / 72.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _grid_html_for_pdf(t: dict) -> str:
    """The reconstructed grid as standalone HTML, for the PDF export.

    Kept separate from the on-screen table because the constraints differ: the
    PDF has a fixed box to fill, no hover, and no scrolling. Low-confidence
    cells keep their tint (it is the point of the comparison), but the tooltip
    that carries the score on screen becomes a printed suffix instead, since a
    reader of the PDF cannot hover.
    """
    e = html.escape
    grid = t["grid"]
    conf = t.get("confidences") or []
    paths = t.get("row_label_paths") or []
    aligns = t.get("alignments") or []

    head = "<th></th><th>row label path</th>" + "".join(
        "<th>c{}</th>".format(c) for c in range(t["n_cols"])
    )
    rows = []
    for r, row in enumerate(grid):
        tds = []
        for c, text in enumerate(row):
            style = "padding:2px 4px;border:0.5pt solid #d8d8d0;"
            if c < len(aligns) and aligns[c] == "right":
                style += "text-align:right;"
            cv = conf[r][c] if r < len(conf) and c < len(conf[r]) else None
            if text.strip() and cv is not None and cv < 0.9:
                style += "background:#fdf3e0;"
            label = e(text)
            if text.strip() and cv is not None and cv < 0.9:
                label += " <span style='color:#a06000'>({:.2f})</span>".format(cv)
            tds.append("<td style='{}'>{}</td>".format(style, label))
        path = " &rsaquo; ".join(e(x) for x in (paths[r] if r < len(paths) else []))
        rows.append(
            "<tr><td style='color:#aaa;padding:2px 4px'>{}</td>"
            "<td style='padding:2px 4px;color:#556;border:0.5pt solid #d8d8d0'>{}</td>"
            "{}</tr>".format(r, path, "".join(tds))
        )
    return (
        "<div style='font-family:sans-serif;font-size:7pt'>"
        "<table style='border-collapse:collapse;width:100%'>"
        "<tr style='background:#eee'>{}</tr>{}</table></div>"
    ).format(head, "".join(rows))


def _comparison_pdf(run_dir: Path, tables: list, indices: list[int]) -> bytes:
    """Source page beside reconstructed grid, one landscape sheet per table.

    The same comparison the browser shows, in a file that can be sent to
    someone who does not have the server running. That is the whole reason it
    exists: an extraction is only reviewable next to the pixels it came from,
    and review is the part of this pipeline that has to survive leaving the
    machine it ran on.
    """
    import pymupdf

    pdfs = sorted(run_dir.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError("source pdf missing from run")
    src = pdfs[0]

    # _render_page_png reopens the source per page. Sharing one handle was
    # measured and is NOT worth it: the cost is the 150-DPI rasterisation, not
    # the open (46 tables went 14.3s -> 16.4s), and a shared handle means the
    # red outline drawn for one table is still on the page when a second table
    # from the SAME page is rendered. Reopening keeps each sheet honest.
    out = pymupdf.open()
    for idx in indices:
        t = tables[idx]
        page = out.new_page(width=842, height=595)  # A4 landscape
        mid = 421.0

        status = "accepted" if t["accepted"] else "rejected: {}".format(
            t.get("rejected_as") or "?"
        )
        page.insert_htmlbox(
            pymupdf.Rect(24, 14, 818, 44),
            "<div style='font-family:sans-serif'>"
            "<b style='font-size:11pt'>{}</b><br>"
            "<span style='font-size:8pt;color:#555'>page {} &middot; table {} &middot; "
            "{}&times;{} &middot; {}</span></div>".format(
                html.escape(src.stem), t["page_no"], t["index"],
                len(t["grid"]), t["n_cols"], html.escape(status),
            ),
        )

        img = _render_page_png(src, t["page_no"], t.get("bbox"), dpi=150.0)
        page.insert_image(pymupdf.Rect(24, 50, mid - 12, 575), stream=img, keep_proportion=True)

        # scale_low lets the grid shrink to fit rather than silently truncate.
        page.insert_htmlbox(
            pymupdf.Rect(mid + 12, 50, 818, 575),
            _grid_html_for_pdf(t),
            scale_low=0.1,
        )
        page.draw_line(
            pymupdf.Point(mid, 50), pymupdf.Point(mid, 575),
            color=(0.85, 0.85, 0.82), width=0.5,
        )

    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "zextract"
    runs_dir: Path
    cfg: Config

    def log_message(self, fmt, *args):  # quieter console
        pass

    # ---------------- GET ----------------
    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])

        if path == "/":
            return self._send(_page("upload", self._index_html()))

        if path.startswith("/run/"):
            rest = path[len("/run/"):]
            run_id, _, rel = rest.partition("/")
            run_dir = (self.runs_dir / _safe_name(run_id)).resolve()
            if not str(run_dir).startswith(str(self.runs_dir.resolve())):
                return self._send(_page("error", "<div class='err'>bad path</div>"), 400)
            if rel.startswith("comparepdf"):
                tpath = run_dir / "state" / "tables.json"
                if not tpath.exists():
                    return self._send(_page("404", "<div class='err'>no run</div>"), 404)
                tables = json.loads(tpath.read_text(encoding="utf-8"))
                if not tables:
                    return self._send(_page("404", "<div class='err'>no tables</div>"), 404)
                parts = rel.split("/")
                arg = parts[1] if len(parts) > 1 else "all"
                if arg == "all":
                    indices = list(range(len(tables)))
                    fname = "comparison__all.pdf"
                elif arg == "accepted":
                    indices = [i for i, x in enumerate(tables) if x["accepted"]]
                    fname = "comparison__accepted.pdf"
                elif arg.isdigit():
                    i = max(0, min(int(arg), len(tables) - 1))
                    indices = [i]
                    fname = "comparison__p{}_t{}.pdf".format(
                        tables[i]["page_no"], tables[i]["index"]
                    )
                else:
                    return self._send(_page("404", "<div class='err'>bad selector</div>"), 404)
                if not indices:
                    return self._send(
                        _page("404", "<div class='err'>nothing to compare</div>"), 404
                    )
                try:
                    data = _comparison_pdf(run_dir, tables, indices)
                except FileNotFoundError:
                    return self._send(
                        _page("404", "<div class='err'>source pdf missing</div>"), 404
                    )
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(data)))
                self.send_header(
                    "Content-Disposition", 'attachment; filename="{}"'.format(fname)
                )
                self.end_headers()
                self.wfile.write(data)
                return

            if rel == "compare" or rel.startswith("compare/"):
                parts = rel.split("/")
                idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                tpath = run_dir / "state" / "tables.json"
                if not tpath.exists():
                    return self._send(_page("404", "<div class='err'>no run</div>"), 404)
                tables = json.loads(tpath.read_text(encoding="utf-8"))
                if not tables:
                    return self._send(
                        _page("compare", "<div class='card'>No table candidates.</div>")
                    )
                idx = max(0, min(idx, len(tables) - 1))
                return self._send(_page("compare", _compare_html(run_id, tables, idx)))

            if rel.startswith("pageimg/"):
                parts = rel.split("/")
                try:
                    page_no = int(parts[1])
                    idx = int(parts[2].split(".")[0])
                except (IndexError, ValueError):
                    return self._send(_page("404", "<div class='err'>bad image path</div>"), 404)
                tables = json.loads(
                    (run_dir / "state" / "tables.json").read_text(encoding="utf-8")
                )
                bbox = tables[idx]["bbox"] if 0 <= idx < len(tables) else None
                pdfs = sorted(run_dir.glob("*.pdf"))
                if not pdfs:
                    return self._send(
                        _page("404", "<div class='err'>source pdf missing</div>"), 404
                    )
                data = _render_page_png(pdfs[0], page_no, bbox)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if not rel:
                meta = run_dir / "metrics.json"
                if not meta.exists():
                    return self._send(_page("404", "<div class='err'>no such run</div>"), 404)
                stats = json.loads(meta.read_text(encoding="utf-8"))
                return self._send(_page(stats["filename"], _stats_html(run_id, stats)))
            return self._send_file(run_dir, rel)

        return self._send(_page("404", "<div class='err'>not found</div>"), 404)

    # ---------------- POST ----------------
    def do_POST(self) -> None:
        if self.path != "/upload":
            return self._send(_page("404", "<div class='err'>not found</div>"), 404)

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_UPLOAD:
            return self._send(
                _page("error", "<div class='err'>missing or oversized upload</div>"), 400
            )

        parsed = _parse_multipart(self.rfile.read(length), self.headers.get("Content-Type", ""))
        if parsed is None:
            return self._send(_page("error", "<div class='err'>no file in request</div>"), 400)

        filename, content = parsed
        if not content.startswith(b"%PDF"):
            return self._send(
                _page(
                    "error",
                    "<div class='err'>That does not look like a PDF "
                    "(no %PDF header). Nothing was run.</div>",
                ),
                400,
            )

        stem = _safe_name(filename)
        run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}__{Path(stem).stem}"[:80]
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = run_dir / stem
        if not pdf_path.name.lower().endswith(".pdf"):
            pdf_path = pdf_path.with_suffix(".pdf")
        pdf_path.write_bytes(content)

        try:
            self._run_pipeline(pdf_path, run_dir)
        except Exception:
            shutil.rmtree(run_dir, ignore_errors=True)
            return self._send(
                _page(
                    "failed",
                    "<div class='card'><h2>Extraction failed</h2>"
                    "<p class='muted'>The traceback is shown rather than a generic "
                    "message: this is a debugging tool, and a silent failure here "
                    "would be worse than an ugly one.</p>"
                    f"<div class='err'>{html.escape(traceback.format_exc())}</div></div>",
                ),
                500,
            )

        self.send_response(303)
        self.send_header("Location", f"/run/{run_id}")
        self.end_headers()

    def _run_pipeline(self, pdf_path: Path, run_dir: Path) -> None:
        _run_pipeline(pdf_path, run_dir, self.cfg)

    # ---------------- helpers ----------------
    def _index_html(self) -> str:
        rows = []
        for d in sorted(self.runs_dir.glob("*/metrics.json"), reverse=True)[:25]:
            try:
                s = json.loads(d.read_text(encoding="utf-8"))
            except Exception:
                continue
            rid = html.escape(d.parent.name)
            rows.append(
                f"<tr><td><a href='/run/{rid}'>{html.escape(s['filename'])}</a></td>"
                f"<td class='n'>{s['pages']}</td>"
                f"<td class='n'>{s['tables_accepted']}</td>"
                f"<td class='n'>{s['tables_rejected']}</td>"
                f"<td class='n'>{s['cells']}</td>"
                f"<td class='n'>{s['flagged_share']*100:.1f}%</td>"
                f"<td class='n'>{s['seconds']}s</td></tr>"
            )
        history = (
            "<div class='card'><h2>Previous runs</h2><table>"
            "<tr><th>document</th><th>pages</th><th>accepted</th><th>rejected</th>"
            "<th>cells</th><th>flagged</th><th>time</th></tr>"
            + "".join(rows)
            + "</table></div>"
            if rows
            else ""
        )
        return f"""
<div class='card'>
  <h2>Upload a PDF</h2>
  <form method='post' action='/upload' enctype='multipart/form-data'
        onsubmit="this.querySelector('button').disabled=true;
                  this.querySelector('button').textContent='Extracting…';">
    <p><input type='file' name='pdf' accept='application/pdf,.pdf' required></p>
    <button type='submit'>Extract</button>
  </form>
  <p class='muted' style='margin-top:14px'>
    Runs the full pipeline (S1–S4, S7–S11) and writes the same artefacts as
    <code>python -m zextract run</code>. A 121-page document takes about 25
    seconds; the page will wait. Scanned pages are not supported yet — the OCR
    path is unbuilt, so an image-only PDF will produce no tables.
  </p>
</div>
{history}
"""

    def _send(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, run_dir: Path, rel: str) -> None:
        target = (run_dir / rel).resolve()
        if not str(target).startswith(str(run_dir.resolve())) or not target.is_file():
            return self._send(_page("404", "<div class='err'>not found</div>"), 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".jsonl": "application/jsonl; charset=utf-8",
            ".csv": "text/csv; charset=utf-8",
            ".db": "application/octet-stream",
            ".png": "image/png",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if target.suffix.lower() in (".db", ".xlsx", ".csv"):
            self.send_header(
                "Content-Disposition", f'attachment; filename="{target.name}"'
            )
        self.end_headers()
        self.wfile.write(data)


def serve(runs_dir: Path, cfg: Config, host: str = "127.0.0.1", port: int = 8000) -> int:
    runs_dir.mkdir(parents=True, exist_ok=True)
    Handler.runs_dir = runs_dir
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"zextract UI on http://{host}:{port}  (runs written to {runs_dir})")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
