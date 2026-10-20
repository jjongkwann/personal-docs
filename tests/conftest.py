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


@pytest.fixture(autouse=True)
def _isolated_graph_and_policy(tmp_path, monkeypatch):
    """.env의 실제 그래프 DB와 접근 정책을 테스트가 열지 않게 한다.

    ingest의 그래프 재정렬처럼 settings.graph_db_path를 그대로 쓰는 경로가 있어, 격리하지 않으면
    스키마 이관이 사용자의 실제 그래프에 적용된다. 그래프가 필요한 테스트는 이 값을 다시 덮어쓴다.
    """
    monkeypatch.setattr("pkb.config.settings.graph_db_path", str(tmp_path / ".graph" / "pkb_graph.sqlite"))
    monkeypatch.setattr("pkb.config.settings.access_policy", "")


# .env가 바꿔도 테스트는 코드 기본값으로 본다. 차원(EMBEDDING_DIMS)은 import 시점에 색인 매핑을 정하므로 그대로 둔다.
_EMBEDDING_FIELDS = (
    "embedding_model", "embedding_revision", "embedding_dtype", "embedding_trust_remote_code", "embedding_task",
    "embedding_query_prompt", "embedding_document_prompt", "embedding_normalize", "embedding_text_only",
    "media_embedding_model",
)


@pytest.fixture(autouse=True)
def _default_embedding_settings(monkeypatch):
    """로컬 .env의 임베딩 모델 설정(예: EmbeddingGemma 프롬프트·bfloat16)에 테스트 결과가 흔들리지 않게 한다."""
    from pkb.config import Settings, settings

    defaults = Settings(_env_file=None)
    for field in _EMBEDDING_FIELDS:
        monkeypatch.setattr(settings, field, getattr(defaults, field))
