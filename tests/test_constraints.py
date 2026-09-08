"""The hard constraints from the brief, as executable tests.

Determinism and offline operation are scored criteria and automatic
disqualifiers. Asserting them in prose is worth nothing; these run in CI.
"""

from __future__ import annotations

import ast
import hashlib
import socket
from pathlib import Path

import pytest

from zextract.model import dumps
from zextract.pipeline import run_layout

SRC = Path(__file__).resolve().parents[1] / "src" / "zextract"


def test_two_runs_produce_identical_state(ruled_pdf, cfg):
    a = dumps(run_layout(ruled_pdf, cfg))
    b = dumps(run_layout(ruled_pdf, cfg))
    assert a == b


def test_pipeline_opens_no_socket(ruled_pdf, cfg, monkeypatch):
    """Prove the offline constraint rather than asserting it.

    The brief runs the pipeline in a network-isolated container; a test that
    merely says "we don't use the network" would not have caught a dependency
    that phones home on import.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("pipeline attempted to open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    run_layout(ruled_pdf, cfg)


def _numeric_literals(path: Path) -> list[tuple[int, float]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, float]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            out.append((node.lineno, node.value))
    return out


# Structural values, not measurements: identity, a half, a whole, and infinity
# as "no limit". None of these is a length, an angle or a size.
_ALLOWED = {0.0, 0.5, 1.0, 2.0, float("inf")}


@pytest.mark.parametrize(
    "module",
    sorted(
        f"{d}/{p.name}"
        for d in ("s2_layout", "s3_detect", "s4_structure")
        for p in (SRC / d).glob("*.py")
        if p.name != "__init__.py"
    ),
)
def test_no_absolute_measurement_constants_in_layout_code(module):
    """DECISIONS.md D20, enforced.

    A bare ``6.0`` in detection code is a threshold tuned to one font size on
    one producer's output, and it is the single most likely reason a pipeline
    that scores well on three sample PDFs collapses on a held-out set. Every
    threshold belongs in config.yaml, expressed as a multiple of a page-derived
    statistic.

    Scoped to S2 (and, as they land, S3/S4) -- the stages where layout
    thresholds live. S1's docstrum module is exempt by design: it contains the
    angle constants 45/90/180, which are properties of the plane rather than of
    any document.
    """
    offenders = [
        (line, value)
        for line, value in _numeric_literals(SRC / module)
        if value not in _ALLOWED
    ]
    assert not offenders, (
        f"{module} contains absolute measurement constants {offenders}; "
        "move them to config.yaml as a multiple of a page statistic"
    )


def test_config_thresholds_are_all_named_and_documented():
    """Every config key carries a comment saying what its unit is.

    The rule in D20 is only half about the code: a config full of undocumented
    magic numbers is the same failure in a different file.
    """
    text = (SRC.parents[1] / "config.yaml").read_text(encoding="utf-8")
    lines = text.splitlines()
    undocumented = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if not value.strip():  # a section header
            continue
        # Look upward for at least one comment line.
        j = i - 1
        documented = False
        while j >= 0 and lines[j].strip().startswith("#"):
            documented = True
            j -= 1
        if not documented:
            undocumented.append(key.strip())
    assert not undocumented, f"config keys without a comment: {undocumented}"


def test_excel_and_database_output_is_byte_deterministic(ruled_pdf, cfg, tmp_path):
    """The brief diffs the Excel files, not just the database.

    openpyxl leaks wall-clock time in two places -- the zip entry headers and
    docProps/core.xml, whose `modified` value it overwrites inside save()
    regardless of what was set beforehand. Both are neutralised in
    persist.excel._save_deterministic, and this is what proves it.
    """
    import sqlite3

    from zextract.config import Config
    from zextract.persist import write_database, write_workbook
    from zextract.pipeline import run_layout

    stamp = "2020-01-01T00:00:00+00:00"
    digests = []
    for run in ("a", "b"):
        doc = run_layout(ruled_pdf, cfg)
        out = tmp_path / run
        write_database(doc, cfg, out / "extraction.db", stamp, stamp)
        n = 0
        for page in doc.pages:
            for table in page.tables:
                if table.accepted:
                    write_workbook(doc, page, table, n, cfg, out / "tables")
                    n += 1
        assert n, "fixture produced no accepted table to persist"
        digests.append(
            {
                p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((out / "tables").rglob("*.xlsx"))
            }
        )

    assert digests[0] == digests[1], "xlsx bytes differ between runs"

    rows = []
    for run in ("a", "b"):
        con = sqlite3.connect(str(tmp_path / run / "extraction.db"))
        try:
            rows.append(con.execute("SELECT * FROM cells ORDER BY cell_id").fetchall())
        finally:
            con.close()
    assert rows[0] == rows[1]


def test_two_cli_runs_produce_byte_identical_output(ruled_pdf, tmp_path):
    """The brief's check is a two-run byte diff of the OUTPUT DIRECTORY.

    The existing determinism test hands the persistence layer a fixed stamp, so
    it proves the writers are reproducible but says nothing about who supplies
    the timestamp. The CLI took it from the wall clock, and a full `zextract
    run` therefore differed between two runs on `extraction.db` alone -- every
    extracted value, and even the content-derived run_id and doc_id, matched.

    That is the whole failure the brief calls an automatic disqualifier, so it
    is tested where a reviewer would meet it: through the command line, over
    the entire output tree.
    """
    from zextract.cli import main

    digests = []
    for run in ("a", "b"):
        out = tmp_path / run
        rc = main(["run", "--input", str(ruled_pdf), "--out", str(out), "--offline"])
        assert rc == 0, f"run {run} exited {rc}"
        digests.append(
            {
                p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(out.rglob("*"))
                if p.is_file()
            }
        )

    assert digests[0] == digests[1], (
        "two runs differ: "
        + ", ".join(
            k for k in digests[0] if digests[0].get(k) != digests[1].get(k)
        )
    )
