"""pkb reembed: 새 물리 색인으로 옮기며 벡터·fingerprint만 바꾸기, 재실행, 안전장치. 메모리 ES로 검증."""

from __future__ import annotations

import pytest

from pkb import reembed
from pkb.config import settings
from pkb.ingest import embed_input, embedding_fingerprint, process_file

SOURCE, TARGET = "pkb_v1", "pkb_v2"


class _Indices:
    def __init__(self, es: FakeES):
        self.es = es

    def exists(self, index):
        return index in self.es.data or index in self.es.aliases

    def exists_alias(self, name):
        return name in self.es.aliases

    def get_alias(self, name):
        return {self.es.aliases[name]: {}}

    def create(self, index, body):
        self.es.data[index] = {}
        self.es.mappings[index] = body["mappings"]

    def get_mapping(self, index):
        return {index: {"mappings": self.es.mappings[index]}}


class FakeES:
    """reembed가 쓰는 search(search_after)·mget·bulk만 흉내 낸다. alias는 물리 색인 하나를 가리킨다."""

    def __init__(self, data: dict[str, dict[str, dict]], aliases: dict[str, str] | None = None):
        self.data = data
        self.aliases = aliases or {}
        self.mappings = {name: {"properties": {"embedding": {"dims": settings.embedding_dims}}} for name in data}
        self.indices = _Indices(self)
        self.bulk_calls: list[list[dict]] = []

    def options(self, **_):
        return self

    @staticmethod
    def _filter(source, source_excludes=(), source_includes=None):
        if source_includes is not None:
            return {k: v for k, v in source.items() if k in source_includes}
        return {k: v for k, v in source.items() if k not in source_excludes}

    def search(self, index, size, sort, search_after=None, query=None, **source):
        docs = self.data[self.aliases.get(index, index)]
        hits = sorted(
            ({"_id": _id, "_source": self._filter(src, **source), "sort": [src["doc_id"], src["chunk_index"]]}
             for _id, src in docs.items()),
            key=lambda hit: hit["sort"],
        )
        if search_after is not None:
            hits = [hit for hit in hits if hit["sort"] > search_after]
        return {"hits": {"hits": hits[:size]}}

    def mget(self, index, ids, **source):
        docs = self.data[self.aliases.get(index, index)]
        return {"docs": [
            {"_id": _id, "found": True, "_source": self._filter(docs[_id], **source)} if _id in docs
            else {"_id": _id, "found": False}
            for _id in ids
        ]}

    def bulk(self, operations, refresh=False):
        self.bulk_calls.append(operations)
        it = iter(operations)
        for action in it:
            (op, meta), = action.items()
            if op == "index":
                self.data[meta["_index"]][meta["_id"]] = next(it)
            else:
                self.data[meta["_index"]].pop(meta["_id"], None)
        return {"errors": False}


def _chunk(doc_id: str, index: int, content: str, **meta) -> dict:
    return {"doc_id": doc_id, "chunk_index": index, "content": content, "title": "제목",
            "section_path": "절", "category": "study", "content_hash": f"h-{content}",
            "embedding": [0.0, 1.0], "embedding_fingerprint": "old-model", **meta}


def _source(*chunks: dict) -> dict[str, dict]:
    return {f"{c['doc_id']}_{c['chunk_index']}": c for c in chunks}


@pytest.fixture
def embedded(monkeypatch):
    """임베딩 호출 기록. 벡터는 입력 길이로 만들어 어떤 텍스트였는지 확인할 수 있게 한다."""
    calls: list[list[str]] = []

    def embed(texts, pad_to_multiple_of=0):
        assert pad_to_multiple_of == reembed._PAD_TO_MULTIPLE_OF
        calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]

    monkeypatch.setattr("pkb.embeddings.embed_documents", embed)
    return calls


@pytest.fixture
def backfilled(monkeypatch):
    """문서 색인 백필이 어느 청크 색인을 대상으로 불렸는지."""
    seen: list[str] = []
    monkeypatch.setattr(
        reembed, "backfill_document_index", lambda es: seen.append(settings.es_index) or {"documents": 1}
    )
    return seen


@pytest.fixture(autouse=True)
def _alias(monkeypatch):
    monkeypatch.setattr(settings, "es_index", "pkb_current")


def test_copies_chunks_with_new_vector_and_fingerprint(embedded, backfilled):
    a0, a1 = _chunk("data/a.md", 0, "첫 청크"), _chunk("data/a.md", 1, "둘째 청크", tags=["x"])
    es = FakeES({SOURCE: _source(a0, a1)}, aliases={"pkb_current": SOURCE})

    result = reembed.reembed_index(es, source="pkb_current", target=TARGET)

    assert result.complete and result.embedded == 2 and result.scanned == 2
    assert embedded == [[embed_input(a0), embed_input(a1)]]  # 제목·절 prefix 포함, 한 번에
    copied = es.data[TARGET]["data/a.md_1"]
    assert copied["embedding"] == [float(len(embed_input(a1))), 1.0]
    assert copied["embedding_fingerprint"] == embedding_fingerprint(a1)
    assert {k: v for k, v in copied.items() if k not in ("embedding", "embedding_fingerprint")} == {
        k: v for k, v in a1.items() if k not in ("embedding", "embedding_fingerprint")
    }
    assert es.data[SOURCE]["data/a.md_1"] == a1  # 원본은 그대로
    assert es.mappings[TARGET]["properties"]["embedding"]["dims"] == settings.embedding_dims
    assert backfilled == [TARGET] and settings.es_index == "pkb_current"


def test_rerun_skips_done_chunks_and_catches_up_with_source_changes(embedded, backfilled):
    a0, a1, b0 = _chunk("data/a.md", 0, "하나"), _chunk("data/a.md", 1, "둘"), _chunk("data/b.md", 0, "셋")
    es = FakeES({SOURCE: _source(a0, a1, b0)})
    reembed.reembed_index(es, source=SOURCE, target=TARGET)
    embedded.clear()

    again = reembed.reembed_index(es, source=SOURCE, target=TARGET)
    assert again.unchanged == 3 and again.embedded == again.copied == again.deleted == 0
    assert embedded == []

    vector_b0 = es.data[TARGET]["data/b.md_0"]["embedding"]
    es.data[SOURCE]["data/a.md_0"] = _chunk("data/a.md", 0, "하나 고침")  # 본문 변경 → 다시 임베딩
    es.data[SOURCE]["data/b.md_0"] = {**b0, "archived_at": "2026-10-15"}  # 메타데이터만 → 벡터 복사
    del es.data[SOURCE]["data/a.md_1"]  # 원본에서 사라짐 → 대상에서 삭제
    catch_up = reembed.reembed_index(es, source=SOURCE, target=TARGET)

    assert (catch_up.embedded, catch_up.copied, catch_up.deleted) == (1, 1, 1)
    assert embedded == [[embed_input(es.data[SOURCE]["data/a.md_0"])]]
    assert es.data[TARGET]["data/b.md_0"]["archived_at"] == "2026-10-15"
    assert es.data[TARGET]["data/b.md_0"]["embedding"] == vector_b0
    assert set(es.data[TARGET]) == {"data/a.md_0", "data/b.md_0"}


def test_media_chunks_keep_their_vector_and_have_no_fingerprint(embedded, backfilled, monkeypatch):
    """이미지·오디오 청크의 벡터는 미디어 모델이 만들었다. content(설명)로 다시 임베딩하지 않는다."""
    monkeypatch.setattr(settings, "media_embedding_model", "omni")
    text = _chunk("data/a.md", 0, "본문")
    image = _chunk("data/a.png", 0, "사진 설명", source_type="image", media_model="omni", embedding=[0.3, 0.4])
    del image["embedding_fingerprint"]
    es = FakeES({SOURCE: _source(text, image)}, aliases={"pkb_current": SOURCE})

    first = reembed.reembed_index(es, source="pkb_current", target=TARGET)

    assert (first.embedded, first.copied) == (1, 1)
    assert embedded == [[embed_input(text)]]
    assert es.data[TARGET]["data/a.png_0"] == image
    assert reembed.reembed_index(es, source="pkb_current", target=TARGET).unchanged == 2


def test_media_chunks_of_another_media_model_are_left_for_media_sync(embedded, backfilled, monkeypatch):
    """미디어 모델을 바꾸면 옛 미디어 벡터는 공간·차원이 달라 옮기지 않는다. 대상에 남은 것도 지운다."""
    monkeypatch.setattr(settings, "media_embedding_model", "new-media")
    text = _chunk("data/a.md", 0, "본문")
    image = _chunk("data/a.png", 0, "사진 설명", source_type="image", media_model="omni", embedding=[0.3, 0.4])
    del image["embedding_fingerprint"]
    es = FakeES({SOURCE: _source(text, image), TARGET: {"data/a.png_0": image}})

    result = reembed.reembed_index(es, source=SOURCE, target=TARGET)

    assert (result.embedded, result.copied, result.media_dropped, result.deleted) == (1, 0, 1, 1)
    assert set(es.data[TARGET]) == {"data/a.md_0"}


def test_max_batches_stops_before_finishing(embedded, backfilled):
    chunks = [_chunk(f"data/{i}.md", 0, f"본문 {i}") for i in range(5)]
    es = FakeES({SOURCE: _source(*chunks)})

    first = reembed.reembed_index(es, source=SOURCE, target=TARGET, batch_size=2, max_batches=1)
    assert not first.complete and first.embedded == 2
    assert backfilled == []  # 끝나기 전에는 문서 색인을 만들지 않는다

    rest = reembed.reembed_index(es, source=SOURCE, target=TARGET, batch_size=2)
    assert rest.complete and rest.unchanged == 2 and rest.embedded == 3
    assert len(es.data[TARGET]) == 5


def test_dry_run_counts_without_writing_or_embedding(embedded, backfilled):
    es = FakeES({SOURCE: _source(_chunk("data/a.md", 0, "하나"), _chunk("data/b.md", 0, "둘"))})

    result = reembed.reembed_index(es, source=SOURCE, target=TARGET, dry_run=True)

    assert result.embedded == 2 and not result.complete
    assert TARGET not in es.data and es.bulk_calls == [] and embedded == [] and backfilled == []


@pytest.mark.parametrize(
    ("target", "message"),
    [("pkb_current", "alias"), (SOURCE, "가리키는"), ("pkb_old", "차원")],
)
def test_refuses_unsafe_targets(target, message, embedded, backfilled):
    es = FakeES({SOURCE: _source(_chunk("data/a.md", 0, "하나")), "pkb_old": {}}, aliases={"pkb_current": SOURCE})
    es.mappings["pkb_old"]["properties"]["embedding"]["dims"] = settings.embedding_dims + 1

    with pytest.raises(ValueError, match=message):
        reembed.reembed_index(es, source="pkb_current", target=target)
    assert es.bulk_calls == []


def test_written_fingerprint_matches_ingest_under_new_settings(tmp_path, monkeypatch, embedded, backfilled):
    """전환 뒤 sync가 재임베딩하지 않으려면 대상의 fingerprint가 새 설정의 process_file 값과 같아야 한다."""
    note = tmp_path / "study" / "note.md"
    note.parent.mkdir()
    note.write_text("# 노트\n\n## 절\n\n" + "본문 문장입니다. " * 40, encoding="utf-8")
    old_chunks = process_file(note, tmp_path, doc_id_prefix="data/")
    es = FakeES({SOURCE: {f"{c['doc_id']}_{c['chunk_index']}": {**c, "embedding": [0.0, 1.0]} for c in old_chunks}})

    for field, value in {
        "embedding_model": "jinaai/jina-embeddings-v5-text-small",
        "embedding_task": "retrieval",
        "embedding_document_prompt": "document",
        "embedding_normalize": True,
    }.items():
        monkeypatch.setattr(settings, field, value)
    reembed.reembed_index(es, source=SOURCE, target=TARGET)

    new_chunks = process_file(note, tmp_path, doc_id_prefix="data/")
    assert [c["embedding_fingerprint"] for c in new_chunks] != [c["embedding_fingerprint"] for c in old_chunks]
    assert {f"{c['doc_id']}_{c['chunk_index']}": c["embedding_fingerprint"] for c in new_chunks} == {
        _id: doc["embedding_fingerprint"] for _id, doc in es.data[TARGET].items()
    }


def test_cli_exit_codes(monkeypatch):
    from typer.testing import CliRunner

    from pkb.cli import app

    outcomes = iter([
        reembed.ReembedResult(scanned=2, embedded=2),
        reembed.ReembedResult(scanned=4, embedded=2, unchanged=2, complete=True, documents={"documents": 3}),
        ValueError("대상은 alias가 아니라 새 물리 인덱스 이름이어야 합니다: pkb_current"),
    ])

    def fake(es, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("pkb.reembed.reembed_index", fake)
    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    runner = CliRunner()

    stopped = runner.invoke(app, ["reembed", "--target", TARGET, "--max-batches", "1"])
    assert stopped.exit_code == 3, stopped.output
    done = runner.invoke(app, ["reembed", "--target", TARGET])
    assert done.exit_code == 0 and f"index-switch {TARGET}" in done.output
    refused = runner.invoke(app, ["reembed", "--target", "pkb_current"])
    assert refused.exit_code == 1 and "alias" in refused.output
