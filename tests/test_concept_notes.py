"""개념 원자노트 레이어(SQLite→노트 투영) 단위 테스트. 실제 ES/DB 서버 없이 tmp SQLite로 검증."""

from __future__ import annotations

import pytest
import yaml

from pkb.graph import store as gstore
from pkb.graph.notes import AUTO_END, merge_concept_note, render_concept_note, sync_concept_notes
from pkb.graph.schema import get_connection, init_schema
from pkb.ingest import is_concept_path, process_file


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


@pytest.fixture
def vault_env(monkeypatch, tmp_path):
    """볼트 하위 DATA_ROOT 구성 — 링크가 볼트 물리 경로(PKB/…)로 렌더되는 모드."""
    vault = tmp_path / "vault"
    (vault / "PKB").mkdir(parents=True)
    monkeypatch.setattr("pkb.config.settings.obsidian_path", str(vault))
    monkeypatch.setattr("pkb.config.settings.data_root", str(vault / "PKB"))
    return vault


@pytest.fixture
def no_vault_env(monkeypatch, tmp_path):
    """OBSIDIAN_PATH 미설정 — basename/평문 폴백 모드."""
    monkeypatch.setattr("pkb.config.settings.obsidian_path", "")
    monkeypatch.setattr("pkb.config.settings.data_root", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    return tmp_path


def _seed_two_concepts(conn, relation="part_of"):
    """개념 A --relation--> B, A에 mention 1개 삽입. (a_id, b_id) 반환."""
    a_id = gstore.upsert_concept(conn, name="개념 A", description="A 설명")
    b_id = gstore.upsert_concept(conn, name="개념 B", description="B 설명")
    gstore.add_edge(conn, a_id, b_id, relation)
    gstore.upsert_document(conn, doc_id="data/study/x.md", title="X 문서", category="study")
    gstore.add_mention(conn, a_id, "data/study/x.md", 0)
    conn.commit()
    return a_id, b_id


def test_render_idempotent(conn):
    _seed_two_concepts(conn)
    slug = gstore.make_slug("개념 A")
    assert render_concept_note(conn, slug) == render_concept_note(conn, slug)


def test_frontmatter_quotes_special_chars(conn):
    """콜론 포함 name·쉼표 포함 alias·따옴표 시작 name도 yaml.safe_load로 파싱 가능해야 한다
    (f-string 조립 시 invalid YAML이 되던 문제의 회귀 테스트)."""
    name = 'LLM01: "거의 맞음" 함정'
    cid = gstore.upsert_concept(conn, name=name, description="설명")
    gstore.add_alias(conn, cid, "(ε,δ)-DP")
    gstore.add_alias(conn, cid, "plain-alias")
    conn.commit()

    slug = gstore.make_slug(name)
    text = render_concept_note(conn, slug)
    fm_block = text.split("---")[1]
    meta = yaml.safe_load(fm_block)

    assert meta["name"] == name
    assert meta["aliases"] == sorted(["(ε,δ)-DP", "plain-alias"])

    # 멱등성: 같은 입력 2회 렌더가 바이트 단위로 동일
    assert render_concept_note(conn, slug) == text


def test_wikilink_format_part_of(conn, vault_env):
    """링크 타깃 = 볼트 물리 경로 (data/ 논리 접두어 아님 — Obsidian 클릭 이동의 전제)."""
    _seed_two_concepts(conn, relation="part_of")
    slug_a = gstore.make_slug("개념 A")
    slug_b = gstore.make_slug("개념 B")
    text = render_concept_note(conn, slug_a)
    assert "## 상위 개념 (part_of)" in text
    assert f"- [[PKB/concepts/{slug_b}|개념 B]]" in text
    assert "[[data/concepts/" not in text  # 미해소 논리 경로 금지


def test_wikilink_related_to_in_related_section(conn, vault_env):
    a_id, _ = _seed_two_concepts(conn, relation="part_of")
    c_id = gstore.upsert_concept(conn, name="개념 C", description="C 설명")
    gstore.add_edge(conn, a_id, c_id, "related_to")
    conn.commit()

    slug_a = gstore.make_slug("개념 A")
    slug_c = gstore.make_slug("개념 C")
    text = render_concept_note(conn, slug_a)

    assert "## 관련 개념 (related_to)" in text
    heading_idx = text.index("## 관련 개념 (related_to)")
    link_idx = text.index(f"[[PKB/concepts/{slug_c}|개념 C]]")
    assert link_idx > heading_idx


def test_source_links_use_vault_physical_paths(conn, vault_env):
    """출처: data/→PKB/ 변환 + .md 생략, obsidian/→볼트 루트 상대 + .pdf 확장자 유지."""
    a_id = gstore.upsert_concept(conn, name="개념 A", description="A 설명")
    gstore.upsert_document(conn, doc_id="data/study/x.md", title="X 문서", category="study")
    gstore.add_mention(conn, a_id, "data/study/x.md", 0)
    gstore.upsert_document(conn, doc_id="obsidian/ES/설계.md", title=None, category="obsidian")
    gstore.add_mention(conn, a_id, "obsidian/ES/설계.md", 3)
    gstore.upsert_document(conn, doc_id="obsidian/논문/p.pdf", title=None, category="obsidian")
    gstore.add_mention(conn, a_id, "obsidian/논문/p.pdf", 1)
    conn.commit()

    text = render_concept_note(conn, gstore.make_slug("개념 A"))
    assert "- [[PKB/study/x|X 문서]]" in text
    assert "- [[ES/설계|obsidian/ES/설계.md]]" in text  # 제목 없으면 doc_id 표시
    assert "- [[논문/p.pdf|obsidian/논문/p.pdf]]" in text  # 비-md 확장자 유지
    assert "chunk" not in text  # 청크 위치는 기계용 — 노트에 표시하지 않음


def test_sources_deduped_per_document(conn, vault_env):
    """같은 문서의 다중 청크 언급은 출처 1줄로 dedup."""
    a_id = gstore.upsert_concept(conn, name="개념 A", description="A 설명")
    gstore.upsert_document(conn, doc_id="data/study/x.md", title="X 문서", category="study")
    gstore.add_mention(conn, a_id, "data/study/x.md", 0)
    gstore.add_mention(conn, a_id, "data/study/x.md", 5)
    gstore.add_mention(conn, a_id, "data/study/x.md", 9)
    conn.commit()
    text = render_concept_note(conn, gstore.make_slug("개념 A"))
    assert text.count("- [[PKB/study/x|X 문서]]") == 1


def test_links_fallback_without_vault(conn, no_vault_env):
    """OBSIDIAN_PATH 미설정: 개념 링크는 basename, 출처는 평문."""
    _seed_two_concepts(conn, relation="part_of")
    slug_b = gstore.make_slug("개념 B")
    text = render_concept_note(conn, gstore.make_slug("개념 A"))
    assert f"- [[{slug_b}|개념 B]]" in text
    assert "- data/study/x.md" in text
    assert "[[PKB/" not in text and "[[data/" not in text


def test_marker_preservation_on_merge(conn):
    a_id, _ = _seed_two_concepts(conn)
    slug_a = gstore.make_slug("개념 A")
    rendered_v1 = render_concept_note(conn, slug_a)

    user_prose = "\n\n## 내 메모\n여기는 내가 직접 쓴 산문.\n"
    existing = rendered_v1 + user_prose

    # DB 상태 변경 (새 관계 추가) → auto 구간 내용이 바뀌어야 merge가 의미있게 검증됨
    d_id = gstore.upsert_concept(conn, name="개념 D", description="D 설명")
    gstore.add_edge(conn, a_id, d_id, "prerequisite_of")
    conn.commit()

    rendered_v2 = render_concept_note(conn, slug_a)
    assert rendered_v2 != rendered_v1
    assert "개념 D" not in rendered_v1
    assert "개념 D" in rendered_v2

    merged = merge_concept_note(existing, rendered_v2)
    assert merged.endswith(user_prose)
    assert "개념 D" in merged
    assert AUTO_END in merged


def test_merge_no_existing_marker_returns_rendered():
    rendered = "---\nslug: x\n---\n\n<!-- pkb:auto:start -->\nbody\n<!-- pkb:auto:end -->\n"
    assert merge_concept_note("아무 텍스트 (마커 없음)", rendered) == rendered


def test_is_concept_path():
    assert is_concept_path("data/concepts/x.md")
    assert not is_concept_path("data/study/x.md")


def test_process_file_skips_concept_path(data_root):
    concepts_dir = data_root / "concepts"
    concepts_dir.mkdir()
    f = concepts_dir / "foo.md"
    f.write_text("---\nslug: foo\n---\n\nbody", encoding="utf-8")
    assert process_file(f, data_root) == []


def test_sync_creates_notes_then_skips_unchanged(conn, data_root):
    _seed_two_concepts(conn)

    result = sync_concept_notes(conn)
    assert result["created"] == 2
    assert result["updated"] == 0
    assert result["pruned"] == 0
    assert result["pending_prune"] == []

    concepts_dir = data_root / "concepts"
    assert (concepts_dir / f"{gstore.make_slug('개념 A')}.md").exists()
    assert (concepts_dir / f"{gstore.make_slug('개념 B')}.md").exists()

    result2 = sync_concept_notes(conn)
    assert result2["created"] == 0
    assert result2["updated"] == 0
    assert result2["skipped"] == 2


def test_sync_orphan_prune_gated_without_confirm(conn, data_root):
    _seed_two_concepts(conn)
    sync_concept_notes(conn)

    concepts_dir = data_root / "concepts"
    # concepts 집합에 없는 slug를 가진 orphan 노트 25개 생성 (임계값 20 초과)
    for i in range(25):
        (concepts_dir / f"orphan-{i}.md").write_text(
            "---\nslug: orphan\n---\n\nx", encoding="utf-8"
        )

    result = sync_concept_notes(conn, confirm_prune=False)
    assert result["pruned"] == 0
    assert len(result["pending_prune"]) == 25
    # 미삭제 확인
    assert (concepts_dir / "orphan-0.md").exists()


def test_curation_empty_table_projects_all(conn, data_root):
    """concept_curation이 비어있으면 v1과 동일 — 전량 투영(None)."""
    assert gstore.projected_slugs(conn) is None
    _seed_two_concepts(conn)
    result = sync_concept_notes(conn)
    assert result["created"] == 2


def test_curation_sync_only_creates_real_notes(conn, data_root):
    _seed_two_concepts(conn, relation="part_of")
    slug_a = gstore.make_slug("개념 A")
    slug_b = gstore.make_slug("개념 B")
    gstore.set_curation(conn, slug_a, "real")
    gstore.set_curation(conn, slug_b, "vocab")
    conn.commit()

    sync_concept_notes(conn)
    concepts_dir = data_root / "concepts"
    assert (concepts_dir / f"{slug_a}.md").exists()
    assert not (concepts_dir / f"{slug_b}.md").exists()


def test_curation_unprojected_dst_renders_plaintext(conn, data_root):
    """B가 vocab이면 A 노트에서 B로 가는 엣지는 위키링크가 아니라 평문."""
    _seed_two_concepts(conn, relation="part_of")
    slug_a = gstore.make_slug("개념 A")
    slug_b = gstore.make_slug("개념 B")
    gstore.set_curation(conn, slug_a, "real")
    gstore.set_curation(conn, slug_b, "vocab")
    conn.commit()

    sync_concept_notes(conn)
    text = (data_root / "concepts" / f"{slug_a}.md").read_text(encoding="utf-8")
    assert "- 개념 B" in text
    assert "[[" not in text


def test_curation_demoted_note_pruned_as_orphan(conn, data_root):
    """이미 노트가 있던 개념이 vocab으로 강등되면 orphan prune 대상이 된다."""
    _seed_two_concepts(conn, relation="part_of")
    slug_a = gstore.make_slug("개념 A")
    slug_b = gstore.make_slug("개념 B")

    sync_concept_notes(conn)  # 큐레이션 전: v1 모드로 A, B 둘 다 생성
    concepts_dir = data_root / "concepts"
    assert (concepts_dir / f"{slug_b}.md").exists()

    gstore.set_curation(conn, slug_a, "real")
    gstore.set_curation(conn, slug_b, "vocab")
    conn.commit()

    result = sync_concept_notes(conn)
    assert result["pruned"] == 1
    assert not (concepts_dir / f"{slug_b}.md").exists()


def test_prose_placeholder_vocab_dst_renders_plaintext(conn, vault_env):
    """산문 속 [[c:slug|표시명]]에서 대상이 vocab이면 평문 표시명으로 치환."""
    gstore.upsert_concept(conn, name="개념 A", description="A 설명")
    gstore.upsert_concept(conn, name="개념 B", description="B 설명")
    conn.commit()
    slug_a = gstore.make_slug("개념 A")
    slug_b = gstore.make_slug("개념 B")
    gstore.set_curation(conn, slug_a, "real", prose=f"이것은 [[c:{slug_b}|개념 B]] 참고.")
    gstore.set_curation(conn, slug_b, "vocab")
    conn.commit()

    text = render_concept_note(conn, slug_a)
    assert "이것은 개념 B 참고." in text
    assert "[[" not in text


def test_prose_placeholder_real_dst_renders_wikilink(conn, vault_env):
    """산문 속 [[c:slug|표시명]]에서 대상이 real이면 볼트 물리 경로 위키링크."""
    gstore.upsert_concept(conn, name="개념 A", description="A 설명")
    gstore.upsert_concept(conn, name="개념 B", description="B 설명")
    conn.commit()
    slug_a = gstore.make_slug("개념 A")
    slug_b = gstore.make_slug("개념 B")
    gstore.set_curation(conn, slug_a, "real", prose=f"[[c:{slug_b}|개념 B]] 참고.")
    gstore.set_curation(conn, slug_b, "real")
    conn.commit()

    text = render_concept_note(conn, slug_a)
    assert f"[[PKB/concepts/{slug_b}|개념 B]]" in text


def test_set_curation_prose_none_preserves_existing(conn):
    slug = gstore.make_slug("개념 A")
    gstore.upsert_concept(conn, name="개념 A", description="A 설명")
    gstore.set_curation(conn, slug, "real", prose="원본 산문")
    gstore.set_curation(conn, slug, "vocab")  # prose 생략 → 기존 값 보존
    conn.commit()

    assert gstore.get_prose(conn, slug) == "원본 산문"
    row = conn.execute(
        "SELECT label FROM concept_curation WHERE slug = ?", (slug,)
    ).fetchone()
    assert row["label"] == "vocab"


def test_render_idempotent_with_curation(conn, vault_env):
    a_id = gstore.upsert_concept(conn, name="개념 A", description="A 설명")
    b_id = gstore.upsert_concept(conn, name="개념 B", description="B 설명")
    gstore.add_edge(conn, a_id, b_id, "part_of")
    conn.commit()
    slug_a = gstore.make_slug("개념 A")
    slug_b = gstore.make_slug("개념 B")
    gstore.set_curation(conn, slug_a, "real", prose=f"[[c:{slug_b}|개념 B]] 참고.")
    gstore.set_curation(conn, slug_b, "vocab")
    conn.commit()

    assert render_concept_note(conn, slug_a) == render_concept_note(conn, slug_a)


def test_make_slug_idempotent():
    """특수문자 제거로 생긴 공백까지 정리 — make_slug(make_slug(x)) == make_slug(x)."""
    from pkb.graph.store import make_slug
    for name in [
        "Law of Visibility (법칙1 — 본 만큼만)",
        " 메시지 표현식",
        "plan → edit → test 사이클",
        "OWASP ASI Threats & Mitigations (T1~T17)",
        "BM25",
    ]:
        once = make_slug(name)
        assert once == make_slug(once), f"비멱등: {name!r} → {once!r}"
        assert once == once.strip() and "  " not in once, f"공백 잔존: {once!r}"
