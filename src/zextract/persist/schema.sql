-- S10 -- SQLite schema.
--
-- The brief's minimum schema, extended where noted, never reduced. Tables for
-- stages that are not built yet (footnotes, cell_footnotes) are created
-- empty rather than omitted, so the output contract holds and a query written
-- against it will not have to change when those stages land.

CREATE TABLE IF NOT EXISTS documents (
    doc_id     TEXT PRIMARY KEY,
    filename   TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    page_id   TEXT PRIMARY KEY,
    doc_id    TEXT NOT NULL REFERENCES documents(doc_id),
    page_no   INTEGER NOT NULL,
    width     REAL NOT NULL,
    height    REAL NOT NULL,
    page_type TEXT NOT NULL,
    rotation  INTEGER NOT NULL,
    -- extension: the deskew angle applied, so a bbox can be mapped back to
    -- original page space, and the self-calibration this page ran on.
    deskew_angle REAL NOT NULL DEFAULT 0.0,
    line_pitch   REAL,
    word_gap     REAL,
    stats_source TEXT
);

-- The only table carrying a timestamp. Everything else is timestamp-free so
-- that two runs diff byte-for-byte.
CREATE TABLE IF NOT EXISTS extraction_runs (
    run_id      TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id),
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    git_sha     TEXT,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tables (
    table_id       TEXT PRIMARY KEY,
    doc_id         TEXT NOT NULL REFERENCES documents(doc_id),
    run_id         TEXT NOT NULL REFERENCES extraction_runs(run_id),
    logical_index  INTEGER NOT NULL,
    start_page     INTEGER NOT NULL,
    end_page       INTEGER NOT NULL,
    n_rows         INTEGER NOT NULL,
    n_cols         INTEGER NOT NULL,
    title          TEXT,
    caption        TEXT,
    section_path   TEXT,
    unit_scale_note TEXT,
    is_continuation INTEGER NOT NULL DEFAULT 0,
    detector_votes_json TEXT,
    table_confidence REAL,
    -- extension: why a candidate was refused, NULL when accepted. Rejections
    -- are recorded rather than dropped so a wrong refusal is one query away.
    rejected_as    TEXT,
    flags_json     TEXT
);

CREATE TABLE IF NOT EXISTS table_regions (
    region_id TEXT PRIMARY KEY,
    table_id  TEXT NOT NULL REFERENCES tables(table_id),
    page_id   TEXT NOT NULL REFERENCES pages(page_id),
    bbox_x0 REAL, bbox_y0 REAL, bbox_x1 REAL, bbox_y1 REAL
);

CREATE TABLE IF NOT EXISTS columns (
    column_id     TEXT PRIMARY KEY,
    table_id      TEXT NOT NULL REFERENCES tables(table_id),
    col_idx       INTEGER NOT NULL,
    header_path   TEXT,
    inferred_type TEXT,
    unit          TEXT,
    nullable      INTEGER,
    -- extension: measured alignment is the strongest pre-S7 type hint, and
    -- the agreement share says how firm the inferred type is.
    alignment      TEXT,
    type_agreement REAL
);

-- Extension. The brief's litmus query -- "every cell whose column header path
-- contains 'Revenue', with its page number and unit" -- must be a join, not a
-- LIKE over a JSON blob. This is that join's right-hand side.
CREATE TABLE IF NOT EXISTS column_header_tokens (
    column_id TEXT NOT NULL REFERENCES columns(column_id),
    level     INTEGER NOT NULL,
    token     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cells (
    cell_id   TEXT PRIMARY KEY,
    table_id  TEXT NOT NULL REFERENCES tables(table_id),
    row_idx   INTEGER NOT NULL,
    col_idx   INTEGER NOT NULL,
    row_span  INTEGER NOT NULL DEFAULT 1,
    col_span  INTEGER NOT NULL DEFAULT 1,
    is_header INTEGER NOT NULL DEFAULT 0,
    raw_text  TEXT NOT NULL,
    normalized_value REAL,
    value_type TEXT,
    row_label_path TEXT,
    page_id   TEXT NOT NULL REFERENCES pages(page_id),
    bbox_x0 REAL, bbox_y0 REAL, bbox_x1 REAL, bbox_y1 REAL,
    source    TEXT NOT NULL,
    confidence REAL,
    -- extension: the normalised text form (dates, error literals), the unit and
    -- scale that were stripped, and which parse rule matched -- so a value can
    -- be re-derived from its raw text without rerunning the pipeline.
    normalized_text TEXT,
    unit  TEXT,
    scale TEXT,
    parse_rule TEXT,
    codes_json TEXT
);

-- Extension, and the row-side twin of column_header_tokens. The thing a reader
-- searches a financial statement for ("Revenue", "Trade Receivables") is a ROW
-- label, and its parent is expressed only by indentation. Without this table
-- the database can say which COLUMN a cell is in but not which LINE ITEM, and
-- the brief's litmus query has nothing to match on.
CREATE TABLE IF NOT EXISTS row_label_tokens (
    table_id TEXT NOT NULL REFERENCES tables(table_id),
    row_idx  INTEGER NOT NULL,
    level    INTEGER NOT NULL,
    token    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS footnotes (
    footnote_id TEXT PRIMARY KEY,
    doc_id  TEXT NOT NULL REFERENCES documents(doc_id),
    page_id TEXT REFERENCES pages(page_id),
    marker  TEXT,
    text    TEXT
);

CREATE TABLE IF NOT EXISTS cell_footnotes (
    cell_id     TEXT NOT NULL REFERENCES cells(cell_id),
    footnote_id TEXT NOT NULL REFERENCES footnotes(footnote_id)
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    doc_id   TEXT NOT NULL REFERENCES documents(doc_id),
    page_id  TEXT REFERENCES pages(page_id),
    kind     TEXT,
    file_path TEXT,
    caption  TEXT,
    bbox_x0 REAL, bbox_y0 REAL, bbox_x1 REAL, bbox_y1 REAL
);

CREATE TABLE IF NOT EXISTS table_assets (
    table_id TEXT NOT NULL REFERENCES tables(table_id),
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    relation TEXT,
    relation_confidence REAL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    table_id TEXT REFERENCES tables(table_id),
    cell_id  TEXT REFERENCES cells(cell_id),
    severity TEXT NOT NULL,
    code     TEXT NOT NULL,
    message  TEXT
);

CREATE INDEX IF NOT EXISTS idx_cells_table ON cells(table_id);
CREATE INDEX IF NOT EXISTS idx_cells_conf  ON cells(confidence);
CREATE INDEX IF NOT EXISTS idx_cols_table  ON columns(table_id);
CREATE INDEX IF NOT EXISTS idx_hdr_token   ON column_header_tokens(token);
CREATE INDEX IF NOT EXISTS idx_issues_code ON issues(code);
CREATE INDEX IF NOT EXISTS idx_rowlbl_token ON row_label_tokens(token);
CREATE INDEX IF NOT EXISTS idx_rowlbl_table ON row_label_tokens(table_id, row_idx);
