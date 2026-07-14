"""log_change 변경 저널 라인 포맷 검증. ES 없이 tmp 디렉토리로 검증."""

from __future__ import annotations

import json

import pkb.search_log as sl


def test_log_change_appends_jsonl_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sl, "CHANGES_FILE", tmp_path / "changes.jsonl")

    sl.log_change("archive", "data/study/x.md", chunks=3, reason="outdated")
    sl.log_change("purge", "*", chunks=7)

    lines = (tmp_path / "changes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["op"] == "archive"
    assert first["doc_id"] == "data/study/x.md"
    assert first["chunks"] == 3
    assert first["reason"] == "outdated"
    assert "ts" in first

    second = json.loads(lines[1])
    assert second["op"] == "purge"
    assert second["doc_id"] == "*"


def test_log_change_swallows_write_failure(tmp_path, monkeypatch):
    # 변이가 로그 실패로 죽으면 안 됨 — 쓸 수 없는 경로여도 예외가 새지 않는다
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(sl, "LOG_DIR", blocker / "sub")  # 파일 하위라 mkdir 실패
    monkeypatch.setattr(sl, "CHANGES_FILE", blocker / "sub" / "changes.jsonl")

    sl.log_change("delete", "data/x.md", chunks=1)  # 예외 없이 통과해야 함
