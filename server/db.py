"""SQLite storage. One short-lived connection per call, so worker threads and requests never share one."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,          -- the name the user uploaded
    stored_name TEXT NOT NULL,       -- <id><ext> under data/folders/<folder_id>/
    page_count INTEGER NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('text', 'markdown')),
    status TEXT NOT NULL,            -- queued running done cancelled interrupted failed
    page_total INTEGER NOT NULL,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,            -- ok, incomplete (model stopped early), failed (error)
    content TEXT NOT NULL,           -- raw model output, never edited
    meta TEXT,                       -- JSON: line confidences, dropped lines (text mode)
    error TEXT,
    seconds REAL,
    UNIQUE (job_id, page_no)
);
-- Kinds of chat message: upload (user sent files), ocr_result (one job's card).
-- Later: question / answer for asking about a folder's documents.
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- user / assistant
    type TEXT NOT NULL,
    payload TEXT NOT NULL,           -- JSON
    created_at REAL NOT NULL
);
-- Ids are never reused (SQLite reuses the largest id after a delete), so a job that is
-- still stopping after its chat was deleted can never be mistaken for a new one.
CREATE TABLE IF NOT EXISTS id_seq (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS messages_folder ON messages(folder_id, id);
CREATE INDEX IF NOT EXISTS jobs_folder ON jobs(folder_id);
CREATE INDEX IF NOT EXISTS pages_job ON pages(job_id, page_no);

-- trigram: matches any run of 3+ characters, so Thai needs no word cutting;
-- ASCII letters match regardless of case.
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(content, tokenize = 'trigram');
CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    DELETE FROM pages_fts WHERE rowid = old.id;
END;
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# A "folders" row is either a chat (holds files, jobs, messages) or a folder that groups chats.
# Chats keep the table name so file paths (data/folders/<chat id>/) and older data stay valid.
MIGRATIONS = [
    ("kind", "ALTER TABLE folders ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'"),
    ("parent_id", "ALTER TABLE folders ADD COLUMN parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE"),
]


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(folders)")}
    for col, sql in MIGRATIONS:
        if col not in have:
            conn.execute(sql)
    conn.execute("CREATE INDEX IF NOT EXISTS folders_parent ON folders(parent_id)")
    conn.commit()
    conn.close()


def now() -> float:
    return time.time()


def row(r) -> dict | None:
    return dict(r) if r is not None else None


def next_id(conn, table: str) -> int:
    """A fresh id for folders/documents/jobs: above every id ever handed out."""
    assert table in ("folders", "documents", "jobs")
    top = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
    seen = conn.execute("SELECT value FROM id_seq WHERE name=?", (table,)).fetchone()
    value = max(top, seen[0] if seen else 0) + 1
    conn.execute("INSERT INTO id_seq(name, value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                 (table, value))
    return value


def add_message(conn, folder_id: int, role: str, type_: str, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO messages(folder_id, role, type, payload, created_at) VALUES (?,?,?,?,?)",
        (folder_id, role, type_, json.dumps(payload, ensure_ascii=False), now()))
    return cur.lastrowid
