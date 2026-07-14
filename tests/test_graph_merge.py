"""개념 병합(merge_concepts) 단위 테스트. 실제 ES/DB 서버 없이 tmp SQLite로 검증."""

from __future__ import annotations

import pytest

from pkb.graph import store as gstore
from pkb.graph.notes import sync_concept_notes
from pkb.graph.schema import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "graph.sqlite"
    init_schema(str(db_path))
    connection = get_connection(str(db_path))
    yield connection
    connection.close()


@pytest.fixture
def data_root(monkeypatch, tmp_path):
    """CWD를 tmp_path로 바꾸고 tmp_path/data를 코퍼스 루트로 강제 (.env의 DATA_ROOT 무시)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pkb.config.settings.data_root", "data")
    root = tmp_path / "data"
    root.mkdir()
    return root


def _seed_variants(conn):
    """표기변형 2개(loser) + 본체 1개(winner) 시드: a2a / a2a agent2agent protocol / a2aagent2agent protocol.

    - 엣지 겹침: winner, loser1 둘 다 -> other(related_to) — 병합 시 누적돼야 함.
    - loser2 고유 엣지(-> other, prerequisite_of) — 충돌 없이 winner로 재지정.
    - mention 겹침: winner, loser1 둘 다 x.md/chunk0 — 병합 시 dedup.
    - loser2 고유 mention(y.md/chunk0) — winner로 재지정.
    - prose는 loser2에만 (winner는 prose 없음) — 병합 시 winner로 승계.
    - 셋 다 real 큐레이션(엣지 보유로 투영 조건도 만족).
    """
    winner_id = gstore.upsert_concept(conn, name="a2a agent2agent protocol", description="본체 설명")
    loser1_id = gstore.upsert_concept(conn, name="a2a", description="")
    loser2_id = gstore.upsert_concept(conn, name="a2aagent2agent protocol", description="")
    other_id = gstore.upsert_concept(conn, name="mcp", description="MCP 설명")

    gstore.upsert_document(conn, doc_id="data/study/x.md", title="X", category="study")
    gstore.upsert_document(conn, doc_id="data/study/y.md", title="Y", category="study")

    gstore.add_edge(conn, winner_id, other_id, "related_to")
    gstore.add_edge(conn, loser1_id, other_id, "related_to")  # winner와 겹침 → 누적 대상
    gstore.add_edge(conn, loser2_id, other_id, "prerequisite_of")  # 고유 → 그대로 재지정

    gstore.add_mention(conn, winner_id, "data/study/x.md", 0)
    gstore.add_mention(conn, loser1_id, "data/study/x.md", 0)  # winner와 겹침 → dedup 대상
    gstore.add_mention(conn, loser2_id, "data/study/y.md", 0)  # 고유 → 그대로 재지정

    gstore.add_alias(conn, loser1_id, "A2A Protocol")

    gstore.set_curation(conn, gstore.make_slug("a2a agent2agent protocol"), "real")
    gstore.set_curation(conn, gstore.make_slug("a2a"), "real")
    gstore.set_curation(
        conn, gstore.make_slug("a2aagent2agent protocol"), "real", prose="loser2 prose"
    )

    conn.commit()
    return winner_id, loser1_id, loser2_id, other_id


def test_merge_concepts_accumulates_edges_dedups_mentions_and_carries_alias_prose(conn):
    winner_id, loser1_id, loser2_id, other_id = _seed_variants(conn)
    winner_slug = gstore.make_slug("a2a agent2agent protocol")
    loser1_slug = gstore.make_slug("a2a")
    loser2_slug = gstore.make_slug("a2aagent2agent protocol")

    result = gstore.merge_concepts(conn, winner_slug, [loser1_slug, loser2_slug, "ghost-slug"])
    conn.commit()

    assert result["merged"] == 2
    assert result["skipped"] == ["ghost-slug"]

    # winner 생존, loser 부재
    assert gstore.find_concept_by_slug(conn, winner_slug) is not None
    assert gstore.find_concept_by_slug(conn, loser1_slug) is None
    assert gstore.find_concept_by_slug(conn, loser2_slug) is None

    # 엣지 유니크 충돌(winner, loser1 둘 다 -> other related_to) → weight/evidence_count 누적
    related = gstore.list_edges(conn, winner_id, "related_to")
    assert len(related) == 1
    assert related[0]["weight"] == 2.0
    assert related[0]["evidence_count"] == 2

    # loser2 고유 엣지는 충돌 없이 winner로 재지정
    prereq = gstore.list_edges(conn, winner_id, "prerequisite_of")
    assert len(prereq) == 1
    assert prereq[0]["dst_id"] == other_id

    # mention dedup + 고유 mention 재지정
    mentions = {(m["doc_id"], m["chunk_index"]) for m in gstore.list_mentions(conn, winner_id, limit=10)}
    assert mentions == {("data/study/x.md", 0), ("data/study/y.md", 0)}

    # alias 승계: loser name + 기존 alias 전부 winner alias로
    aliases = set(gstore.list_aliases(conn, winner_id))
    assert {"a2a", "a2aagent2agent protocol", "A2A Protocol"} <= aliases

    # prose 승계 (winner엔 없었고 loser2엔 있었음)
    assert gstore.get_prose(conn, winner_slug) == "loser2 prose"

    # mention_count는 재지정된 멘션 실측치 — 중복 멘션(x.md:0)은 한 번만 (합산이면 3으로 부풀었음)
    assert gstore.find_concept_by_slug(conn, winner_slug)["mention_count"] == 2

    # loser의 concept_curation 행 삭제
    assert (
        conn.execute(
            "SELECT 1 FROM concept_curation WHERE slug = ?", (loser2_slug,)
        ).fetchone()
        is None
    )


def test_merge_missing_loser_skipped_and_counted(conn):
    gstore.upsert_concept(conn, name="본체")
    conn.commit()
    winner_slug = gstore.make_slug("본체")

    result = gstore.merge_concepts(conn, winner_slug, ["ghost-slug"])

    assert result["merged"] == 0
    assert result["skipped"] == ["ghost-slug"]


# ---------- 관계 confidence 이산 루브릭 ----------


def test_add_edge_confidence_accumulates_max(conn):
    a = gstore.upsert_concept(conn, name="개념 A")
    b = gstore.upsert_concept(conn, name="개념 B")

    gstore.add_edge(conn, a, b, "related_to", confidence=0.5)
    gstore.add_edge(conn, a, b, "related_to", confidence=0.9)  # 높은 값 → MAX 갱신
    gstore.add_edge(conn, a, b, "related_to")  # None → 기존 유지
    gstore.add_edge(conn, a, b, "related_to", confidence=0.5)  # 낮은 값 → MAX로 무시
    conn.commit()

    edge = gstore.list_edges(conn, a, "related_to")[0]
    assert edge["confidence"] == 0.9
    assert edge["evidence_count"] == 4


def test_add_edge_confidence_none_stays_null(conn):
    """구데이터(NULL) 엣지에 None 재호출 — NULL 유지 (0 발명 금지)."""
    a = gstore.upsert_concept(conn, name="개념 A")
    b = gstore.upsert_concept(conn, name="개념 B")
    gstore.add_edge(conn, a, b, "related_to")
    gstore.add_edge(conn, a, b, "related_to")
    conn.commit()

    assert gstore.list_edges(conn, a)[0]["confidence"] is None


def test_merge_carries_edge_confidence_max(conn):
    """엣지 충돌 병합 시 confidence = MAX(COALESCE(양쪽,0)) 승계."""
    winner = gstore.upsert_concept(conn, name="본체")
    loser = gstore.upsert_concept(conn, name="변형")
    other = gstore.upsert_concept(conn, name="상대")
    gstore.add_edge(conn, winner, other, "related_to", confidence=0.5)
    gstore.add_edge(conn, loser, other, "related_to", confidence=0.9)
    conn.commit()

    gstore.merge_concepts(conn, gstore.make_slug("본체"), [gstore.make_slug("변형")])
    conn.commit()

    edge = gstore.list_edges(conn, winner, "related_to")[0]
    assert edge["confidence"] == 0.9
    assert edge["evidence_count"] == 2


def test_merge_edge_conflict_null_confidence_stays_null(conn):
    """엣지 충돌 병합에서 양쪽 confidence가 NULL(구데이터)이면 NULL 유지 — 0 발명 금지."""
    winner = gstore.upsert_concept(conn, name="본체")
    loser = gstore.upsert_concept(conn, name="변형")
    other = gstore.upsert_concept(conn, name="상대")
    gstore.add_edge(conn, winner, other, "related_to")
    gstore.add_edge(conn, loser, other, "related_to")
    conn.commit()

    gstore.merge_concepts(conn, gstore.make_slug("본체"), [gstore.make_slug("변형")])
    conn.commit()

    edge = gstore.list_edges(conn, winner, "related_to")[0]
    assert edge["confidence"] is None
    assert edge["evidence_count"] == 2


def test_merge_carries_label_when_winner_uncurated(conn):
    """winner 미큐레이션 + loser에 prose 없는 label만 있어도 큐레이션(real 판정) 승계.

    유실되면 winner가 미큐레이션으로 남아 다음 sync에서 노트가 prune된다.
    """
    gstore.upsert_concept(conn, name="본체")
    gstore.upsert_concept(conn, name="변형")
    gstore.set_curation(conn, gstore.make_slug("변형"), "real")
    conn.commit()

    gstore.merge_concepts(conn, gstore.make_slug("본체"), [gstore.make_slug("변형")])
    conn.commit()

    row = conn.execute(
        "SELECT label, prose FROM concept_curation WHERE slug = ?",
        (gstore.make_slug("본체"),),
    ).fetchone()
    assert row is not None
    assert row["label"] == "real"
    assert row["prose"] is None


def test_merge_then_sync_prunes_loser_notes(conn, data_root):
    """병합 후 sync_concept_notes 재실행 시 loser 노트가 orphan prune 후보로 잡혀 정리된다."""
    _seed_variants(conn)
    winner_slug = gstore.make_slug("a2a agent2agent protocol")
    loser1_slug = gstore.make_slug("a2a")
    loser2_slug = gstore.make_slug("a2aagent2agent protocol")

    sync_concept_notes(conn)  # 병합 전: winner/loser1/loser2 모두 real+엣지 有 → 노트 생성
    concepts_dir = data_root / "_concepts"
    assert (concepts_dir / f"{loser1_slug}.md").exists()
    assert (concepts_dir / f"{loser2_slug}.md").exists()

    gstore.merge_concepts(conn, winner_slug, [loser1_slug, loser2_slug])
    conn.commit()

    result = sync_concept_notes(conn)
    assert result["pruned"] == 2
    assert not (concepts_dir / f"{loser1_slug}.md").exists()
    assert not (concepts_dir / f"{loser2_slug}.md").exists()
    assert (concepts_dir / f"{winner_slug}.md").exists()
