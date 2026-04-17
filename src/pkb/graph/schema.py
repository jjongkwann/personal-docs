"""SQLite 스키마 정의 및 초기화."""

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS concepts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT UNIQUE NOT NULL,
    category        TEXT,
    description     TEXT,
    embedding       BLOB,
    mention_count   INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_aliases (
    concept_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    alias           TEXT NOT NULL,
    alias_slug      TEXT NOT NULL,
    PRIMARY KEY (concept_id, alias_slug)
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY,
    doc_id          TEXT UNIQUE NOT NULL,
    title           TEXT,
    category        TEXT
);

CREATE TABLE IF NOT EXISTS concept_edges (
    src_id          INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    dst_id          INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,
    weight          REAL DEFAULT 1.0,
    evidence_count  INTEGER DEFAULT 1,
    PRIMARY KEY (src_id, dst_id, relation)
);

CREATE TABLE IF NOT EXISTS concept_mentions (
    concept_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    section_path    TEXT,
    PRIMARY KEY (concept_id, doc_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS graph_runs (
    id               INTEGER PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    scope_category   TEXT,
    scope_doc_id     TEXT,
    chunks_processed INTEGER DEFAULT 0,
    concepts_added   INTEGER DEFAULT 0,
    edges_added      INTEGER DEFAULT 0,
    model            TEXT,
    status           TEXT
);

CREATE INDEX IF NOT EXISTS idx_concepts_slug ON concepts(slug);
CREATE INDEX IF NOT EXISTS idx_concepts_category ON concepts(category);
CREATE INDEX IF NOT EXISTS idx_concept_edges_src ON concept_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_concept_edges_dst ON concept_edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_concept_mentions_doc ON concept_mentions(doc_id);
CREATE INDEX IF NOT EXISTS idx_aliases_slug ON concept_aliases(alias_slug);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    """SQLite 커넥션 획득. 부모 디렉터리 자동 생성."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(db_path: str) -> None:
    """스키마 초기화 (존재하지 않는 테이블만 생성)."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
