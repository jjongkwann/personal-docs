"""SQLite 스키마 정의 및 초기화."""

import contextlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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
    confidence      REAL,               -- 이산 루브릭 0.9/0.7/0.5 (NULL=루브릭 도입 전 구데이터)
    PRIMARY KEY (src_id, dst_id, relation)
);

CREATE TABLE IF NOT EXISTS concept_edge_evidence (
    doc_id          TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    src_id          INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    dst_id          INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,
    confidence      REAL,
    PRIMARY KEY (doc_id, chunk_index, src_id, dst_id, relation)
);

CREATE TABLE IF NOT EXISTS extracted_chunks (
    doc_id          TEXT NOT NULL,
    chunk_index     INTEGER,            -- NULL = 구마커 (doc_id, content_hash로만 기록)
    content_hash    TEXT,
    extracted_at    TEXT,
    PRIMARY KEY (doc_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS concept_mentions (
    concept_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    section_path    TEXT,
    PRIMARY KEY (concept_id, doc_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS concept_curation (
    slug        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,       -- 'real' | 'vocab'
    prose       TEXT,                -- 증류 산문 (real만, [[c:slug|표시명]] 플레이스홀더 링크)
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS graph_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concepts_slug ON concepts(slug);
CREATE INDEX IF NOT EXISTS idx_concepts_category ON concepts(category);
CREATE INDEX IF NOT EXISTS idx_concept_edges_src ON concept_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_concept_edges_dst ON concept_edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edge_evidence_doc ON concept_edge_evidence(doc_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_edge_evidence_src ON concept_edge_evidence(src_id);
CREATE INDEX IF NOT EXISTS idx_edge_evidence_dst ON concept_edge_evidence(dst_id);
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


def _migrate_extracted_chunks(conn: sqlite3.Connection) -> None:
    """구 마커((doc_id, content_hash) PK)를 chunk_index 키 테이블로 이관.

    구 행은 chunk_index=NULL로 남긴다 — 해시만 아는 레거시 마커로서 fallback 매칭에만
    쓰이고, 해당 청크 내용이 바뀌면 자연 소멸한다. 통째로 버리면 이미 구축된 그래프
    전량이 pending으로 돌아가 재추출(사람·LLM 루프)을 강요하게 된다.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(extracted_chunks)")]
    if not cols or "chunk_index" in cols:
        return
    conn.execute("ALTER TABLE extracted_chunks RENAME TO extracted_chunks_old")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO extracted_chunks (doc_id, chunk_index, content_hash, extracted_at) "
        "SELECT doc_id, NULL, content_hash, extracted_at FROM extracted_chunks_old"
    )
    conn.execute("DROP TABLE extracted_chunks_old")


def _cleanup_invalid_slugs(conn: sqlite3.Connection) -> None:
    """정규화 결과가 빈 레거시 개념을 외래키 cascade로 한 번만 제거."""
    key = "invalid_slug_cleanup_v1"
    if conn.execute("SELECT 1 FROM graph_meta WHERE key = ?", (key,)).fetchone():
        return
    conn.execute("DELETE FROM concept_curation WHERE trim(slug) = ''")
    conn.execute("DELETE FROM concepts WHERE trim(slug) = ''")
    conn.execute("INSERT INTO graph_meta (key, value) VALUES (?, 'complete')", (key,))


def init_schema(db_path: str) -> None:
    """스키마 초기화 (존재하지 않는 테이블만 생성)."""
    with get_connection(db_path) as conn:
        _migrate_extracted_chunks(conn)
        conn.executescript(SCHEMA_SQL)
        # 기존 DB 마이그레이션: 컬럼이 이미 있으면 no-op
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE concept_edges ADD COLUMN confidence REAL")
        # 셀프추출 마커로 대체된 미사용 run 인프라 제거. 기존 DB에도 1회 적용된다.
        conn.execute("DROP TABLE IF EXISTS graph_runs")
        _cleanup_invalid_slugs(conn)
        conn.commit()


@contextmanager
def graph_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """스키마 초기화가 보장된 그래프 연결을 트랜잭션 컨텍스트로 제공."""
    init_schema(db_path)
    with get_connection(db_path) as conn:
        yield conn
