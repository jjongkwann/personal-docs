"""청크 색인을 현재 임베딩 설정으로 새 물리 색인에 옮긴다 (`pkb reembed`).

임베딩 모델을 바꿀 때 메모리가 작은 서버 대신 GPU가 있는 Mac에서 SSH 터널로 같은 ES에 붙어
돌린다. _id·본문·메타데이터는 그대로 두고 embedding과 embedding_fingerprint만 바꾼다.
fingerprint는 ingest가 같은 설정에서 계산하는 값이라, 전환 뒤 sync는 이 청크를 다시 임베딩하지 않는다.
media_model이 있는 청크(이미지·오디오)는 벡터를 미디어 모델이 만들었고 content는 설명뿐이라 그대로 옮긴다.

진행 상태를 따로 저장하지 않는다. 다시 실행하면 원본을 처음부터 읽고, 대상에 같은 내용이 이미
있는 청크는 건너뛴다. 원본이 그사이 바뀌었으면 바뀐 청크만 다시 임베딩하고 사라진 청크는 지운다.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.ingest import embed_input, embedding_fingerprint
from pkb.store import INDEX_SETTINGS, _bulk, backfill_document_index

# SSH 터널 너머 ES에서도 끊김·시간 초과를 같은 요청으로 다시 보낸다
_RETRY_OPTIONS = {"request_timeout": 120, "max_retries": 5, "retry_on_timeout": True}
# 청크 토큰 길이를 이 배수로 맞춰 MPS 메모리가 묶음마다 늘지 않게 한다 (embeddings._padded_to_multiple)
_PAD_TO_MULTIPLE_OF = 128
_DELETE_BATCH = 1000


@dataclass
class ReembedResult:
    scanned: int = 0
    unchanged: int = 0
    embedded: int = 0  # 새로 임베딩한(dry_run이면 임베딩할) 청크
    copied: int = 0  # 벡터를 그대로 옮긴 청크 — 메타데이터만 바뀐 청크(대상 벡터)와 미디어 청크(원본 벡터)
    deleted: int = 0  # 원본에 없어 대상에서 지운 청크
    embed_seconds: float = 0.0
    complete: bool = False
    documents: dict | None = None  # 문서 색인 백필 결과


def _physical_indices(es: Elasticsearch, name: str) -> set[str]:
    if es.indices.exists_alias(name=name):
        return set(es.indices.get_alias(name=name))
    return {name}


def _check_indices(es: Elasticsearch, source: str, target: str) -> None:
    if not es.indices.exists(index=source):
        raise ValueError(f"원본 색인이 없습니다: {source}")
    if target == settings.es_index or es.indices.exists_alias(name=target):
        raise ValueError(f"대상은 alias가 아니라 새 물리 색인 이름이어야 합니다: {target}")
    # 원본이나 읽기 alias 뒤의 물리 색인에 쓰면 운영 색인의 벡터가 섞인다
    if target in _physical_indices(es, source) | _physical_indices(es, settings.es_index):
        raise ValueError(f"대상이 원본 또는 '{settings.es_index}'가 가리키는 색인입니다: {target}")
    if es.indices.exists(index=target):
        mapping = es.indices.get_mapping(index=target)[target]["mappings"]["properties"]
        dims = mapping.get("embedding", {}).get("dims")
        if dims != settings.embedding_dims:
            raise ValueError(f"대상 색인의 벡터 차원 {dims}이 EMBEDDING_DIMS {settings.embedding_dims}와 다릅니다")


def _pages(es: Elasticsearch, index: str, size: int, **source) -> Iterator[list[dict]]:
    """색인 전체를 (doc_id, chunk_index) 순으로 size개씩 읽는다."""
    search_after: list | None = None
    while True:
        kwargs = {
            "index": index,
            "query": {"match_all": {}},
            "size": size,
            "sort": [{"doc_id": "asc"}, {"chunk_index": "asc"}],
            **source,
        }
        if search_after is not None:
            kwargs["search_after"] = search_after
        hits = es.search(**kwargs)["hits"]["hits"]
        if hits:
            yield hits
        if len(hits) < size:
            return
        search_after = hits[-1]["sort"]


def _mget_sources(es: Elasticsearch, index: str, ids: list[str], **source) -> dict[str, dict]:
    docs = es.mget(index=index, ids=ids, **source)["docs"]
    return {doc["_id"]: doc["_source"] for doc in docs if doc.get("found")}


@contextlib.contextmanager
def _es_index(name: str) -> Iterator[None]:
    """store의 문서 색인 함수는 settings.es_index를 읽는다. 잠시 대상 색인으로 바꾼다."""
    previous = settings.es_index
    settings.es_index = name
    try:
        yield
    finally:
        settings.es_index = previous


def reembed_index(
    es: Elasticsearch,
    *,
    source: str,
    target: str,
    batch_size: int = 256,
    max_batches: int = 0,
    dry_run: bool = False,
    progress: Callable[[ReembedResult], None] | None = None,
) -> ReembedResult:
    """source의 청크를 현재 임베딩 설정으로 target에 옮긴다.

    batch_size개씩 읽고 그중 바뀐 청크만 한 번에 임베딩해 bulk로 쓴다. max_batches>0이면 임베딩한
    묶음이 그만큼 차면 멈춘다(complete=False) — 일부만 시험하거나 실행을 나눌 때.
    전부 옮기면 원본에 없는 청크를 지우고 문서 색인 '<target>_docs'를 `pkb doc-index`와 같은 방식으로
    다시 채운다. dry_run은 아무것도 쓰지 않고 할 일만 센다(모델도 올리지 않는다).
    """
    es = es.options(**_RETRY_OPTIONS)
    _check_indices(es, source, target)
    result = ReembedResult()
    target_exists = es.indices.exists(index=target)
    if not target_exists and not dry_run:
        es.indices.create(index=target, body=INDEX_SETTINGS)
        target_exists = True

    source_ids: set[str] = set()
    batches = 0
    for hits in _pages(es, source, batch_size, source_excludes=["embedding"]):
        docs: dict[str, dict] = {}
        for hit in hits:
            src = hit["_source"]
            if "media_model" not in src:
                src = {**src, "embedding_fingerprint": embedding_fingerprint(src)}
            docs[hit["_id"]] = src
        current = _mget_sources(es, target, list(docs), source_excludes=["embedding"]) if target_exists else {}
        to_embed: list[str] = []
        to_copy: list[str] = []
        from_source: list[str] = []  # 원본 벡터를 그대로 옮길 미디어 청크
        for _id, doc in docs.items():
            old = current.get(_id)
            if old == doc:
                continue
            if "media_model" in doc:
                from_source.append(_id)
            elif old is not None and old.get("embedding_fingerprint") == doc["embedding_fingerprint"]:
                to_copy.append(_id)
            else:
                to_embed.append(_id)
        if to_embed and max_batches and batches >= max_batches and not dry_run:
            return result
        result.scanned += len(hits)
        source_ids.update(docs)
        result.unchanged += len(docs) - len(to_embed) - len(to_copy) - len(from_source)
        result.embedded += len(to_embed)
        result.copied += len(to_copy) + len(from_source)
        if dry_run or not (to_embed or to_copy or from_source):
            continue

        ops: list[dict] = []
        for index, ids in ((target, to_copy), (source, from_source)):
            vectors = _mget_sources(es, index, ids, source_includes=["embedding"]) if ids else {}
            for _id in ids:
                ops.extend([{"index": {"_index": target, "_id": _id}}, {**docs[_id], **vectors[_id]}])
        if to_embed:
            from pkb.embeddings import embed_documents

            started = time.perf_counter()
            embedded = embed_documents(
                [embed_input(docs[_id]) for _id in to_embed], pad_to_multiple_of=_PAD_TO_MULTIPLE_OF
            )
            result.embed_seconds += time.perf_counter() - started
            batches += 1
            for _id, vector in zip(to_embed, embedded, strict=True):
                ops.extend([{"index": {"_index": target, "_id": _id}}, {**docs[_id], "embedding": vector}])
        _bulk(es, ops, "reembed")
        if progress:
            progress(result)

    stale = [
        hit["_id"]
        for hits in (_pages(es, target, 1000, source_includes=["doc_id"]) if target_exists else ())
        for hit in hits
        if hit["_id"] not in source_ids
    ]
    result.deleted = len(stale)
    if dry_run:
        return result
    for start in range(0, len(stale), _DELETE_BATCH):
        ops = [{"delete": {"_index": target, "_id": _id}} for _id in stale[start : start + _DELETE_BATCH]]
        _bulk(es, ops, "reembed")
    with _es_index(target):
        result.documents = backfill_document_index(es)
    result.complete = True
    return result
