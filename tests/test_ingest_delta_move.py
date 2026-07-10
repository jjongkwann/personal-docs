"""ingest_files 델타의 슬롯 이동(임베딩 복사) 경로 단위 테스트.

시나리오: 문서 맨 앞에 청크 삽입 → 기존 청크들의 chunk_index가 전부 시프트.
수정 전에는 전량 재임베딩됐지만, 이제 같은 content_hash가 다른 슬롯에 있으면
임베딩을 복사(moved)하고 신규 청크만 임베딩해야 한다.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from pkb.ingest import _content_hash, ingest_files

DOC_ID = "data/test/delta_move.md"


def _chunk(idx: int, content: str) -> dict:
    return {
        "content": content,
        "content_hash": _content_hash(content),
        "source_path": DOC_ID,
        "category": "test",
        "doc_id": DOC_ID,
        "chunk_index": idx,
        "section_path": f"sec{idx}",
        "title": "t",
        "tags": [],
        "date_modified": "2026-07-05",
        "language": "ko",
    }


def _existing_hit(chunk: dict) -> dict:
    src = {k: v for k, v in chunk.items() if k != "content"}
    return {"_source": src}


def _setup(monkeypatch, es, new_chunks, embed_calls):
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr(
        "pkb.ingest.process_file",
        lambda *a, **kw: new_chunks,
    )

    def fake_embed(texts):
        embed_calls.append(list(texts))
        return [[float(len(t))] for t in texts]

    monkeypatch.setattr("pkb.embeddings.embed", fake_embed)


def test_front_insert_moves_shifted_chunks_without_reembedding(monkeypatch):
    old = [_chunk(0, "aaa"), _chunk(1, "bbb"), _chunk(2, "ccc")]
    old_vecs = {0: [10.0], 1: [11.0], 2: [12.0]}
    # 새 파일: 맨 앞에 "new" 삽입 → 기존 3개가 1,2,3으로 시프트
    new = [_chunk(0, "new"), _chunk(1, "aaa"), _chunk(2, "bbb"), _chunk(3, "ccc")]

    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [_existing_hit(c) for c in old]}}
    es.mget.return_value = {
        "docs": [
            {"found": True, "_source": {"chunk_index": i, "embedding": v}}
            for i, v in old_vecs.items()
        ]
    }
    es.bulk.return_value = {"errors": False}

    embed_calls: list[list[str]] = []
    _setup(monkeypatch, es, new, embed_calls)

    stats = ingest_files([Path("dummy.md")], Path("."))

    # 신규 청크 1개만 임베딩, 시프트된 3개는 이동
    assert embed_calls == [["new"]]
    assert stats["moved"] == 3
    assert stats["embedded"] == 1  # 슬롯 0은 기존 슬롯이므로 re-embedded로 집계
    assert stats["added"] == 1  # 슬롯 3만 신규 슬롯
    assert stats["reused"] == 0
    assert stats["deleted"] == 0

    # mget이 구 슬롯 임베딩을 요청했는지
    mget_ids = es.mget.call_args.kwargs["ids"]
    assert sorted(mget_ids) == [f"{DOC_ID}_0", f"{DOC_ID}_1", f"{DOC_ID}_2"]

    # bulk에 4개 슬롯 전부 index되고, 이동 청크는 기존 벡터를 그대로 가짐
    ops = es.bulk.call_args.kwargs["operations"]
    indexed = {
        ops[i + 1]["chunk_index"]: ops[i + 1]
    for i in range(0, len(ops), 2) if "index" in ops[i]
    }
    assert set(indexed) == {0, 1, 2, 3}
    assert indexed[1]["embedding"] == old_vecs[0]  # aaa: 0→1 이동
    assert indexed[2]["embedding"] == old_vecs[1]  # bbb: 1→2 이동
    assert indexed[3]["embedding"] == old_vecs[2]  # ccc: 2→3 이동
    assert indexed[0]["embedding"] == [3.0]  # "new" 신규 임베딩


def test_missing_source_embedding_falls_back_to_reembed(monkeypatch):
    old = [_chunk(0, "aaa")]
    new = [_chunk(0, "new"), _chunk(1, "aaa")]

    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [_existing_hit(c) for c in old]}}
    # 구 청크에 임베딩이 없음 (마이그레이션 이전 데이터 등)
    es.mget.return_value = {"docs": [{"found": True, "_source": {"chunk_index": 0}}]}
    es.bulk.return_value = {"errors": False}

    embed_calls: list[list[str]] = []
    _setup(monkeypatch, es, new, embed_calls)

    stats = ingest_files([Path("dummy.md")], Path("."))

    assert stats["moved"] == 0
    assert sorted(embed_calls[0]) == ["aaa", "new"]  # 둘 다 임베딩
    assert stats["embedded"] + stats["added"] == 2


def test_pure_append_reuses_untouched_slots(monkeypatch):
    old = [_chunk(0, "aaa"), _chunk(1, "bbb")]
    new = [_chunk(0, "aaa"), _chunk(1, "bbb"), _chunk(2, "new")]

    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [_existing_hit(c) for c in old]}}
    es.bulk.return_value = {"errors": False}

    embed_calls: list[list[str]] = []
    _setup(monkeypatch, es, new, embed_calls)

    stats = ingest_files([Path("dummy.md")], Path("."))

    assert stats["reused"] == 2
    assert stats["moved"] == 0
    assert stats["added"] == 1
    assert embed_calls == [["new"]]
    es.mget.assert_not_called()
