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


def _namespace_slug(namespace: str | None) -> str:
    """Return the normalized identity namespace (empty = legacy/global)."""
    if namespace is None:
        return ""
    return make_slug(str(namespace))


def _effective_namespace(
    namespace: str | None,
    category: str | None,
) -> str:
    """Resolve the optional identity namespace without changing old callers.

    ``namespace`` is explicit for callers that have a domain namespace.  The
    ingestion path historically only supplied ``category``, so category is a
    useful fallback for new writes.  A missing value remains the global legacy
    scope and therefore keeps existing slug/alias behavior.
    """
    return _namespace_slug(namespace if namespace is not None else category)


def _row_namespace(row: sqlite3.Row) -> str:
    """Read namespace from both migrated and pre-migration rows."""
    keys = row.keys()
    explicit = row["namespace"] if "namespace" in keys else ""
    if explicit:
        return _namespace_slug(explicit)
    # Before namespace migration category was the only useful scope hint.
    # Treating a categorized legacy row as scoped prevents a new, unrelated
    # category from silently reusing its slug; uncategorized rows stay global.
    category = row["category"] if "category" in keys else None
    return _namespace_slug(category)


def _scope_matches(row: sqlite3.Row, namespace: str) -> bool:
    """Whether an existing row may satisfy a scoped identity lookup.

    An unscoped caller is intentionally a wildcard for backwards compatibility
    with ``get_concept(name)`` and old graph tools.  Scoped callers match the
    exact namespace, or a truly global (uncategorized) legacy row.
    """
    if not namespace:
        return True
    return not _row_namespace(row) or _row_namespace(row) == namespace


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

def find_concept_by_slug(
    conn: sqlite3.Connection,
    slug: str,
    *,
    namespace: str | None = None,
    category: str | None = None,
) -> sqlite3.Row | None:
    """Find one physical slug, optionally constrained to a namespace.

    ``slug`` remains the stable public identifier for legacy callers.  Scoped
    writes may allocate a physical ``<base>--<namespace>`` slug, so callers
    that have category context should pass it to avoid selecting a conflicting
    legacy row.
    """
    identity_namespace = _effective_namespace(namespace, category)
    rows = conn.execute(
        "SELECT * FROM concepts WHERE slug = ?", (slug,)
    ).fetchall()
    if not identity_namespace:
        return rows[0] if rows else None
    matches = [row for row in rows if _scope_matches(row, identity_namespace)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    # The requested concept may have received a scoped physical slug because
    # another namespace already owned the base slug.  Treat this helper as a
    # namespace-aware identity lookup as well, while retaining exact-slug
    # behavior for unqualified callers.
    candidates = conn.execute(
        "SELECT * FROM concepts WHERE base_slug = ? ORDER BY id", (slug,)
    ).fetchall()
    candidates = [row for row in candidates if _scope_matches(row, identity_namespace)]
    return candidates[0] if len(candidates) == 1 else None


def find_concepts_by_base_slug(
    conn: sqlite3.Connection,
    base_slug: str,
    *,
    namespace: str | None = None,
    category: str | None = None,
) -> list[sqlite3.Row]:
    """Return all concepts sharing a normalized name, scope-aware."""
    identity_namespace = _effective_namespace(namespace, category)
    rows = conn.execute(
        "SELECT * FROM concepts WHERE base_slug = ? OR (base_slug = '' AND slug = ?) "
        "ORDER BY id",
        (base_slug, base_slug),
    ).fetchall()
    if identity_namespace:
        rows = [row for row in rows if _scope_matches(row, identity_namespace)]
    return list(rows)


def find_concepts_by_alias(
    conn: sqlite3.Connection,
    alias_slug: str,
    *,
    namespace: str | None = None,
    category: str | None = None,
) -> list[sqlite3.Row]:
    """Return every concept using an alias, never silently choosing a row."""
    alias_slug = make_slug(alias_slug)
    if not alias_slug:
        return []
    identity_namespace = _effective_namespace(namespace, category)
    rows = conn.execute(
        "SELECT c.* FROM concepts c "
        "JOIN concept_aliases a ON a.concept_id = c.id "
        "WHERE a.alias_slug = ? ORDER BY c.id",
        (alias_slug,),
    ).fetchall()
    if identity_namespace:
        rows = [row for row in rows if _scope_matches(row, identity_namespace)]
    return list(rows)


def find_concept_by_alias(
    conn: sqlite3.Connection,
    alias_slug: str,
    *,
    namespace: str | None = None,
    category: str | None = None,
) -> sqlite3.Row | None:
    """Resolve an alias only when it is unambiguous in the requested scope."""
    rows = find_concepts_by_alias(
        conn, alias_slug, namespace=namespace, category=category
    )
    if len(rows) != 1:
        return None
    # A legacy alias may have been written before another concept claimed the
    # same normalized name.  Do not let the alias path bypass the canonical
    # ambiguity guard; scoped callers can still resolve their own concept.
    canonical = find_concepts_by_base_slug(
        conn,
        make_slug(alias_slug),
        namespace=namespace,
        category=category,
    )
    if any(candidate["id"] != rows[0]["id"] for candidate in canonical):
        return None
    return rows[0]


def find_concept_by_embedding(
    conn: sqlite3.Connection,
    embedding: list[float],
    threshold: float | None = None,
    *,
    namespace: str | None = None,
    category: str | None = None,
) -> tuple[sqlite3.Row, float] | None:
    """임베딩 유사도가 threshold 이상인 가장 가까운 개념을 반환."""
    if threshold is None:
        threshold = settings.graph_dedup_threshold

    identity_namespace = _effective_namespace(namespace, category)
    best: tuple[sqlite3.Row, float] | None = None
    for row in conn.execute("SELECT * FROM concepts WHERE embedding IS NOT NULL"):
        if identity_namespace and not _scope_matches(row, identity_namespace):
            continue
        other = _unpack_embedding(row["embedding"])
        score = _cosine(embedding, other)
        if score >= threshold and (best is None or score > best[1]):
            best = (row, score)
    return best


def top_concepts_by_embedding(
    conn: sqlite3.Connection,
    vec: list[float],
    k: int = 5,
    threshold: float = 0.4,
) -> list[tuple[sqlite3.Row, float]]:
    """임베딩 유사도가 threshold 이상인 상위 k개 개념을 (row, score)로 반환.

    검색 쿼리를 코퍼스 개념 어휘로 바꿔 재질의하는 시드용.
    """
    # ponytail: 파이썬 코사인 풀스캔 — 개인 규모 N이면 충분
    scored: list[tuple[sqlite3.Row, float]] = []
    for row in conn.execute("SELECT * FROM concepts WHERE embedding IS NOT NULL"):
        score = _cosine(vec, _unpack_embedding(row["embedding"]))
        if score >= threshold:
            scored.append((row, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def upsert_concept(
    conn: sqlite3.Connection,
    name: str,
    description: str = "",
    category: str | None = None,
    embedding: list[float] | None = None,
    *,
    namespace: str | None = None,
    match_by_alias: bool = True,
    match_by_embedding: bool = True,
) -> int:
    """개념 insert or update. 반환: concept_id.

    정규화 순서: slug 일치 → alias 일치 → 임베딩 유사도.
    mention_count는 여기서 올리지 않는다 — concept_mentions에서 유도(recompute_mention_counts).
    호출마다 +1 하면 같은 청크 재추출이 카운트를 부풀린다.
    """
    name = name.strip()
    base_slug = make_slug(name)
    if not base_slug:
        raise ValueError("정규화 후 빈 slug가 되는 개념명은 저장할 수 없습니다.")
    identity_namespace = _effective_namespace(namespace, category)
    now = _now()

    # 1. slug 일치.  A category/namespace mismatch is a real identity
    # conflict, not a spelling variant; in that case we continue below and
    # allocate a deterministic scoped physical slug.
    row = find_concept_by_slug(conn, base_slug)
    if row is not None and not _scope_matches(row, identity_namespace):
        row = None
    if row is None:
        scoped_rows = find_concepts_by_base_slug(
            conn, base_slug, namespace=identity_namespace or None
        )
        if len(scoped_rows) == 1:
            row = scoped_rows[0]
    # 2. alias slug 일치 (ambiguous aliases deliberately return no row)
    if row is None and match_by_alias:
        row = find_concept_by_alias(
            conn, base_slug, namespace=identity_namespace or None
        )
    if row:
        # 빈 description 채움: 설명 없는 기존 개념에 비어있지 않은 새 설명이 오면
        # description·embedding을 함께 채운다 (비어있지 않은 기존 설명은 보존 — 파괴적 덮어쓰기 없음).
        if description and not row["description"]:
            blob = _pack_embedding(embedding) if embedding else row["embedding"]
            conn.execute(
                "UPDATE concepts SET description = ?, embedding = ? WHERE id = ?",
                (description, blob, row["id"]),
            )
        conn.execute(
            "UPDATE concepts SET updated_at = ? WHERE id = ?", (now, row["id"])
        )
        return row["id"]

    # 3. 임베딩 유사도. 대량 자동 추출에서는 약어·동음어 오병합을 피하려고
    # 호출자가 끌 수 있다(DP→Data Parallelism 같은 실제 오병합 방지).
    if embedding and match_by_embedding:
        match = find_concept_by_embedding(
            conn, embedding, namespace=identity_namespace or None
        )
        if match:
            existing = match[0]
            # 새 이름을 alias로 추가
            add_alias(conn, existing["id"], name, _allow_conflict=False)
            conn.execute(
                "UPDATE concepts SET updated_at = ? WHERE id = ?", (now, existing["id"])
            )
            return existing["id"]

    # 4. 신규 insert.  ``slug`` is still globally unique for old consumers;
    # only a scoped collision gets a suffix.  The base name remains searchable
    # through ``base_slug`` and the namespace column.
    slug = base_slug
    if conn.execute("SELECT 1 FROM concepts WHERE slug = ?", (slug,)).fetchone():
        if identity_namespace:
            scope_slug = _namespace_slug(identity_namespace)
            slug = f"{base_slug}--{scope_slug}"
            suffix = 2
            while conn.execute(
                "SELECT 1 FROM concepts WHERE slug = ?", (slug,)
            ).fetchone():
                slug = f"{base_slug}--{scope_slug}-{suffix}"
                suffix += 1
        else:
            # A no-context caller is allowed to retain the pre-v2 global
            # behavior.  This branch is reachable only when an existing row is
            # scoped and was intentionally bypassed (e.g. match_by_alias=False).
            slug = f"{base_slug}--global"
            suffix = 2
            while conn.execute(
                "SELECT 1 FROM concepts WHERE slug = ?", (slug,)
            ).fetchone():
                slug = f"{base_slug}--global-{suffix}"
                suffix += 1
    blob = _pack_embedding(embedding) if embedding else None
    cur = conn.execute(
        "INSERT INTO concepts "
        "(name, slug, namespace, base_slug, category, description, embedding, mention_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (name, slug, identity_namespace, base_slug, category, description, blob, now, now),
    )
    return cur.lastrowid


def _record_alias_conflict(
    conn: sqlite3.Connection,
    *,
    alias: str,
    alias_slug: str,
    concept_id: int,
    existing_concept_id: int | None,
    reason: str,
) -> None:
    """Persist one rejected alias write without modifying either concept."""
    conn.execute(
        "INSERT OR IGNORE INTO concept_alias_conflicts "
        "(alias, alias_slug, concept_id, existing_concept_id, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (alias, alias_slug, concept_id, existing_concept_id, reason, _now()),
    )


def alias_conflicts(
    conn: sqlite3.Connection,
    alias: str | None = None,
) -> list[dict]:
    """Report both rejected writes and legacy aliases that resolve ambiguously."""
    alias_slug = make_slug(alias) if alias else None
    params: tuple[object, ...] = (alias_slug,) if alias_slug else ()
    where = "WHERE alias_slug = ?" if alias_slug else ""
    rejected = conn.execute(
        "SELECT alias, alias_slug, concept_id, existing_concept_id, reason, created_at "
        "FROM concept_alias_conflicts "
        f"{where} ORDER BY alias_slug, id",
        params,
    ).fetchall()
    result = [dict(row) for row in rejected]

    # Legacy data may contain duplicate aliases because v1 had only a
    # per-concept primary key.  Surface those collisions without rewriting the
    # rows; callers can then curate/merge deliberately.
    ambiguous = conn.execute(
        "SELECT a.alias, a.alias_slug, GROUP_CONCAT(DISTINCT a.concept_id) AS concept_ids "
        "FROM concept_aliases a "
        f"{where} "
        "GROUP BY a.alias_slug HAVING COUNT(DISTINCT a.concept_id) > 1 "
        "ORDER BY a.alias_slug",
        params,
    ).fetchall()
    seen = {(item["alias_slug"], item["concept_id"], item["existing_concept_id"]) for item in result}
    for row in ambiguous:
        concept_ids = [int(value) for value in row["concept_ids"].split(",")]
        # A stable pairwise report makes the ambiguity actionable while keeping
        # the original alias rows untouched.
        for concept_id in concept_ids[1:]:
            key = (row["alias_slug"], concept_ids[0], concept_id)
            if key in seen:
                continue
            result.append(
                {
                    "alias": row["alias"],
                    "alias_slug": row["alias_slug"],
                    "concept_id": concept_ids[0],
                    "existing_concept_id": concept_id,
                    "reason": "ambiguous_legacy_alias",
                    "created_at": None,
                }
            )
            seen.add(key)

    canonical_collisions = conn.execute(
        "SELECT a.alias, a.alias_slug, a.concept_id, c.id AS existing_concept_id "
        "FROM concept_aliases a JOIN concepts c "
        "ON c.id != a.concept_id AND (c.base_slug = a.alias_slug OR c.slug = a.alias_slug) "
        f"{where} ORDER BY a.alias_slug, a.concept_id, c.id",
        params,
    ).fetchall()
    for row in canonical_collisions:
        key = (row["alias_slug"], row["concept_id"], row["existing_concept_id"])
        if key in seen:
            continue
        result.append(
            {
                "alias": row["alias"],
                "alias_slug": row["alias_slug"],
                "concept_id": row["concept_id"],
                "existing_concept_id": row["existing_concept_id"],
                "reason": "canonical_or_alias_collision",
                "created_at": None,
            }
        )
        seen.add(key)
    return result


# More discoverable alias for callers that prefer a verb phrase.
find_alias_conflicts = alias_conflicts


def add_alias(
    conn: sqlite3.Connection,
    concept_id: int,
    alias: str,
    *,
    _allow_conflict: bool = False,
) -> bool:
    """Add an alias, rejecting cross-concept/canonical collisions.

    Returns ``True`` when the alias is accepted (inserted or already present)
    and ``False`` when it is rejected.  The return value is additive; existing
    callers ignored the old ``None`` result.
    """
    alias = alias.strip()
    alias_slug = make_slug(alias)
    if not alias_slug:
        return False
    owner = conn.execute(
        "SELECT * FROM concepts WHERE id = ?", (concept_id,)
    ).fetchone()
    if owner is None:
        raise ValueError(f"개념을 찾을 수 없습니다: {concept_id}")
    # Idempotent re-extraction of the same alias is not a conflict.  Return
    # True so service-level reporting only mentions rejected cross-concept
    # writes.
    if conn.execute(
        "SELECT 1 FROM concept_aliases WHERE concept_id = ? AND alias_slug = ?",
        (concept_id, alias_slug),
    ).fetchone():
        return True

    if not _allow_conflict:
        # Aliases must not shadow another concept's canonical name or alias.
        # Even if their namespaces differ, an unqualified lookup would be
        # ambiguous; scoped lookups can still use each canonical concept name.
        canonical_rows = conn.execute(
            "SELECT * FROM concepts WHERE id != ? AND "
            "(slug = ? OR base_slug = ?)",
            (concept_id, alias_slug, alias_slug),
        ).fetchall()
        alias_rows = conn.execute(
            "SELECT c.* FROM concepts c JOIN concept_aliases a ON a.concept_id = c.id "
            "WHERE c.id != ? AND a.alias_slug = ?",
            (concept_id, alias_slug),
        ).fetchall()
        conflicts = {row["id"]: row for row in [*canonical_rows, *alias_rows]}
        if conflicts:
            for existing in conflicts.values():
                _record_alias_conflict(
                    conn,
                    alias=alias,
                    alias_slug=alias_slug,
                    concept_id=concept_id,
                    existing_concept_id=existing["id"],
                    reason="canonical_or_alias_collision",
                )
            return False

    try:
        conn.execute(
            "INSERT INTO concept_aliases (concept_id, alias, alias_slug) VALUES (?, ?, ?)",
            (concept_id, alias, alias_slug),
        )
    except sqlite3.IntegrityError:
        return False
    return True


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
    """단일 doc_id의 concept_mentions·documents·extracted_chunks 행 삭제 (ES 문서 삭제 후 그래프 정리용).

    추출 마커(extracted_chunks)도 함께 지운다 — 마커가 남으면 삭제 후 동일 내용이
    재생성됐을 때 pending에서 빠져 멘션이 영구 결손된다.
    반환: {"mentions_pruned": n, "documents_pruned": m}
    """
    touched = {
        row["concept_id"]
        for row in conn.execute(
            "SELECT DISTINCT concept_id FROM concept_mentions WHERE doc_id = ?", (doc_id,)
        )
    }
    clear_edge_evidence_for_document(conn, doc_id)
    m = conn.execute("DELETE FROM concept_mentions WHERE doc_id = ?", (doc_id,)).rowcount
    d = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,)).rowcount
    conn.execute("DELETE FROM extracted_chunks WHERE doc_id = ?", (doc_id,))
    recompute_mention_counts(conn, touched)
    return {"mentions_pruned": m, "documents_pruned": d}


def rename_document(conn: sqlite3.Connection, old_doc_id: str, new_doc_id: str) -> dict:
    """Move graph provenance to a new physical document ID.

    Curated file moves should preserve mentions, extraction markers, and edge
    evidence instead of pruning and re-extracting unchanged content.  The
    caller owns the transaction; any target collision fails before updates.
    """
    old_doc_id = old_doc_id.strip()
    new_doc_id = new_doc_id.strip()
    if not old_doc_id or not new_doc_id:
        raise ValueError("old_doc_id와 new_doc_id는 비어 있을 수 없습니다")
    tables = (
        "documents",
        "concept_mentions",
        "concept_edge_evidence",
        "extracted_chunks",
    )
    empty = {table: 0 for table in tables}
    if old_doc_id == new_doc_id:
        return empty

    source_counts = {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE doc_id = ?",
            (old_doc_id,),
        ).fetchone()[0]
        for table in tables
    }
    if not any(source_counts.values()):
        return empty
    target_counts = {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE doc_id = ?",
            (new_doc_id,),
        ).fetchone()[0]
        for table in tables
    }
    collisions = [table for table, count in target_counts.items() if count]
    if collisions:
        raise ValueError(
            f"그래프 문서 이동 대상이 이미 존재합니다: {new_doc_id} "
            f"({', '.join(collisions)})"
        )

    updated: dict[str, int] = {}
    for table in tables:
        cursor = conn.execute(
            f"UPDATE {table} SET doc_id = ? WHERE doc_id = ?",
            (new_doc_id, old_doc_id),
        )
        updated[table] = cursor.rowcount
    return updated


def prune_missing_documents(conn: sqlite3.Connection, existing_doc_ids: set[str]) -> dict:
    """코퍼스(ES)에 더 이상 없는 문서의 concept_mentions·documents 행을 정리한다.

    원본 파일이 삭제/이동됐지만 그래프에 언급·문서 레코드가 남아있는 dangling
    mention을 없앤다. 반환: {"mentions_pruned": n, "documents_pruned": m}
    """
    doc_ids = {r["doc_id"] for r in conn.execute("SELECT DISTINCT doc_id FROM concept_mentions")}
    doc_ids |= {
        r["doc_id"] for r in conn.execute("SELECT DISTINCT doc_id FROM concept_edge_evidence")
    }
    doc_ids |= {r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents")}
    stale = doc_ids - existing_doc_ids

    mentions_pruned = documents_pruned = 0
    for doc_id in stale:
        result = purge_document(conn, doc_id)  # 추출 마커도 함께 정리 — 이동 문서는 자동 재추출 대상
        mentions_pruned += result["mentions_pruned"]
        documents_pruned += result["documents_pruned"]
    return {"mentions_pruned": mentions_pruned, "documents_pruned": documents_pruned}


# ---------- Edges ----------

def add_edge(
    conn: sqlite3.Connection,
    src_id: int,
    dst_id: int,
    relation: str,
    confidence: float | None = None,
    *,
    doc_id: str | None = None,
    chunk_index: int | None = None,
    materialize: bool | None = None,
) -> None:
    """관계를 저장한다.

    doc_id/chunk_index가 있으면 청크별 evidence를 멱등 upsert하고 materialized edge 집계를
    evidence에서 다시 계산한다. 둘 다 없으면 구데이터·테스트용 append-only 호환 경로다.
    """
    if src_id == dst_id:
        return
    if (doc_id is None) != (chunk_index is None):
        raise ValueError("doc_id와 chunk_index는 함께 지정해야 합니다.")
    if doc_id is not None and chunk_index is not None:
        conn.execute(
            "INSERT INTO concept_edge_evidence "
            "(doc_id, chunk_index, src_id, dst_id, relation, confidence) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(doc_id, chunk_index, src_id, dst_id, relation) DO UPDATE SET "
            "confidence = CASE "
            "WHEN excluded.confidence IS NULL THEN concept_edge_evidence.confidence "
            "WHEN concept_edge_evidence.confidence IS NULL THEN excluded.confidence "
            "ELSE MAX(concept_edge_evidence.confidence, excluded.confidence) END",
            (doc_id, chunk_index, src_id, dst_id, relation, confidence),
        )
        if materialize is None:
            materialize = not edge_evidence_rebuild_active(conn)
        if materialize:
            _recompute_edge(conn, src_id, dst_id, relation)
        return

    # provenance가 없던 기존 호출의 하위호환 경로.
    row = conn.execute(
        "SELECT weight, evidence_count FROM concept_edges "
        "WHERE src_id = ? AND dst_id = ? AND relation = ?",
        (src_id, dst_id, relation),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE concept_edges SET weight = weight + 1.0, evidence_count = evidence_count + 1, "
            "confidence = CASE WHEN ? IS NULL THEN confidence "
            "ELSE MAX(COALESCE(confidence, 0), ?) END "
            "WHERE src_id = ? AND dst_id = ? AND relation = ?",
            (confidence, confidence, src_id, dst_id, relation),
        )
    else:
        conn.execute(
            "INSERT INTO concept_edges (src_id, dst_id, relation, weight, evidence_count, confidence) "
            "VALUES (?, ?, ?, 1.0, 1, ?)",
            (src_id, dst_id, relation, confidence),
        )


def _recompute_edge(
    conn: sqlite3.Connection, src_id: int, dst_id: int, relation: str
) -> None:
    """한 materialized edge를 evidence 실측치로 갱신하거나 evidence가 없으면 삭제."""
    aggregate = conn.execute(
        "SELECT COUNT(*) AS n, MAX(confidence) AS confidence "
        "FROM concept_edge_evidence WHERE src_id = ? AND dst_id = ? AND relation = ?",
        (src_id, dst_id, relation),
    ).fetchone()
    count = aggregate["n"]
    if count == 0:
        conn.execute(
            "DELETE FROM concept_edges WHERE src_id = ? AND dst_id = ? AND relation = ?",
            (src_id, dst_id, relation),
        )
        return
    conn.execute(
        "INSERT INTO concept_edges (src_id, dst_id, relation, weight, evidence_count, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(src_id, dst_id, relation) DO UPDATE SET "
        "weight = excluded.weight, evidence_count = excluded.evidence_count, "
        "confidence = excluded.confidence",
        (src_id, dst_id, relation, float(count), count, aggregate["confidence"]),
    )


def clear_edge_evidence_for_chunk(
    conn: sqlite3.Connection, doc_id: str, chunk_index: int
) -> int:
    """청크의 관계 근거를 삭제하고 영향받은 materialized edge를 재계산."""
    rows = conn.execute(
        "SELECT src_id, dst_id, relation FROM concept_edge_evidence "
        "WHERE doc_id = ? AND chunk_index = ?",
        (doc_id, chunk_index),
    ).fetchall()
    conn.execute(
        "DELETE FROM concept_edge_evidence WHERE doc_id = ? AND chunk_index = ?",
        (doc_id, chunk_index),
    )
    if not edge_evidence_rebuild_active(conn):
        for row in rows:
            _recompute_edge(conn, row["src_id"], row["dst_id"], row["relation"])
    return len(rows)


def clear_edge_evidence_for_document(conn: sqlite3.Connection, doc_id: str) -> int:
    """문서의 관계 근거를 삭제하고 영향받은 materialized edge를 재계산."""
    rows = conn.execute(
        "SELECT DISTINCT src_id, dst_id, relation FROM concept_edge_evidence WHERE doc_id = ?",
        (doc_id,),
    ).fetchall()
    count = conn.execute(
        "DELETE FROM concept_edge_evidence WHERE doc_id = ?", (doc_id,)
    ).rowcount
    if not edge_evidence_rebuild_active(conn):
        for row in rows:
            _recompute_edge(conn, row["src_id"], row["dst_id"], row["relation"])
    return count


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


def clear_mentions_for_chunk(
    conn: sqlite3.Connection, doc_id: str, chunk_index: int
) -> list[int]:
    """한 청크의 멘션을 전부 삭제하고 영향받은 concept_id를 반환.

    청크 재추출은 append가 아니라 교체다 — 지우지 않으면 개정된 청크에서 사라진
    개념의 멘션이 영구히 남는다. 삭제된 concept_id는 카운트 재계산 대상.
    """
    ids = [
        r["concept_id"]
        for r in conn.execute(
            "SELECT concept_id FROM concept_mentions WHERE doc_id = ? AND chunk_index = ?",
            (doc_id, chunk_index),
        )
    ]
    conn.execute(
        "DELETE FROM concept_mentions WHERE doc_id = ? AND chunk_index = ?",
        (doc_id, chunk_index),
    )
    return ids


def realign_doc_chunks(
    conn: sqlite3.Connection,
    doc_id: str,
    old_hashes: dict[int, str],
    new_hashes: dict[int, str],
) -> dict:
    """재청킹으로 슬롯이 바뀐 문서의 멘션·마커를 새 레이아웃에 맞춘다.

    인제스트가 청크를 교체할 때 호출하지 않으면 멘션이 옛 인덱스에 그대로 남아
    검색 결과에 엉뚱한 개념이 붙는다. is_pending은 이동한 청크를 재추출 대상으로
    보지만, 내용이 그대로면 해시가 같아 레거시 마커에 걸려 pending에서 빠진다 —
    그래서 위치 보정은 인제스트가 직접 해야 한다(옛 레이아웃을 아는 유일한 지점).

      - 같은 내용이 다른 슬롯으로 이동 → 멘션·마커를 새 인덱스로 이설(재추출 불필요)
      - 내용이 사라짐 → 멘션·마커 삭제. 그 자리의 새 내용은 해시가 달라 pending이 된다.
    """
    dst_by_hash: dict[str, int] = {}
    for idx in sorted(new_hashes):
        dst_by_hash.setdefault(new_hashes[idx], idx)

    moved: dict[int, int] = {}
    gone: list[int] = []
    for idx, h in old_hashes.items():
        if new_hashes.get(idx) == h:
            continue  # 같은 자리에 같은 내용 — 멘션 유효
        dst = dst_by_hash.get(h)
        if dst is None:
            gone.append(idx)
        else:
            moved[idx] = dst
    if not moved and not gone:
        return {"moved": 0, "dropped": 0}

    rows = conn.execute(
        "SELECT concept_id, chunk_index, section_path FROM concept_mentions WHERE doc_id = ?",
        (doc_id,),
    ).fetchall()
    touched = {r["concept_id"] for r in rows if r["chunk_index"] in moved or r["chunk_index"] in gone}
    # 이동 슬롯끼리 목적지가 겹칠 수 있어(같은 내용의 청크) set으로 중복 제거 후 재삽입
    kept = {
        (r["concept_id"], moved.get(r["chunk_index"], r["chunk_index"]), r["section_path"])
        for r in rows
        if r["chunk_index"] not in gone
    }
    dropped = len(rows) - len({(c, i) for c, i, _ in kept})
    conn.execute("DELETE FROM concept_mentions WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO concept_mentions "
        "(concept_id, doc_id, chunk_index, section_path) VALUES (?, ?, ?, ?)",
        [(cid, doc_id, idx, sp) for cid, idx, sp in kept],
    )

    # 관계 evidence도 동일한 청크 이동 규칙을 따른다. materialized edge는 이동 후 실측 재계산.
    evidence_rows = conn.execute(
        "SELECT chunk_index, src_id, dst_id, relation, confidence "
        "FROM concept_edge_evidence WHERE doc_id = ?",
        (doc_id,),
    ).fetchall()
    affected_edges = {
        (row["src_id"], row["dst_id"], row["relation"]) for row in evidence_rows
    }
    conn.execute("DELETE FROM concept_edge_evidence WHERE doc_id = ?", (doc_id,))
    for row in evidence_rows:
        if row["chunk_index"] in gone:
            continue
        new_index = moved.get(row["chunk_index"], row["chunk_index"])
        conn.execute(
            "INSERT INTO concept_edge_evidence "
            "(doc_id, chunk_index, src_id, dst_id, relation, confidence) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(doc_id, chunk_index, src_id, dst_id, relation) DO UPDATE SET "
            "confidence = CASE "
            "WHEN excluded.confidence IS NULL THEN concept_edge_evidence.confidence "
            "WHEN concept_edge_evidence.confidence IS NULL THEN excluded.confidence "
            "ELSE MAX(concept_edge_evidence.confidence, excluded.confidence) END",
            (
                doc_id,
                new_index,
                row["src_id"],
                row["dst_id"],
                row["relation"],
                row["confidence"],
            ),
        )
    if not edge_evidence_rebuild_active(conn):
        for src_id, dst_id, relation in affected_edges:
            _recompute_edge(conn, src_id, dst_id, relation)

    # 마커도 전량 재작성 — 제자리 UPDATE는 슬롯이 밀릴 때 서로 덮어쓴다(0→1이 기존 1을 밀어냄)
    gone_hashes = {old_hashes[i] for i in gone}
    marks = conn.execute(
        "SELECT chunk_index, content_hash, extracted_at FROM extracted_chunks WHERE doc_id = ?",
        (doc_id,),
    ).fetchall()
    keep: dict[int, tuple[str, str]] = {}
    legacy: dict[str, str] = {}
    for r in marks:
        idx = r["chunk_index"]
        if idx is None:
            # 해시-only 레거시 마커 — 사라진 내용의 것은 버린다. 남으면 '추출 완료'로 보여 재추출을 막는다
            if r["content_hash"] not in gone_hashes:
                legacy[r["content_hash"]] = r["extracted_at"]
        elif idx not in gone:
            keep[moved.get(idx, idx)] = (r["content_hash"], r["extracted_at"])
    conn.execute("DELETE FROM extracted_chunks WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO extracted_chunks (doc_id, chunk_index, content_hash, extracted_at) "
        "VALUES (?, ?, ?, ?)",
        [(doc_id, idx, h, at) for idx, (h, at) in keep.items()]
        + [(doc_id, None, h, at) for h, at in legacy.items()],
    )
    recompute_mention_counts(conn, touched)
    return {"moved": len(moved), "dropped": dropped}


def recompute_mention_counts(conn: sqlite3.Connection, concept_ids: set[int]) -> None:
    """concepts.mention_count를 concept_mentions 실측치로 재계산 (표시·큐레이션 정렬용)."""
    for cid in concept_ids:
        conn.execute(
            "UPDATE concepts SET mention_count = "
            "(SELECT COUNT(*) FROM concept_mentions WHERE concept_id = ?) WHERE id = ?",
            (cid, cid),
        )


# ---------- 추출 마커 ----------

def extracted_markers(
    conn: sqlite3.Connection,
) -> tuple[dict[tuple[str, int], str], set[tuple[str, str]]]:
    """추출 마커 조회 → ((doc_id, chunk_index) → content_hash, 레거시 (doc_id, hash) 집합).

    레거시 = chunk_index 없이 해시로만 기록된 구마커. 청크가 다른 인덱스로 이동해도
    '추출 완료'로 보이는 한계가 있으나, 내용이 바뀌면 자연 소멸한다.
    """
    by_idx: dict[tuple[str, int], str] = {}
    legacy: set[tuple[str, str]] = set()
    for r in conn.execute("SELECT doc_id, chunk_index, content_hash FROM extracted_chunks"):
        if r["chunk_index"] is None:
            legacy.add((r["doc_id"], r["content_hash"]))
        else:
            by_idx[(r["doc_id"], r["chunk_index"])] = r["content_hash"]
    return by_idx, legacy


def is_pending(
    source: dict,
    by_idx: dict[tuple[str, int], str],
    legacy: set[tuple[str, str]],
) -> bool:
    """ES 청크 소스(doc_id/chunk_index/content_hash)가 (재)추출 대상인지.

    (doc_id, chunk_index)의 마커 해시가 현재 해시와 같아야 추출 완료 —
    청크가 이동하거나 내용이 바뀌면 pending이 되어 멘션이 올바른 인덱스로 갱신된다.
    content_hash 없는 구청크는 항상 pending.
    """
    h = source.get("content_hash")
    if not h:
        return True
    if by_idx.get((source["doc_id"], source["chunk_index"])) == h:
        return False
    return (source["doc_id"], h) not in legacy


def record_extraction(
    conn: sqlite3.Connection, doc_id: str, chunk_index: int, content_hash: str, now: str
) -> None:
    """추출 완료 마커 기록. 같은 내용의 레거시(해시-only) 마커는 함께 정리."""
    conn.execute(
        "INSERT OR REPLACE INTO extracted_chunks "
        "(doc_id, chunk_index, content_hash, extracted_at) VALUES (?, ?, ?, ?)",
        (doc_id, chunk_index, content_hash, now),
    )
    conn.execute(
        "DELETE FROM extracted_chunks "
        "WHERE doc_id = ? AND chunk_index IS NULL AND content_hash = ?",
        (doc_id, content_hash),
    )


def mentions_for_chunks(
    conn: sqlite3.Connection, pairs: list[tuple[str, int]]
) -> dict[tuple[str, int], list[dict]]:
    """(doc_id, chunk_index) 쌍들에 언급된 개념을 단일 SELECT로 조회.

    반환: {(doc_id, chunk_index): [{"name": ..., "slug": ...}, ...]}
    projected_slugs가 None이면 전 개념 포함(큐레이션 테이블이 비면 전량 투영과 동일),
    set이면 투영된 개념(교집합)만.
    """
    if not pairs:
        return {}
    allowed = projected_slugs(conn)
    where = " OR ".join(["(m.doc_id = ? AND m.chunk_index = ?)"] * len(pairs))
    params = [v for pair in pairs for v in pair]
    rows = conn.execute(
        "SELECT m.doc_id, m.chunk_index, c.name, c.slug "
        "FROM concept_mentions m JOIN concepts c ON c.id = m.concept_id "
        f"WHERE {where}",
        params,
    ).fetchall()
    result: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        if allowed is not None and r["slug"] not in allowed:
            continue
        result.setdefault((r["doc_id"], r["chunk_index"]), []).append(
            {"name": r["name"], "slug": r["slug"]}
        )
    return result


# ---------- Queries ----------

def get_concept(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    namespace: str | None = None,
    category: str | None = None,
) -> sqlite3.Row | None:
    """Resolve a name/slug/alias without silently crossing namespaces.

    An unqualified identifier that maps to multiple scoped concepts returns
    ``None``.  Read tools can then report a clear missing/ambiguous result, and
    callers with document category context can disambiguate by passing
    ``category``.
    """
    slug = make_slug(identifier)
    if not slug:
        return None
    identity_namespace = _effective_namespace(namespace, category)

    # A physical slug remains an exact lookup for compatibility, but an
    # unqualified base name must first pass the ambiguity guard below.
    row = find_concept_by_slug(conn, slug, namespace=identity_namespace or None)
    candidates = find_concepts_by_base_slug(
        conn, slug, namespace=identity_namespace or None
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return None
    if row is not None and _scope_matches(row, identity_namespace):
        return row

    # Alias lookup has the same ambiguity guard.  An alias with the same
    # normalized text as another concept's canonical name is intentionally not
    # preferred over the canonical candidate above.
    return find_concept_by_alias(
        conn, slug, namespace=identity_namespace or None
    )


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


def list_edges_inbound(conn: sqlite3.Connection, concept_id: int) -> list[sqlite3.Row]:
    """Inbound 엣지 (concept_id가 dst인 관계). list_edges와 대칭 — src 개념명/slug 포함."""
    rows = conn.execute(
        "SELECT e.*, c.name as src_name, c.slug as src_slug "
        "FROM concept_edges e JOIN concepts c ON c.id = e.src_id "
        "WHERE e.dst_id = ? "
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


def orphan_concept_slugs(conn: sqlite3.Connection) -> list[str]:
    """멘션을 모두 잃은 개념 slug 목록 (문서 삭제·병합 뒤 잔존 후보, slug 순)."""
    rows = conn.execute(
        "SELECT slug FROM concepts "
        "WHERE id NOT IN (SELECT DISTINCT concept_id FROM concept_mentions) "
        "ORDER BY slug"
    ).fetchall()
    return [r["slug"] for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    return {
        "concepts": conn.execute("SELECT COUNT(*) AS c FROM concepts").fetchone()["c"],
        "edges": conn.execute("SELECT COUNT(*) AS c FROM concept_edges").fetchone()["c"],
        "edge_evidence": conn.execute(
            "SELECT COUNT(*) AS c FROM concept_edge_evidence"
        ).fetchone()["c"],
        "mentions": conn.execute("SELECT COUNT(*) AS c FROM concept_mentions").fetchone()["c"],
        "documents": conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"],
        "aliases": conn.execute("SELECT COUNT(*) AS c FROM concept_aliases").fetchone()["c"],
    }


def edge_evidence_coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(evidence가 있는 materialized edge 수, 전체 edge 수)."""
    covered = conn.execute(
        "SELECT COUNT(*) AS c FROM concept_edges e WHERE EXISTS ("
        "SELECT 1 FROM concept_edge_evidence ev "
        "WHERE ev.src_id=e.src_id AND ev.dst_id=e.dst_id AND ev.relation=e.relation)"
    ).fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) AS c FROM concept_edges").fetchone()["c"]
    return covered, total


def edge_evidence_rebuild_active(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM graph_meta WHERE key = 'edge_evidence_rebuild'"
    ).fetchone()
    return bool(row and row["value"] == "active")


def prepare_edge_evidence_rebuild(conn: sqlite3.Connection) -> dict:
    """기존 그래프를 서비스한 채 staging evidence/마커를 비워 전량 재추출을 준비."""
    before = {
        "edges_preserved": conn.execute(
            "SELECT COUNT(*) AS c FROM concept_edges"
        ).fetchone()["c"],
        "edge_evidence": conn.execute(
            "SELECT COUNT(*) AS c FROM concept_edge_evidence"
        ).fetchone()["c"],
        "mentions_preserved": conn.execute(
            "SELECT COUNT(*) AS c FROM concept_mentions"
        ).fetchone()["c"],
        "markers": conn.execute("SELECT COUNT(*) AS c FROM extracted_chunks").fetchone()["c"],
    }
    conn.execute("DELETE FROM concept_edge_evidence")
    conn.execute("DELETE FROM extracted_chunks")
    conn.execute("DELETE FROM concept_curation WHERE trim(slug) = ''")
    conn.execute("DELETE FROM concepts WHERE trim(slug) = ''")
    conn.execute(
        "INSERT INTO graph_meta (key, value) VALUES ('edge_evidence_rebuild', 'active') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    return before


def finalize_edge_evidence_rebuild(conn: sqlite3.Connection) -> dict:
    """staging evidence를 materialized edge로 원자 전환하고 rebuild 상태를 종료."""
    if not edge_evidence_rebuild_active(conn):
        raise ValueError("진행 중인 edge evidence 재구축이 없습니다.")
    before = conn.execute("SELECT COUNT(*) AS c FROM concept_edges").fetchone()["c"]
    evidence = conn.execute(
        "SELECT COUNT(*) AS c FROM concept_edge_evidence"
    ).fetchone()["c"]
    conn.execute("DELETE FROM concept_edges")
    conn.execute(
        "INSERT INTO concept_edges "
        "(src_id, dst_id, relation, weight, evidence_count, confidence) "
        "SELECT src_id, dst_id, relation, CAST(COUNT(*) AS REAL), COUNT(*), MAX(confidence) "
        "FROM concept_edge_evidence GROUP BY src_id, dst_id, relation"
    )
    conn.execute(
        "UPDATE concepts SET mention_count = ("
        "SELECT COUNT(*) FROM concept_mentions m WHERE m.concept_id = concepts.id)"
    )
    conn.execute("DELETE FROM graph_meta WHERE key = 'edge_evidence_rebuild'")
    after = conn.execute("SELECT COUNT(*) AS c FROM concept_edges").fetchone()["c"]
    return {"edges_before": before, "edges_after": after, "edge_evidence": evidence}


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
    """label='real'이고 관계(엣지)를 1개 이상 보유(src 또는 dst)한 slug 집합.

    고아(관계 0) 개념노트는 연결 가치가 없어 투영 제외 — 검토(2026-07-10) 관찰:
    품질 판별력은 mention수보다 관계 유무.

    큐레이션 테이블이 비어있으면 None (v1 전량 투영).
    """
    if conn.execute("SELECT 1 FROM concept_curation LIMIT 1").fetchone() is None:
        return None
    rows = conn.execute(
        "SELECT cc.slug FROM concept_curation cc "
        "JOIN concepts c ON c.slug = cc.slug "
        "WHERE cc.label = 'real' AND EXISTS ("
        "  SELECT 1 FROM concept_edges e WHERE e.src_id = c.id OR e.dst_id = c.id"
        ")"
    ).fetchall()
    return {r["slug"] for r in rows}


def get_prose(conn: sqlite3.Connection, slug: str) -> str | None:
    row = conn.execute(
        "SELECT prose FROM concept_curation WHERE slug = ?", (slug,)
    ).fetchone()
    return row["prose"] if row else None


# ---------- Merge ----------

def merge_concepts(
    conn: sqlite3.Connection, winner_slug: str, loser_slugs: list[str]
) -> dict:
    """표기 변형으로 쪼개진 동일 개념들을 winner 하나로 병합.

    각 loser에 대해 edges/mentions를 winner로 재지정하고, alias·prose를 winner로 승계한 뒤
    loser concepts 행을 삭제한다. mention_count는 재지정된 멘션에서 재계산.
    존재하지 않거나 winner 자신인 loser_slug는 스킵.

    반환: {"merged": n, "edges_repointed": n, "mentions_repointed": n,
           "aliases_added": n, "skipped": [...]}
    """
    winner = find_concept_by_slug(conn, winner_slug)
    if winner is None:
        raise ValueError(f"승자 개념을 찾을 수 없습니다: {winner_slug}")
    winner_id = winner["id"]

    result = {
        "merged": 0,
        "edges_repointed": 0,
        "evidence_repointed": 0,
        "mentions_repointed": 0,
        "aliases_added": 0,
        "skipped": [],
    }

    for loser_slug in loser_slugs:
        loser = find_concept_by_slug(conn, loser_slug)
        if loser is None or loser["id"] == winner_id:
            result["skipped"].append(loser_slug)
            continue
        loser_id = loser["id"]

        # ---- edges re-point (src/dst 양쪽) ----
        edge_rows = conn.execute(
            "SELECT * FROM concept_edges WHERE src_id = ? OR dst_id = ?",
            (loser_id, loser_id),
        ).fetchall()
        for e in edge_rows:
            new_src = winner_id if e["src_id"] == loser_id else e["src_id"]
            new_dst = winner_id if e["dst_id"] == loser_id else e["dst_id"]
            if new_src == new_dst:
                # re-point 결과 self-edge — 무의미하므로 버린다.
                conn.execute(
                    "DELETE FROM concept_edges WHERE src_id = ? AND dst_id = ? AND relation = ?",
                    (e["src_id"], e["dst_id"], e["relation"]),
                )
                result["edges_repointed"] += 1
                continue
            conflict = conn.execute(
                "SELECT 1 FROM concept_edges WHERE src_id = ? AND dst_id = ? AND relation = ?",
                (new_src, new_dst, e["relation"]),
            ).fetchone()
            if conflict:
                # winner 쪽에 이미 같은 (src,dst,relation) 엣지 존재 — weight/evidence 누적 후 loser 엣지 삭제.
                # confidence는 add_edge와 동일한 NULL 보존 — 양쪽 다 NULL(구데이터)이면 0 발명 없이 NULL 유지.
                conn.execute(
                    "UPDATE concept_edges SET weight = weight + ?, evidence_count = evidence_count + ?, "
                    "confidence = CASE WHEN confidence IS NULL AND ? IS NULL THEN NULL "
                    "ELSE MAX(COALESCE(confidence, 0), COALESCE(?, 0)) END "
                    "WHERE src_id = ? AND dst_id = ? AND relation = ?",
                    (e["weight"], e["evidence_count"], e["confidence"], e["confidence"],
                     new_src, new_dst, e["relation"]),
                )
                conn.execute(
                    "DELETE FROM concept_edges WHERE src_id = ? AND dst_id = ? AND relation = ?",
                    (e["src_id"], e["dst_id"], e["relation"]),
                )
            else:
                conn.execute(
                    "UPDATE concept_edges SET src_id = ?, dst_id = ? "
                    "WHERE src_id = ? AND dst_id = ? AND relation = ?",
                    (new_src, new_dst, e["src_id"], e["dst_id"], e["relation"]),
                )
            result["edges_repointed"] += 1

        # ---- edge evidence re-point ----
        evidence_rows = conn.execute(
            "SELECT * FROM concept_edge_evidence WHERE src_id = ? OR dst_id = ?",
            (loser_id, loser_id),
        ).fetchall()
        affected_edges: set[tuple[int, int, str]] = set()
        for evidence in evidence_rows:
            conn.execute(
                "DELETE FROM concept_edge_evidence WHERE doc_id = ? AND chunk_index = ? "
                "AND src_id = ? AND dst_id = ? AND relation = ?",
                (
                    evidence["doc_id"],
                    evidence["chunk_index"],
                    evidence["src_id"],
                    evidence["dst_id"],
                    evidence["relation"],
                ),
            )
            new_src = winner_id if evidence["src_id"] == loser_id else evidence["src_id"]
            new_dst = winner_id if evidence["dst_id"] == loser_id else evidence["dst_id"]
            if new_src == new_dst:
                result["evidence_repointed"] += 1
                continue
            conn.execute(
                "INSERT INTO concept_edge_evidence "
                "(doc_id, chunk_index, src_id, dst_id, relation, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_id, chunk_index, src_id, dst_id, relation) DO UPDATE SET "
                "confidence = CASE "
                "WHEN excluded.confidence IS NULL THEN concept_edge_evidence.confidence "
                "WHEN concept_edge_evidence.confidence IS NULL THEN excluded.confidence "
                "ELSE MAX(concept_edge_evidence.confidence, excluded.confidence) END",
                (
                    evidence["doc_id"],
                    evidence["chunk_index"],
                    new_src,
                    new_dst,
                    evidence["relation"],
                    evidence["confidence"],
                ),
            )
            affected_edges.add((new_src, new_dst, evidence["relation"]))
            result["evidence_repointed"] += 1
        for src_id, dst_id, relation in affected_edges:
            _recompute_edge(conn, src_id, dst_id, relation)

        # ---- mentions re-point ----
        mention_rows = conn.execute(
            "SELECT doc_id, chunk_index FROM concept_mentions WHERE concept_id = ?",
            (loser_id,),
        ).fetchall()
        for m in mention_rows:
            conflict = conn.execute(
                "SELECT 1 FROM concept_mentions WHERE concept_id = ? AND doc_id = ? AND chunk_index = ?",
                (winner_id, m["doc_id"], m["chunk_index"]),
            ).fetchone()
            if conflict:
                # winner가 이미 같은 (doc_id, chunk_index)를 언급 — 중복 mention 삭제.
                conn.execute(
                    "DELETE FROM concept_mentions WHERE concept_id = ? AND doc_id = ? AND chunk_index = ?",
                    (loser_id, m["doc_id"], m["chunk_index"]),
                )
            else:
                conn.execute(
                    "UPDATE concept_mentions SET concept_id = ? "
                    "WHERE concept_id = ? AND doc_id = ? AND chunk_index = ?",
                    (winner_id, loser_id, m["doc_id"], m["chunk_index"]),
                )
            result["mentions_repointed"] += 1

        # ---- aliases 이전: loser name + aliases 전부 winner alias로 ----
        for alias in [loser["name"], *list_aliases(conn, loser_id)]:
            before = conn.total_changes
            # The merge command is an explicit operator decision; permit the
            # loser canonical name to become a winner alias before the loser
            # row is deleted.  Ordinary extraction writes remain guarded.
            add_alias(conn, winner_id, alias, _allow_conflict=True)
            if conn.total_changes != before:
                result["aliases_added"] += 1

        # ---- mention_count: 재지정된 멘션 실측치로 재계산 (합산은 중복 멘션을 이중 계상) ----
        recompute_mention_counts(conn, {winner_id})
        conn.execute("UPDATE concepts SET updated_at = ? WHERE id = ?", (_now(), winner_id))

        # ---- curation 승계: winner 미큐레이션이면 label째, 아니면 prose만 채움. loser curation은 삭제 ----
        winner_curation = conn.execute(
            "SELECT label, prose FROM concept_curation WHERE slug = ?", (winner_slug,)
        ).fetchone()
        loser_curation = conn.execute(
            "SELECT label, prose FROM concept_curation WHERE slug = ?", (loser_slug,)
        ).fetchone()
        if loser_curation and winner_curation is None:
            # prose 없는 label만 있어도 승계 — real 판정이 유실되면 다음 sync에서
            # winner 노트가 prune된다.
            set_curation(conn, winner_slug, loser_curation["label"], prose=loser_curation["prose"])
        elif loser_curation and loser_curation["prose"] and not (
            winner_curation and winner_curation["prose"]
        ):
            label = winner_curation["label"] if winner_curation else loser_curation["label"]
            set_curation(conn, winner_slug, label, prose=loser_curation["prose"])
        if loser_curation:
            conn.execute("DELETE FROM concept_curation WHERE slug = ?", (loser_slug,))

        # ---- loser concepts 행 삭제 (concept_aliases는 ON DELETE CASCADE로 정리) ----
        conn.execute("DELETE FROM concepts WHERE id = ?", (loser_id,))
        result["merged"] += 1

    return result
