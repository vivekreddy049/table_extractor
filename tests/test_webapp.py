"""Smoke test for the local upload UI.

Not a UI test -- it checks the three things that would be silently wrong: that a
posted PDF actually runs the pipeline and writes artefacts, that a non-PDF is
refused before anything executes, and that path traversal out of the run
directory is blocked (the handler serves files from disk by name).
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

import pytest

from zextract.webapp import Handler


@pytest.fixture()
def server(tmp_path, cfg):
    Handler.runs_dir = tmp_path / "runs"
    Handler.runs_dir.mkdir()
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(base, filename, content):
    b = "----" + uuid.uuid4().hex
    body = (
        f'--{b}\r\nContent-Disposition: form-data; name="pdf"; '
        f'filename="{filename}"\r\n\r\n'
    ).encode() + content + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(
        base + "/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"},
    )
    return urllib.request.urlopen(req, timeout=120)


def test_index_renders(server):
    assert urllib.request.urlopen(server + "/", timeout=10).status == 200


def test_upload_runs_the_pipeline_and_writes_artefacts(server, ruled_pdf):
    resp = _post(server, "ruled.pdf", ruled_pdf.read_bytes())
    page = resp.read().decode("utf-8", "replace")
    assert resp.status == 200
    assert "tables accepted" in page
    # The honesty banner is part of the contract, not decoration.
    assert "counts, not accuracy" in page

    runs = list(Handler.runs_dir.glob("*/metrics.json"))
    assert len(runs) == 1
    run_dir = runs[0].parent
    for name in (
        "viewer.html",
        "review_queue.csv",
        "extraction.db",
        "metrics.json",
        "logs/run.jsonl",
        "assets/index.json",
    ):
        assert (run_dir / name).exists(), name


def test_non_pdf_is_refused_before_anything_runs(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "notes.txt", b"this is not a pdf")
    assert exc.value.code == 400
    assert not list(Handler.runs_dir.glob("*/metrics.json"))


def test_path_traversal_is_blocked(server, ruled_pdf):
    _post(server, "ruled.pdf", ruled_pdf.read_bytes())
    run_id = next(Handler.runs_dir.glob("*/metrics.json")).parent.name
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            f"{server}/run/{run_id}/../../../../etc/passwd", timeout=10
        )
    assert exc.value.code == 404
