"""embedding_fingerprint 기반 재임베딩 판정 단위 테스트.

content_hash는 본문 동일성(그래프 pending)만 추적하고, 벡터 재사용은
모델·전처리·임베딩 입력을 합친 fingerprint가 판정한다 — 모델 교체나
prefix 토글 시 본문이 같아도 재임베딩돼야 한다.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pkb.config import settings
from pkb.ingest import _content_hash, embed_input, embedding_fingerprint, ingest_files

DOC_ID = "data/test/fp.md"


@pytest.fixture(autouse=True)
def _no_prefix(monkeypatch):
    # 판정 로직 검증이 목적 — 임베딩 입력을 content 단독으로 고정해 prefix 기본값과 격리
    monkeypatch.setattr(settings, "embed_context_prefix", False)


def _chunk(idx: int, content: str) -> dict:
    chunk = {
        "content": content,
        "content_hash": _content_hash(content),
        "source_path": DOC_ID,
        "category": "test",
        "doc_id": DOC_ID,
        "chunk_index": idx,
        "section_path": f"sec{idx}",
        "title": "제목",
        "tags": [],
        "date_modified": "2026-07-20",
        "language": "ko",
    }
    chunk["embedding_fingerprint"] = embedding_fingerprint(chunk)
    return chunk


def _existing_hit(chunk: dict) -> dict:
    src = {k: v for k, v in chunk.items() if k != "content"}
    return {"_source": src}


def _setup(monkeypatch, es, new_chunks, embed_calls):
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.ingest.process_file", lambda *a, **kw: new_chunks)

    def fake_embed(texts):
        embed_calls.append(list(texts))
        return [[float(len(t))] for t in texts]

    monkeypatch.setattr("pkb.embeddings.embed", fake_embed)


def test_fingerprint_changes_with_model(monkeypatch):
    c = _chunk(0, "같은 본문")
    monkeypatch.setattr(settings, "embedding_model", "other-model")
    assert embedding_fingerprint(c) != c["embedding_fingerprint"]
    assert c["content_hash"] == _content_hash("같은 본문")  # content_hash는 불변


def test_prefix_toggle_changes_input_and_fingerprint(monkeypatch):
    monkeypatch.setattr(settings, "embed_context_prefix", False)
    c = _chunk(0, "본문")
    assert embed_input(c) == "본문"
    monkeypatch.setattr(settings, "embed_context_prefix", True)
    assert embed_input(c) == "제목\nsec0\n본문"
    assert embedding_fingerprint(c) != c["embedding_fingerprint"]


def test_missing_fingerprint_reembeds_unchanged_content(monkeypatch):
    """fingerprint 없는 구 청크는 본문이 같아도 재임베딩 (백필 겸 안전 규칙)."""
    old = _chunk(0, "aaa")
    del old["embedding_fingerprint"]
    new = [_chunk(0, "aaa")]

    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [_existing_hit(old)]}}
    es.bulk.return_value = {"errors": False}
    embed_calls: list[list[str]] = []
    _setup(monkeypatch, es, new, embed_calls)

    stats = ingest_files([Path("dummy.md")], Path("."))

    assert embed_calls == [["aaa"]]
    assert stats["reused"] == 0
    assert stats["embedded"] == 1
    es.mget.assert_not_called()  # 복사 경로도 타지 않음


def test_model_change_blocks_vector_copy(monkeypatch):
    """모델이 바뀌면 같은 본문이 다른 슬롯에 있어도 구벡터를 복사하지 않는다."""
    old = [_chunk(0, "aaa")]  # 현재 모델 fingerprint로 저장된 상태
    monkeypatch.setattr(settings, "embedding_model", "new-model")
    new = [_chunk(0, "new"), _chunk(1, "aaa")]  # aaa가 슬롯 1로 이동

    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [_existing_hit(c) for c in old]}}
    es.bulk.return_value = {"errors": False}
    embed_calls: list[list[str]] = []
    _setup(monkeypatch, es, new, embed_calls)

    stats = ingest_files([Path("dummy.md")], Path("."))

    assert stats["moved"] == 0
    assert sorted(embed_calls[0]) == ["aaa", "new"]  # 둘 다 새 모델로 임베딩
    es.mget.assert_not_called()
