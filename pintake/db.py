from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    name         TEXT,
    ext          TEXT,
    size         INTEGER,
    mtime        REAL,
    md5          TEXT,
    status       TEXT DEFAULT 'new',
    dupe_of      TEXT,
    pages        INTEGER,
    text_chars   INTEGER,
    ocr          INTEGER DEFAULT 0,
    text         TEXT,
    meta_json    TEXT,
    decision     TEXT,
    override     TEXT,
    paperless_id INTEGER,
    task_id      TEXT,
    error        TEXT,
    updated      REAL
);
CREATE INDEX IF NOT EXISTS idx_files_md5    ON files(md5);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

CREATE TABLE IF NOT EXISTS paperless_docs (
    checksum TEXT PRIMARY KEY,
    doc_id   INTEGER,
    orig     TEXT,
    title    TEXT,
    synced   REAL
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def touch(con, path, **fields):
    fields["updated"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE files SET {sets} WHERE path=?", (*fields.values(), path))
    con.commit()
