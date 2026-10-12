"""모든 테스트 공용 격리."""

import pytest

from pkb import search_log


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    """검색·변경 로그가 실제 코퍼스(DATA_ROOT/.logs)에 쌓이지 않게 임시 폴더로 돌린다."""
    log_dir = tmp_path / ".logs"
    monkeypatch.setattr(search_log, "LOG_DIR", log_dir)
    monkeypatch.setattr(search_log, "LOG_FILE", log_dir / "search.jsonl")
    monkeypatch.setattr(search_log, "CHANGES_FILE", log_dir / "changes.jsonl")
    monkeypatch.setattr(search_log, "LAST_SYNC_FILE", log_dir / "last_sync.json")
