"""개념 그래프 CRUD 레이어."""

import contextlib
import re
import sqlite3
import struct
from datetime import UTC, datetime

from pkb.config import settings


def _now() -> str:
    return datetime.now(UTC).isoformat()


def make_slug(name: str) -> str:
    """이름을 slug로 정규화: 소문자 + 공백/특수문자 단순화.

    멱등: 특수문자 제거를 먼저 하고, 그로 인해 생긴 공백을 마지막에 축소·트림한다.
    (반대 순서면 제거 후 생긴 앞/뒤/이중 공백이 남아 make_slug(make_slug(x))!=make_slug(x)가 됨)
    """
    s = name.lower()
    s = re.sub(r"[^\w\s가-힣·\-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------- Concepts ----------

def find_concept_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM concepts WHERE slug = ?", (slug,)
    ).fetchone()


def find_concept_by_alias(conn: sqlite3.Connection, alias_slug: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT c.* FROM concepts c "
        "JOIN concept_aliases a ON a.concept_id = c.id "
        "WHERE a.alias_slug = ?",
        (alias_slug,),
    ).fetchone()
    return row


def find_concept_by_embedding(
    conn: sqlite3.Connection,
    embedding: list[float],
    threshold: float | None = None,
) -> tuple[sqlite3.Row, float] | None:
    """임베딩 유사도가 threshold 이상인 가장 가까운 개념을 반환."""
    if threshold is None:
        threshold = settings.graph_dedup_threshold

    best: tuple[sqlite3.Row, float] | None = None
    for row in conn.execute("SELECT * FROM concepts WHERE embedding IS NOT NULL"):
        other = _unpack_embedding(row["embedding"])
        score = _cosine(embedding, other)
        if score >= threshold and (best is None or score > best[1]):
            best = (row, score)
    return best


def upsert_concept(
    conn: sqlite3.Connection,
    name: str,
    description: str = "",
    category: str | None = None,
    embedding: list[float] | None = None,
) -> int:
    """개념 insert or update. 반환: concept_id.

    정규화 순서: slug 일치 → alias 일치 → 임베딩 유사도.
    """
    slug = make_slug(name)
    now = _now()

    # 1. slug 일치
    row = find_concept_by_slug(conn, slug)
    if row:
        conn.execute(
            "UPDATE concepts SET mention_count = mention_count + 1, updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        return row["id"]

    # 2. alias slug 일치
    row = find_concept_by_alias(conn, slug)
    if row:
        conn.execute(
            "UPDATE concepts SET mention_count = mention_count + 1, updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        return row["id"]

    # 3. 임베딩 유사도
    if embedding:
        match = find_concept_by_embedding(conn, embedding)
        if match:
            existing = match[0]
            # 새 이름을 alias로 추가
            with contextlib.suppress(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO concept_aliases (concept_id, alias, alias_slug) VALUES (?, ?, ?)",
                    (existing["id"], name, slug),
                )
            conn.execute(
                "UPDATE concepts SET mention_count = mention_count + 1, updated_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
            return existing["id"]

    # 4. 신규 insert
    blob = _pack_embedding(embedding) if embedding else None
    cur = conn.execute(
        "INSERT INTO concepts "
        "(name, slug, category, description, embedding, mention_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (name, slug, category, description, blob, now, now),
    )
    return cur.lastrowid


def add_alias(conn: sqlite3.Connection, concept_id: int, alias: str) -> None:
    alias_slug = make_slug(alias)
    with contextlib.suppress(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO concept_aliases (concept_id, alias, alias_slug) VALUES (?, ?, ?)",
            (concept_id, alias, alias_slug),
        )


def list_aliases(conn: sqlite3.Connection, concept_id: int) -> list[str]:
    """개념의 별칭 목록."""
    rows = conn.execute(
        "SELECT alias FROM concept_aliases WHERE concept_id = ? ORDER BY alias_slug",
        (concept_id,),
    ).fetchall()
    return [r["alias"] for r in rows]


# ---------- Documents ----------

def upsert_document(
    conn: sqlite3.Connection, doc_id: str, title: str | None, category: str | None
) -> int:
    row = conn.execute("SELECT id FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO documents (doc_id, title, category) VALUES (?, ?, ?)",
        (doc_id, title, category),
    )
    return cur.lastrowid


def get_document_title(conn: sqlite3.Connection, doc_id: str) -> str | None:
    """doc_id로 문서 제목 조회."""
    row = conn.execute(
        "SELECT title FROM documents WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    return row["title"] if row else None


def purge_document(conn: sqlite3.Connection, doc_id: str) -> dict:
    """단일 doc_id의 concept_mentions·documents 행 삭제 (ES 문서 삭제 후 그래프 정리용).

    반환: {"mentions_pruned": n, "documents_pruned": m}
    """
    m = conn.execute("DELETE FROM concept_mentions WHERE doc_id = ?", (doc_id,)).rowcount
    d = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,)).rowcount
    return {"mentions_pruned": m, "documents_pruned": d}


def prune_missing_documents(conn: sqlite3.Connection, existing_doc_ids: set[str]) -> dict:
    """코퍼스(ES)에 더 이상 없는 문서의 concept_mentions·documents 행을 정리한다.

    원본 파일이 삭제/이동됐지만 그래프에 언급·문서 레코드가 남아있는 dangling
    mention을 없앤다. 반환: {"mentions_pruned": n, "documents_pruned": m}
    """
    doc_ids = {r["doc_id"] for r in conn.execute("SELECT DISTINCT doc_id FROM concept_mentions")}
    doc_ids |= {r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents")}
    stale = doc_ids - existing_doc_ids

    mentions_pruned = documents_pruned = 0
    for doc_id in stale:
        result = purge_document(conn, doc_id)
        mentions_pruned += result["mentions_pruned"]
        documents_pruned += result["documents_pruned"]
    return {"mentions_pruned": mentions_pruned, "documents_pruned": documents_pruned}


# ---------- Edges ----------

def add_edge(
    conn: sqlite3.Connection,
    src_id: int,
    dst_id: int,
    relation: str,
) -> None:
    """동일 (src, dst, relation) 재호출 시 weight/evidence_count 누적."""
    if src_id == dst_id:
        return
    row = conn.execute(
        "SELECT weight, evidence_count FROM concept_edges "
        "WHERE src_id = ? AND dst_id = ? AND relation = ?",
        (src_id, dst_id, relation),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE concept_edges SET weight = weight + 1.0, evidence_count = evidence_count + 1 "
            "WHERE src_id = ? AND dst_id = ? AND relation = ?",
            (src_id, dst_id, relation),
        )
    else:
        conn.execute(
            "INSERT INTO concept_edges (src_id, dst_id, relation, weight, evidence_count) "
            "VALUES (?, ?, ?, 1.0, 1)",
            (src_id, dst_id, relation),
        )


# ---------- Mentions ----------

def add_mention(
    conn: sqlite3.Connection,
    concept_id: int,
    doc_id: str,
    chunk_index: int,
    section_path: str = "",
) -> None:
    with contextlib.suppress(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO concept_mentions (concept_id, doc_id, chunk_index, section_path) "
            "VALUES (?, ?, ?, ?)",
            (concept_id, doc_id, chunk_index, section_path),
        )


# ---------- Queries ----------

def get_concept(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    """이름/slug/alias로 개념 조회."""
    slug = make_slug(identifier)
    row = find_concept_by_slug(conn, slug)
    if row:
        return row
    return find_concept_by_alias(conn, slug)


def list_concepts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """전체 개념 목록 (slug 순 정렬)."""
    return list(conn.execute("SELECT * FROM concepts ORDER BY slug").fetchall())


def list_edges(
    conn: sqlite3.Connection, concept_id: int, relation: str | None = None
) -> list[sqlite3.Row]:
    """Outbound 엣지 (concept_id가 src인 관계)."""
    if relation:
        rows = conn.execute(
            "SELECT e.*, c.name as dst_name, c.slug as dst_slug "
            "FROM concept_edges e JOIN concepts c ON c.id = e.dst_id "
            "WHERE e.src_id = ? AND e.relation = ? "
            "ORDER BY e.weight DESC",
            (concept_id, relation),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT e.*, c.name as dst_name, c.slug as dst_slug "
            "FROM concept_edges e JOIN concepts c ON c.id = e.dst_id "
            "WHERE e.src_id = ? "
            "ORDER BY e.weight DESC",
            (concept_id,),
        ).fetchall()
    return list(rows)


def list_mentions(conn: sqlite3.Connection, concept_id: int, limit: int = 20) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM concept_mentions WHERE concept_id = ? LIMIT ?",
        (concept_id, limit),
    ).fetchall()
    return list(rows)


def stats(conn: sqlite3.Connection) -> dict:
    return {
        "concepts": conn.execute("SELECT COUNT(*) AS c FROM concepts").fetchone()["c"],
        "edges": conn.execute("SELECT COUNT(*) AS c FROM concept_edges").fetchone()["c"],
        "mentions": conn.execute("SELECT COUNT(*) AS c FROM concept_mentions").fetchone()["c"],
        "documents": conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"],
        "aliases": conn.execute("SELECT COUNT(*) AS c FROM concept_aliases").fetchone()["c"],
        "runs": conn.execute("SELECT COUNT(*) AS c FROM graph_runs").fetchone()["c"],
    }


# ---------- Curation ----------

def set_curation(
    conn: sqlite3.Connection, slug: str, label: str, prose: str | None = None
) -> None:
    """개념 큐레이션(real/vocab) 지정. prose=None이면 기존 prose 보존."""
    conn.execute(
        "INSERT INTO concept_curation (slug, label, prose, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "label = excluded.label, "
        "prose = COALESCE(excluded.prose, concept_curation.prose), "
        "updated_at = excluded.updated_at",
        (slug, label, prose, _now()),
    )


def projected_slugs(conn: sqlite3.Connection) -> set[str] | None:
    """label='real'인 slug 집합. 큐레이션 테이블이 비어있으면 None (v1 전량 투영)."""
    if conn.execute("SELECT 1 FROM concept_curation LIMIT 1").fetchone() is None:
        return None
    rows = conn.execute("SELECT slug FROM concept_curation WHERE label = 'real'").fetchall()
    return {r["slug"] for r in rows}


def get_prose(conn: sqlite3.Connection, slug: str) -> str | None:
    row = conn.execute(
        "SELECT prose FROM concept_curation WHERE slug = ?", (slug,)
    ).fetchone()
    return row["prose"] if row else None


# ---------- Runs ----------

def start_run(
    conn: sqlite3.Connection, scope_category: str = "", scope_doc_id: str = "", model: str = ""
) -> int:
    cur = conn.execute(
        "INSERT INTO graph_runs (started_at, scope_category, scope_doc_id, model, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (_now(), scope_category or None, scope_doc_id or None, model),
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    chunks_processed: int,
    concepts_added: int,
    edges_added: int,
    status: str = "success",
) -> None:
    conn.execute(
        "UPDATE graph_runs SET finished_at = ?, chunks_processed = ?, concepts_added = ?, "
        "edges_added = ?, status = ? WHERE id = ?",
        (_now(), chunks_processed, concepts_added, edges_added, status, run_id),
    )
