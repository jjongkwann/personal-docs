"""doctor 조치 후보 나열 테스트. ES 없이 검증.

orphan SQL은 인메모리 SQLite로, ES 집계는 test_retrieve_query_shapes.py 패턴의
셰이프 검증(호출 인자 캡처)으로 본다 — 집계 파서 오류는 런타임에만 나므로.
"""

from __future__ import annotations

import pytest

from pkb.graph import store as gstore
from pkb.graph.schema import SCHEMA_SQL, get_connection, init_schema
from pkb.report import PURGE_CANDIDATE_DAYS, build_health_report

# ---------- orphan_concept_slugs (인메모리 SQLite) ----------


def _memdb():
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    return conn


def test_orphan_concept_slugs_lists_mentionless_only():
    conn = _memdb()
    kept = gstore.upsert_concept(conn, name="bm25")
    gstore.upsert_concept(conn, name="rrf")
    gstore.upsert_concept(conn, name="kNN")
    gstore.add_mention(conn, kept, "data/rag/x.md", 0)

    assert gstore.orphan_concept_slugs(conn) == ["knn", "rrf"]  # slug 순
    conn.close()


def test_orphan_concept_slugs_empty_when_all_mentioned():
    conn = _memdb()
    cid = gstore.upsert_concept(conn, name="bm25")
    gstore.add_mention(conn, cid, "data/rag/x.md", 0)

    assert gstore.orphan_concept_slugs(conn) == []
    conn.close()


# ---------- ES 집계 셰이프 (호출 인자 캡처) ----------


class _FakeES:
    """build_health_report가 부르는 최소 ES 표면. search 호출 인자를 캡처한다."""

    def __init__(
        self,
        agg_docs: list[str] | None = None,
        agg_total: int = 0,
        chunk_hashes: list[tuple[str, int, str | None]] | None = None,
    ):
        self.search_calls: list[dict] = []
        self._agg_docs = agg_docs or []
        self._agg_total = agg_total
        # [(doc_id, chunk_index, content_hash|None), ...] — 그래프 미추출 스캔용
        self._chunk_hashes = chunk_hashes
        self.indices = self  # indices.exists를 자기 자신으로 위임

    def info(self):
        return {"version": {"number": "8.0.0-test"}}

    def exists(self, index):
        return True

    def count(self, index=None, query=None):
        return {"count": 0}

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        aggs = kwargs.get("aggs", {})
        if "by_doc" in aggs:
            return {
                "hits": {"total": {"value": self._agg_total}},
                "aggregations": {
                    "by_doc": {
                        "buckets": [{"key": d, "doc_count": 1} for d in self._agg_docs]
                    }
                },
            }
        if kwargs.get("source_includes") == ["doc_id", "chunk_index", "content_hash"]:
            hashes = self._chunk_hashes or []
            return {
                "hits": {
                    "total": {"value": len(hashes)},
                    "hits": [
                        {"_source": {"doc_id": d, "chunk_index": i, "content_hash": h}}
                        for d, i, h in hashes
                    ],
                }
            }
        return {
            "hits": {"total": {"value": 0}},
            "aggregations": {name: {"buckets": []} for name in aggs},
        }


@pytest.fixture
def doctor_env(monkeypatch, tmp_path):
    """.env 무시: obsidian 비활성 + 그래프 DB는 tmp 미존재 경로."""
    monkeypatch.setattr("pkb.config.settings.obsidian_path", "")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", str(tmp_path / "no.sqlite"))


def _by_doc_calls(es: _FakeES) -> list[dict]:
    return [c for c in es.search_calls if "by_doc" in c.get("aggs", {})]


def test_expired_agg_query_shape(doctor_env):
    es = _FakeES()
    build_health_report(es)

    call = _by_doc_calls(es)[0]  # 첫 by_doc 집계 = expired 미아카이브
    assert call["size"] == 0
    assert call["aggs"] == {"by_doc": {"terms": {"field": "doc_id", "size": 10}}}
    bool_q = call["query"]["bool"]
    assert {"exists": {"field": "expires_at"}} in bool_q["must"]
    assert {"range": {"expires_at": {"lte": "now"}}} in bool_q["must"]
    assert bool_q["must_not"] == [{"exists": {"field": "archived_at"}}]


def test_purge_agg_query_shape(doctor_env):
    es = _FakeES()
    build_health_report(es)

    call = _by_doc_calls(es)[1]  # 두 번째 by_doc 집계 = purge 후보
    assert call["size"] == 0
    assert call["aggs"] == {"by_doc": {"terms": {"field": "doc_id", "size": 10}}}
    # ES date math 리터럴 — 상수가 바뀌면 쿼리도 함께 바뀌어야 함
    assert call["query"] == {"range": {"archived_at": {"lte": f"now-{PURGE_CANDIDATE_DAYS}d/d"}}}
    assert PURGE_CANDIDATE_DAYS == 30


# ---------- 리포트 출력 ----------


def test_report_lists_candidates_and_purge_hint(doctor_env):
    es = _FakeES(agg_docs=["data/rag/old.md"], agg_total=3)
    report = build_health_report(es)

    assert "expired(still-visible): 3" in report
    assert "- data/rag/old.md" in report
    assert "purge 후보(archived 30일 경과): 3" in report
    assert "`pkb purge-archived`" in report


def test_report_omits_purge_hint_when_no_candidates(doctor_env):
    report = build_health_report(_FakeES())
    assert "purge 후보" not in report
    assert "pkb purge-archived" not in report


def test_report_marks_orphan_with_leftover_note(doctor_env, monkeypatch, tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    monkeypatch.setattr("pkb.config.settings.data_root", str(tmp_path / "data"))
    init_schema(db_path)

    conn = get_connection(db_path)
    kept = gstore.upsert_concept(conn, name="bm25")
    gstore.add_mention(conn, kept, "data/rag/x.md", 0)
    gstore.upsert_concept(conn, name="rrf")  # 고아 + 노트 잔존
    gstore.upsert_concept(conn, name="kNN")  # 고아 + 노트 없음
    conn.commit()
    conn.close()

    concepts_dir = tmp_path / "data" / "_concepts"
    concepts_dir.mkdir(parents=True)
    (concepts_dir / "rrf.md").write_text("---\nslug: rrf\n---\n", encoding="utf-8")

    report = build_health_report(_FakeES())
    assert "고아 개념(멘션 0): 2" in report
    assert "- rrf (노트 잔존)" in report
    assert "- knn" in report
    assert "- knn (노트 잔존)" not in report


def test_pending_chunks_query_shape_and_count(doctor_env, monkeypatch, tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    conn = get_connection(db_path)
    gstore.record_extraction(conn, "data/rag/x.md", 0, "hash1", "2026-01-01T00:00:00+00:00")
    conn.execute(  # 레거시 마커 (chunk_index 없이 해시로만 기록된 구데이터)
        "INSERT INTO extracted_chunks (doc_id, content_hash, extracted_at) VALUES (?, ?, ?)",
        ("data/rag/z.md", "hash3", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    es = _FakeES(chunk_hashes=[
        ("data/rag/x.md", 0, "hash1"),  # 마커와 (인덱스, 해시) 일치 — pending 아님
        ("data/rag/x.md", 1, "hash2"),  # 마커 없음 — pending
        ("data/rag/y.md", 0, None),  # content_hash 없음 — 항상 pending
        ("data/rag/z.md", 2, "hash3"),  # 레거시 해시 매칭 — 인덱스 달라도 추출 완료로 인정
    ])
    report = build_health_report(es)

    call = next(
        c
        for c in es.search_calls
        if c.get("source_includes") == ["doc_id", "chunk_index", "content_hash"]
    )
    assert call["size"] == 10000
    assert call["track_total_hits"] is True

    assert "그래프 미추출 청크: 2 / 4" in report
