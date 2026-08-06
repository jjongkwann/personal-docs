"""아카이브 상태 frontmatter 왕복 — 파일이 SSOT, reindex/재인제스트에도 archived_at 유지.

ES 없이 검증: 헬퍼 왕복 + process_file 청크 필드 + 도구 레벨(ingest_files monkeypatch).
"""

from __future__ import annotations

import pytest

from pkb.documents import insert_archive_frontmatter as _insert_archive_frontmatter
from pkb.documents import strip_archive_frontmatter as _strip_archive_frontmatter
from pkb.ingest import _diff_metadata, parse_frontmatter, process_file
from pkb.mcp_server import archive_document as _archive_document
from pkb.mcp_server import restore_document as _restore_document
from pkb.retrieve import _lifecycle_filter

# MCPServer @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근.
archive_document = getattr(_archive_document, "fn", _archive_document)
restore_document = getattr(_restore_document, "fn", _restore_document)

TS = "2026-07-11T00:00:00+00:00"

_STATS = {
    "files": 1, "reused": 0, "moved": 0, "embedded": 0,
    "added": 0, "metadata_updated": 1, "deleted": 0,
}


@pytest.fixture
def data_root(monkeypatch, tmp_path):
    """tmp_path/data를 코퍼스 루트로 강제 (.env의 DATA_ROOT 무시)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pkb.config.settings.data_root", "data")
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def fake_ingest(monkeypatch):
    """ingest_files를 스텁으로 대체 — 호출된 파일 목록만 기록 (ES/임베딩 미접근)."""
    calls: list[list] = []

    def _fake(files, base_dir, **kwargs):
        calls.append(list(files))
        return dict(_STATS)

    monkeypatch.setattr("pkb.ingest.ingest_files", _fake)
    return calls


# ---------- 헬퍼 왕복 (삽입 → 제거) ----------


def test_insert_and_strip_roundtrip_preserves_original():
    original = "---\ntitle: 노트\ntags: [a, b]\n---\n\n본문\n"
    inserted = _insert_archive_frontmatter(original, TS, "오래된 내용: v2로 대체")
    assert f"archived_at: {TS}\n" in inserted
    # reason은 json.dumps 인용 — 콜론 포함 문자열도 YAML 안전
    assert 'archive_reason: "오래된 내용: v2로 대체"\n' in inserted
    # 기존 키 순서·스타일 보존 (YAML 재직렬화 금지)
    assert "title: 노트\ntags: [a, b]" in inserted
    assert _strip_archive_frontmatter(inserted) == original


def test_insert_creates_block_when_missing_and_strip_restores():
    original = "# 제목\n\n본문\n"
    inserted = _insert_archive_frontmatter(original, TS, "")
    assert inserted.startswith(f"---\narchived_at: {TS}\n---\n")
    assert "archive_reason" not in inserted
    assert _strip_archive_frontmatter(inserted) == original


def test_reinsert_does_not_duplicate_lines():
    once = _insert_archive_frontmatter("---\ntitle: x\n---\n본문\n", TS, "r1")
    twice = _insert_archive_frontmatter(once, "2026-08-01T00:00:00+00:00", "r2")
    assert twice.count("archived_at:") == 1
    assert twice.count("archive_reason:") == 1


def test_insert_normalizes_missing_trailing_newline():
    # 닫는 '---'가 개행 없이 EOF — 정규화 없이는 parse_frontmatter가 블록을 인식하지
    # 못해 archived_at이 본문으로 색인되고 아카이브가 적용되지 않는다.
    inserted = _insert_archive_frontmatter("---\ntitle: x\n---", TS, "")
    fm, _ = parse_frontmatter(inserted)
    assert fm.get("archived_at") is not None


# ---------- process_file: frontmatter → 청크 필드 ----------


def test_process_file_sets_archive_chunk_fields(data_root):
    note = data_root / "study" / "note.md"
    note.parent.mkdir()
    note.write_text(
        f"---\ntitle: 노트\narchived_at: {TS}\narchive_reason: 오래됨\n---\n\n"
        "본문입니다. 아카이브 왕복 테스트.\n",
        encoding="utf-8",
    )
    chunks = process_file(note, data_root, doc_id_prefix="data/")
    assert chunks
    for c in chunks:
        assert c["archived_at"] == TS
        assert c["archive_reason"] == "오래됨"


def test_process_file_ignores_non_str_archive_reason(data_root):
    note = data_root / "study" / "bad.md"
    note.parent.mkdir()
    note.write_text(
        f"---\narchived_at: {TS}\narchive_reason: 123\n---\n\n본문.\n",
        encoding="utf-8",
    )
    chunks = process_file(note, data_root, doc_id_prefix="data/")
    assert chunks[0]["archived_at"] == TS
    assert "archive_reason" not in chunks[0]


def test_reindex_after_es_delete_keeps_archived_at(data_root):
    """reindex 시뮬레이션: ES 문서 전량 삭제 후 원본만으로 재인제스트해도 상태 유지."""
    note = data_root / "study" / "note.md"
    note.parent.mkdir()
    original = "---\ntitle: 노트\n---\n\n본문입니다.\n"
    note.write_text(_insert_archive_frontmatter(original, TS, "구버전"), encoding="utf-8")

    # ES가 비어 있다고 가정 — process_file은 파일만 읽으므로 청크에 상태가 그대로 실린다.
    chunks = process_file(note, data_root, doc_id_prefix="data/")
    assert all(c["archived_at"] == TS for c in chunks)
    assert all(c["archive_reason"] == "구버전" for c in chunks)


def test_diff_metadata_reports_archive_state_change():
    # 아카이브: 필드 추가 → 메타-only partial update 경로로 전파
    diff = _diff_metadata({}, {"archived_at": TS, "archive_reason": "r"})
    assert diff == {"archived_at": TS, "archive_reason": "r"}
    # 필드 제거(None)는 diff에 미포함 — frontmatter 마커 없는 파일의 재인제스트가
    # ES-only 아카이브를 지우면 안 됨. ES 필드 해제는 restore_document의 명시 호출 몫.
    diff = _diff_metadata({"archived_at": TS, "archive_reason": "r"}, {})
    assert diff == {}


# ---------- 도구 레벨 (ES 없이 ingest_files 스텁) ----------


def test_archive_tool_writes_frontmatter_and_reingests(data_root, fake_ingest, monkeypatch):
    logged: list[tuple] = []
    monkeypatch.setattr(
        "pkb.search_log.log_change", lambda op, doc_id, **kw: logged.append((op, doc_id))
    )
    note = data_root / "study" / "note.md"
    note.parent.mkdir()
    note.write_text("---\ntitle: 노트\n---\n\n본문\n", encoding="utf-8")

    result = archive_document("data/study/note.md", reason="옛 문서")
    assert "아카이브 완료" in result
    assert "사유: 옛 문서" in result
    text = note.read_text(encoding="utf-8")
    assert "archived_at:" in text
    assert 'archive_reason: "옛 문서"' in text
    assert fake_ingest == [[note]]
    # frontmatter 분기는 store.archive_document를 안 타므로 changes.jsonl 기록을 직접 남긴다
    assert logged == [("archive", "data/study/note.md")]


def test_restore_tool_strips_frontmatter_and_reingests(data_root, fake_ingest, monkeypatch):
    note = data_root / "study" / "note.md"
    note.parent.mkdir()
    original = "---\ntitle: 노트\n---\n\n본문\n"
    note.write_text(_insert_archive_frontmatter(original, TS, "옛 문서"), encoding="utf-8")

    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    restored: list[str] = []
    monkeypatch.setattr(
        "pkb.store.restore_document", lambda es, doc_id: restored.append(doc_id) or 1
    )

    result = restore_document("data/study/note.md")
    assert "복구 완료" in result
    assert note.read_text(encoding="utf-8") == original
    assert fake_ingest == [[note]]
    # 재인제스트는 아카이브 필드의 None을 전파하지 않으므로 ES 해제는 명시 restore 호출로
    assert restored == ["data/study/note.md"]


def test_archive_and_restore_fall_back_to_es_when_file_missing(data_root, monkeypatch):
    called: dict = {}
    monkeypatch.setattr("pkb.store.get_client", lambda: object())

    def fake_archive(es, doc_id, reason=None):
        called["archive"] = doc_id
        return 3

    def fake_restore(es, doc_id):
        called["restore"] = doc_id
        return 3

    monkeypatch.setattr("pkb.store.archive_document", fake_archive)
    monkeypatch.setattr("pkb.store.restore_document", fake_restore)

    assert "3개 청크" in archive_document("data/study/missing.md")
    assert called["archive"] == "data/study/missing.md"
    assert "3개 청크" in restore_document("data/study/missing.md")
    assert called["restore"] == "data/study/missing.md"


def test_restore_falls_back_to_es_when_no_frontmatter_marker(data_root, monkeypatch):
    """파일은 있지만 frontmatter에 기록이 없는 경우 — 구 ES-only 아카이브 복구 경로."""
    note = data_root / "study" / "note.md"
    note.parent.mkdir()
    note.write_text("---\ntitle: 노트\n---\n\n본문\n", encoding="utf-8")

    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    monkeypatch.setattr("pkb.store.restore_document", lambda es, doc_id: 2)

    assert "2개 청크" in restore_document("data/study/note.md")


# ---------- 검색 제외 필터 셰이프 ----------


def test_lifecycle_filter_still_excludes_archived():
    filters = _lifecycle_filter(include_archived=False)
    assert {"bool": {"must_not": {"exists": {"field": "archived_at"}}}} in filters
