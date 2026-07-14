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
) -> int:
    """개념 insert or update. 반환: concept_id.

    정규화 순서: slug 일치 → alias 일치 → 임베딩 유사도.
    mention_count는 여기서 올리지 않는다 — concept_mentions에서 유도(recompute_mention_counts).
    호출마다 +1 하면 같은 청크 재추출이 카운트를 부풀린다.
    """
    slug = make_slug(name)
    now = _now()

    # 1. slug 일치 → 2. alias slug 일치
    row = find_concept_by_slug(conn, slug) or find_concept_by_alias(conn, slug)
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
                "UPDATE concepts SET updated_at = ? WHERE id = ?", (now, existing["id"])
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
    """단일 doc_id의 concept_mentions·documents·extracted_chunks 행 삭제 (ES 문서 삭제 후 그래프 정리용).

    추출 마커(extracted_chunks)도 함께 지운다 — 마커가 남으면 삭제 후 동일 내용이
    재생성됐을 때 pending에서 빠져 멘션이 영구 결손된다.
    반환: {"mentions_pruned": n, "documents_pruned": m}
    """
    m = conn.execute("DELETE FROM concept_mentions WHERE doc_id = ?", (doc_id,)).rowcount
    d = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,)).rowcount
    conn.execute("DELETE FROM extracted_chunks WHERE doc_id = ?", (doc_id,))
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
) -> None:
    """동일 (src, dst, relation) 재호출 시 weight/evidence_count 누적.

    confidence: 이산 루브릭 값(0.9/0.7/0.5). 기존 행은 MAX(COALESCE(기존,0), 신규)로
    누적, 신규가 None이면 기존 값 유지 (NULL=루브릭 도입 전 구데이터).

    ponytail: 엣지는 append-only — 멘션과 달리 청크별 provenance가 없어 같은 청크를
    재추출하면 weight/evidence_count가 부풀고 되돌릴 수 없다(전량 재빌드만이 리셋).
    weight는 노트의 관계 정렬에만 쓰여 피해가 표시 수준이라 감수. 정확한 집계가
    필요해지면 concept_edge_evidence(doc_id, chunk_index, src, dst, relation) 테이블을
    두고 weight를 COUNT로 유도할 것.
    """
    if src_id == dst_id:
        return
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
            add_alias(conn, winner_id, alias)
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
