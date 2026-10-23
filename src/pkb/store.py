import contextlib
import copy
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache

from elasticsearch import Elasticsearch, NotFoundError

from pkb import access
from pkb.config import settings
from pkb.items import ITEM_MAPPING_PROPERTIES, ITEM_METADATA_FIELDS
from pkb.legal import LEGAL_MAPPING_PROPERTIES
from pkb.media import MEDIA_MAPPING_PROPERTIES, exclude_media_filter
from pkb.search_log import log_change

INDEX_SETTINGS = {
    "settings": {
        "analysis": {
            "analyzer": {
                "korean": {
                    "type": "custom",
                    "tokenizer": "nori_tokenizer",
                    "filter": ["nori_readingform", "lowercase"],
                }
            }
        }
    },
    "mappings": {
        "properties": {
            "content": {
                "type": "text",
                "analyzer": "korean",
                "fields": {"standard": {"type": "text", "analyzer": "standard"}},
            },
            "embedding": {
                "type": "dense_vector",
                "dims": settings.embedding_dims,
                "index": True,
                "similarity": "cosine",
            },
            "source_path": {"type": "keyword"},
            "category": {"type": "keyword"},
            "doc_id": {"type": "keyword"},
            "chunk_index": {"type": "integer"},
            "section_path": {"type": "text", "analyzer": "korean"},
            "title": {"type": "text", "analyzer": "korean"},
            "tags": {"type": "keyword"},
            "date_modified": {"type": "date"},
            "language": {"type": "keyword"},
            "content_hash": {"type": "keyword"},
            # 벡터 재사용 판정 키 (모델+전처리+임베딩 입력 해시). ingest.embedding_fingerprint 참고.
            "embedding_fingerprint": {"type": "keyword"},
            # Lifecycle: 사용자 지정 만료일(expires_at)과 실제 아카이브 시점(archived_at).
            # 둘 다 null이면 검색에 정상 포함. 쿼리 필터는 retrieve._lifecycle_filter 참고.
            "expires_at": {"type": "date"},
            "archived_at": {"type": "date"},
            "archive_reason": {"type": "keyword"},
            # Optional document-contract metadata from YAML frontmatter.  ES
            # accepts scalar-or-array values for keyword fields, so aliases,
            # source IDs, support links, and concept IDs all use the same
            # mapping while retaining exact-match filter semantics.
            "schema_version": {"type": "keyword"},
            "doc_type": {"type": "keyword"},
            "canonical_id": {"type": "keyword"},
            "status": {"type": "keyword"},
            "authority": {"type": "keyword"},
            "import_extraction": {"type": "keyword"},
            "import_document_id": {"type": "keyword"},
            "import_version": {"type": "integer"},
            "aliases": {"type": "keyword"},
            "source_ids": {"type": "keyword"},
            "supports": {"type": "keyword"},
            "concept_ids": {"type": "keyword"},
            # 수집 항목 메타데이터 (pkb.items). 기존 색인에는 ensure_item_mapping이 더한다.
            **ITEM_MAPPING_PROPERTIES,
            **LEGAL_MAPPING_PROPERTIES,
            # 이미지·오디오 청크 (pkb.media). 기존 색인에는 ensure_item_mapping이 더한다.
            **MEDIA_MAPPING_PROPERTIES,
        }
    },
}

_SEARCH_PAGE_SIZE = 1000
_AGG_PAGE_SIZE = 1000

# list_documents가 돌려주는 문서 메타데이터. 문서 색인 행도 이 필드를 대표 청크에서 옮겨 담는다.
_LIST_FIELDS = (
    "doc_id",
    "source_path",
    "category",
    "title",
    "tags",
    "date_modified",
    "expires_at",
    "schema_version",
    "doc_type",
    "canonical_id",
    "status",
    "authority",
    "aliases",
    "source_ids",
    "supports",
    "concept_ids",
    "import_document_id",
    "import_version",
)
DOC_ROW_FIELDS = (*_LIST_FIELDS, "archived_at", "archive_reason", *ITEM_METADATA_FIELDS)
DOC_INDEX_SUFFIX = "_docs"
# 문서당 한 행. 필터·생명주기 필드는 청크 색인과 같은 이름·매핑을 써서 검색 필터를 그대로 공유한다.
DOC_INDEX_SETTINGS = {
    "settings": INDEX_SETTINGS["settings"],
    "mappings": {
        "properties": {
            **{field: INDEX_SETTINGS["mappings"]["properties"][field] for field in DOC_ROW_FIELDS},
            "chunk_count": {"type": "integer"},
            # 청크 임베딩 평균을 L2 정규화한 벡터 — 관련 문서·문서 지도용
            "doc_vector": {
                "type": "dense_vector",
                "dims": settings.embedding_dims,
                "index": True,
                "similarity": "cosine",
            },
        }
    },
}
# 이 프로세스에서 항목 필드 매핑을 확인한 청크 색인 이름
_item_mapping_checked: set[str] = set()


@lru_cache(maxsize=4)
def _client_for_host(host: str) -> Elasticsearch:
    """프로세스 수명 동안 transport/connection pool을 재사용."""
    return Elasticsearch(host)


def get_client() -> Elasticsearch:
    return _client_for_host(settings.es_host)


def doc_index_name() -> str:
    """문서 단위 색인 이름. ES_INDEX가 읽기 alias면 이것도 alias다.

    create_doc_index가 이 alias를 물리 색인 '<물리 청크 색인>_docs'에 걸고,
    switch_read_alias가 청크 alias와 함께 옮긴다. 모델이 다른 두 벡터 공간이 섞이지 않는다.
    """
    return f"{settings.es_index}{DOC_INDEX_SUFFIX}"


def create_index(es: Elasticsearch) -> None:
    """청크 색인을 만든다. 이미 있으면 새 항목 메타데이터 필드만 더한다.

    문서 색인은 청크 색인을 새로 만들 때만 같이 만든다. 이미 채워진 청크 색인 옆에 빈 문서
    색인이 생기면 list_documents가 빈 목록을 보기 때문이다. 그때는 `pkb doc-index`가 만들고 채운다.
    """
    if es.indices.exists(index=settings.es_index):
        ensure_item_mapping(es)
        return
    es.indices.create(index=settings.es_index, body=INDEX_SETTINGS)
    create_doc_index(es)


def ensure_item_mapping(es: Elasticsearch) -> None:
    """기존 청크 색인에 항목·미디어 메타데이터 필드를 더한다. 재색인 없이 put_mapping 한 번.

    매핑이 없으면 동적 매핑이 people·links를 text로 잡아 term 필터가 맞지 않으므로 첫
    인제스트 전에 명시한다. 같은 매핑을 다시 넣는 것은 ES에서 아무 일도 하지 않는다.
    프로세스마다 색인 이름당 한 번만 부른다.
    """
    index = settings.es_index
    if index in _item_mapping_checked or not es.indices.exists(index=index):
        return
    es.indices.put_mapping(index=index, properties={
        **ITEM_MAPPING_PROPERTIES, **MEDIA_MAPPING_PROPERTIES,
        "import_extraction": {"type": "keyword"},
        "import_document_id": {"type": "keyword"},
        "import_version": {"type": "integer"},
    })
    if es.indices.exists(index=doc_index_name()):
        es.indices.put_mapping(index=doc_index_name(), properties={
            "import_document_id": {"type": "keyword"}, "import_version": {"type": "integer"},
        })
    _item_mapping_checked.add(index)


def create_doc_index(es: Elasticsearch) -> bool:
    """문서 색인이 없으면 만든다. 만들었으면 True.

    ES_INDEX가 alias면 그 물리 청크 색인 이름으로 물리 문서 색인을 만들고 doc_index_name()
    alias를 건다 — 물리 색인이 alias 이름을 차지하면 나중에 switch_read_alias가 막힌다.
    """
    name = doc_index_name()
    if es.indices.exists(index=name):
        return False
    mapping = copy.deepcopy(DOC_INDEX_SETTINGS)
    mapping["mappings"]["properties"]["doc_vector"]["dims"] = settings.embedding_dims
    if es.indices.exists_alias(name=settings.es_index):
        targets = sorted(es.indices.get_alias(name=settings.es_index))
        if len(targets) != 1:
            raise ValueError(f"'{settings.es_index}' alias가 물리 인덱스 하나를 가리켜야 합니다: {targets}")
        physical = f"{targets[0]}{DOC_INDEX_SUFFIX}"
        if not es.indices.exists(index=physical):
            es.indices.create(index=physical, body=mapping)
        es.indices.put_alias(index=physical, name=name)
        return True
    es.indices.create(index=name, body=mapping)
    return True


def migrate_legal_mapping(es: Elasticsearch, *, index: str) -> list[str]:
    """Add legal fields to one explicitly selected physical index; preserve all data.

    Not called by create_index or ingest: existing services require a separate
    authorized migration. Conflicting dynamic mappings require a fresh index.
    """
    if not index or any(char in index for char in "*,?") or index == "_all":
        raise ValueError("select one physical index for the legal metadata migration")
    if es.indices.exists_alias(name=index):
        raise ValueError("select a physical index, not an alias")
    mappings = es.indices.get_mapping(index=index)
    if set(mappings) != {index}:
        raise ValueError("migration must resolve to exactly the selected physical index")
    existing = mappings[index]["mappings"].get("properties", {})
    for field, expected in LEGAL_MAPPING_PROPERTIES.items():
        if field in existing and existing[field].get("type") != expected["type"]:
            raise ValueError(f"incompatible mapping for {field}; reindex into a separate index")
    additions = {field: spec for field, spec in LEGAL_MAPPING_PROPERTIES.items() if field not in existing}
    if additions:
        es.indices.put_mapping(index=index, properties=additions)
    return list(additions)


def delete_index(es: Elasticsearch) -> None:
    for index in (settings.es_index, doc_index_name()):
        with contextlib.suppress(NotFoundError):
            es.indices.delete(index=index)


def switch_read_alias(es: Elasticsearch, new_index: str) -> list[str]:
    """settings.es_index를 읽기 alias로 보고 new_index 하나로 원자 전환.

    모델·차원이 다른 물리 인덱스(예: pkb_documents_v2)를 미리 채운 뒤 호출하면
    검색 경로가 무중단으로 새 인덱스를 보게 된다. 이전 alias 대상 목록을 반환한다.
    물리 인덱스가 alias 이름을 점유 중이면 에러 — 물리 삭제는 사용자가 명시적으로 한다.
    문서 색인 alias('<alias>_docs')도 '<new_index>_docs'로 같은 요청에서 옮긴다.
    """
    alias = settings.es_index
    doc_alias, new_docs = doc_index_name(), f"{new_index}{DOC_INDEX_SUFFIX}"
    if not es.indices.exists(index=new_index):
        raise ValueError(f"대상 인덱스가 없습니다: {new_index}")
    if new_index == alias:
        raise ValueError(f"대상이 alias 이름과 같습니다: {new_index}")
    if not es.indices.exists(index=new_docs):
        raise ValueError(
            f"대상의 문서 색인이 없습니다: {new_docs}. ES_INDEX={new_index}로 `pkb doc-index`를 먼저 실행하세요."
        )
    for name in (alias, doc_alias):
        if es.indices.exists(index=name) and not es.indices.exists_alias(name=name):
            raise ValueError(
                f"'{name}'는 alias가 아니라 물리 인덱스입니다. 먼저 데이터를 새 물리 인덱스로 "
                f"옮기고 '{name}'를 삭제한 뒤 다시 실행하세요."
            )
    actions: list[dict] = []
    old_targets: list[str] = []
    if es.indices.exists_alias(name=alias):
        old_targets = sorted(es.indices.get_alias(name=alias))
        actions.extend(
            {"remove": {"index": old, "alias": alias}} for old in old_targets
        )
    if es.indices.exists_alias(name=doc_alias):
        actions.extend(
            {"remove": {"index": old, "alias": doc_alias}}
            for old in sorted(es.indices.get_alias(name=doc_alias))
        )
    actions.append({"add": {"index": new_index, "alias": alias}})
    actions.append({"add": {"index": new_docs, "alias": doc_alias}})
    es.indices.update_aliases(actions=actions)
    return old_targets


def add_chunks(
    es: Elasticsearch,
    chunks: list[dict],
) -> int:
    """청크들을 ES에 벌크 인덱싱. 인덱싱된 수를 반환."""
    actions = []
    for chunk in chunks:
        doc_id = f"{chunk['doc_id']}_{chunk['chunk_index']}"
        actions.append({"index": {"_index": settings.es_index, "_id": doc_id}})
        actions.append(chunk)

    if actions:
        es.bulk(operations=actions, refresh=True)
    return len(chunks)


def _doc_chunk_sources(es: Elasticsearch, doc_id: str, source_excludes: list[str]) -> list[dict]:
    """doc_id의 청크 source를 chunk_index 순으로 끝까지 읽는다. 색인이 없으면 NotFoundError."""
    sources: list[dict] = []
    search_after: list | None = None
    while True:
        kwargs = {
            "index": settings.es_index,
            "query": {"term": {"doc_id": doc_id}},
            "size": _SEARCH_PAGE_SIZE,
            "source_excludes": source_excludes,
            "sort": [{"chunk_index": "asc"}],
        }
        if search_after is not None:
            kwargs["search_after"] = search_after
        res = es.search(**kwargs)
        hits = res["hits"]["hits"]
        sources.extend(hit["_source"] for hit in hits)
        if len(hits) < _SEARCH_PAGE_SIZE or not hits or "sort" not in hits[-1]:
            break
        search_after = hits[-1]["sort"]
    return sources


def get_existing_chunks(es: Elasticsearch, doc_id: str) -> dict[int, dict]:
    """{chunk_index: source dict (excluding embedding+content)} 반환.
    빈 dict면 신규 문서 또는 인덱스 미존재.
    """
    try:
        sources = _doc_chunk_sources(es, doc_id, ["embedding", "content"])
    except NotFoundError:
        return {}
    return {source["chunk_index"]: source for source in sources}


def get_chunk_embeddings(
    es: Elasticsearch, doc_id: str, indices: list[int]
) -> dict[int, list[float]]:
    """지정 슬롯들의 embedding 벡터만 반환. 슬롯 이동 시 임베딩 재사용용."""
    if not indices:
        return {}
    res = es.mget(
        index=settings.es_index,
        ids=[f"{doc_id}_{i}" for i in indices],
        source_includes=["embedding", "chunk_index"],
    )
    out: dict[int, list[float]] = {}
    for d in res["docs"]:
        src = d.get("_source") if d.get("found") else None
        if src and "embedding" in src:
            out[src["chunk_index"]] = src["embedding"]
    return out


def apply_chunk_delta(
    es: Elasticsearch,
    doc_id: str,
    new_chunks: list[dict],
    metadata_updates: list[tuple[int, dict]],
    delete_indices: list[int],
) -> dict:
    """index/update/delete 혼합 bulk 1회. 카운트 반환.

    new_chunks: 새로 또는 다시 임베딩된 청크 (embedding 포함, content_hash 필수).
    metadata_updates: (chunk_index, partial doc) — 메타데이터-only 변경.
    delete_indices: 사라진 청크 슬롯의 chunk_index 목록.
    """
    for c in new_chunks:
        assert "content_hash" in c, f"missing content_hash in chunk {c.get('chunk_index')}"

    ops: list[dict] = []
    for c in new_chunks:
        _id = f"{doc_id}_{c['chunk_index']}"
        ops.append({"index": {"_index": settings.es_index, "_id": _id}})
        ops.append(c)
    for idx, partial in metadata_updates:
        ops.append({"update": {"_index": settings.es_index, "_id": f"{doc_id}_{idx}"}})
        ops.append({"doc": partial})
    for idx in delete_indices:
        ops.append({"delete": {"_index": settings.es_index, "_id": f"{doc_id}_{idx}"}})

    if ops:
        _bulk(es, ops, "bulk delta")

    return {
        "indexed": len(new_chunks),
        "updated": len(metadata_updates),
        "deleted": len(delete_indices),
    }


def _bulk(es: Elasticsearch, ops: list[dict], what: str) -> None:
    """bulk 1회. 항목 하나라도 실패하면 첫 오류로 RuntimeError."""
    resp = es.bulk(operations=ops, refresh=True)
    if resp.get("errors"):
        first_err = next(
            (
                item[op]["error"]
                for item in resp.get("items", [])
                for op in item
                if "error" in item[op]
            ),
            None,
        )
        raise RuntimeError(f"{what} failed: {first_err}")


def build_document_row(chunks: list[dict]) -> dict:
    """한 문서의 청크(임베딩 포함)로 문서 색인 행을 만든다.

    메타데이터는 문서 단위라 모든 청크에 같으므로 가장 앞 슬롯에서 옮긴다. doc_vector는
    청크 벡터 평균의 L2 정규화 — 합과 평균은 방향이 같아 합을 정규화한다. 벡터가 없는
    구 청크만 있으면 doc_vector를 넣지 않는다.
    """
    first = min(chunks, key=lambda chunk: chunk["chunk_index"])
    row = {field: first[field] for field in DOC_ROW_FIELDS if first.get(field) is not None}
    row["chunk_count"] = len(chunks)
    vectors = [chunk["embedding"] for chunk in chunks if chunk.get("embedding")]
    if vectors:
        total = [sum(column) for column in zip(*vectors, strict=True)]
        norm = math.sqrt(sum(value * value for value in total))
        if norm > 0:
            row["doc_vector"] = [value / norm for value in total]
    return row


# 행에는 문서 벡터가 들어 있어 묶음이 크면 메모리를 많이 쓴다 (1024차원 × 200행 ≈ 수 MB).
_DOC_ROW_BATCH = 200


def refresh_document_rows(es: Elasticsearch, doc_ids: Iterable[str]) -> int:
    """청크 색인의 현재 상태로 문서 색인 행을 다시 쓴다. 청크가 없는 문서는 행을 지운다.

    청크에 저장된 벡터를 읽으므로 재임베딩하지 않는다. 문서 색인이 없으면(`pkb doc-index`
    전) 아무것도 하지 않는다. 쓴 행 수를 반환한다.
    """
    index = doc_index_name()
    if not es.indices.exists(index=index):
        return 0
    written = 0
    ops: list[dict] = []
    for doc_id in dict.fromkeys(doc_ids):
        try:
            chunks = _doc_chunk_sources(es, doc_id, ["content"])
        except NotFoundError:
            chunks = []
        if chunks:
            ops.extend([{"index": {"_index": index, "_id": doc_id}}, build_document_row(chunks)])
            written += 1
        else:
            ops.append({"delete": {"_index": index, "_id": doc_id}})
        if len(ops) >= _DOC_ROW_BATCH * 2:
            _bulk(es, ops, "document rows")
            ops = []
    if ops:
        _bulk(es, ops, "document rows")
    return written


def backfill_document_index(es: Elasticsearch) -> dict:
    """청크 색인에서 문서 색인을 만들고 채운다. 청크가 없는 행은 지운다."""
    created = create_doc_index(es)
    doc_ids = [bucket["key"]["doc_id"] for bucket in _composite_buckets(es, "ids", query={"match_all": {}})]
    written = refresh_document_rows(es, doc_ids)
    rows = {
        bucket["key"]["doc_id"]
        for bucket in _composite_buckets(es, "ids", query={"match_all": {}}, index=doc_index_name())
    }
    orphans = sorted(rows - set(doc_ids))
    refresh_document_rows(es, orphans)
    return {"created": created, "documents": written, "removed": len(orphans)}


def list_categories(es: Elasticsearch, include_archived: bool = False) -> dict[str, int]:
    """권한이 있는 최신 문서 목록과 같은 기준으로 카테고리별 수를 센다."""
    if not es.indices.exists(index=doc_index_name()):
        raise ValueError("문서 색인이 없습니다. 서버에서 `uv run pkb doc-index`로 채우세요.")
    counts: dict[str, int] = {}
    for doc in list_documents(es, include_archived=include_archived):
        category = doc["category"]
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


def count_chunks_without_hash(es: Elasticsearch) -> int:
    """content_hash 필드가 없는 청크 수. 마이그레이션 진행도 추적용."""
    try:
        result = es.count(
            index=settings.es_index,
            query={"bool": {"must_not": {"exists": {"field": "content_hash"}}}},
        )
        return result["count"]
    except NotFoundError:
        return 0


def delete_document(es: Elasticsearch, doc_id: str) -> int:
    """doc_id에 해당하는 모든 청크 삭제. 삭제된 수를 반환."""
    result = es.delete_by_query(
        index=settings.es_index,
        query={"term": {"doc_id": doc_id}},
        refresh=True,
    )
    refresh_document_rows(es, [doc_id])
    log_change("delete", doc_id, chunks=result["deleted"])
    return result["deleted"]


# reconcile(prune)에서 이보다 많은 문서를 지우려면 명시적 확인 필요.
# ponytail: 러프한 임계 — 잘못 지워도 재sync로 복구 가능하니 정밀할 필요 없음.
PRUNE_CONFIRM_THRESHOLD = 20


def list_doc_ids(es: Elasticsearch, prefix: str) -> set[str]:
    """doc_id가 prefix로 시작하는 고유 doc_id 집합.

    archived 문서는 제외 — 아카이브는 의도된 보존이므로 reconcile prune 대상이 아님.
    """
    query = {
        "bool": {
            "must": [{"prefix": {"doc_id": prefix}}],
            "must_not": [{"exists": {"field": "archived_at"}}],
        }
    }
    return {
        bucket["key"]["doc_id"]
        for bucket in _composite_buckets(es, "ids", query=query)
    }


def _composite_buckets(
    es: Elasticsearch,
    name: str,
    *,
    query: dict,
    sub_aggs: dict | None = None,
    index: str | None = None,
):
    """doc_id composite aggregation을 after_key로 끝까지 순회. index 기본값은 청크 색인."""
    after: dict | None = None
    while True:
        composite: dict = {
            "size": _AGG_PAGE_SIZE,
            "sources": [{"doc_id": {"terms": {"field": "doc_id"}}}],
        }
        if after is not None:
            composite["after"] = after
        aggregation: dict = {"composite": composite}
        if sub_aggs:
            aggregation["aggs"] = sub_aggs
        result = es.search(
            index=index or settings.es_index,
            query=query,
            aggs={name: aggregation},
            size=0,
        )
        page = result["aggregations"][name]
        yield from page["buckets"]
        after = page.get("after_key")
        if not after:
            break


def archive_document(
    es: Elasticsearch, doc_id: str, reason: str | None = None
) -> int:
    """doc_id에 해당하는 청크들에 archived_at=now(UTC) 설정 (soft delete).
    reason이 주어지면 archive_reason도 함께 저장. 수정된 청크 수 반환.
    """
    source_parts = ["ctx._source.archived_at = params.ts;"]
    params: dict = {"ts": datetime.now(UTC).isoformat()}
    if reason:
        source_parts.append("ctx._source.archive_reason = params.reason;")
        params["reason"] = reason
    result = es.update_by_query(
        index=settings.es_index,
        query={"term": {"doc_id": doc_id}},
        script={"source": " ".join(source_parts), "lang": "painless", "params": params},
        refresh=True,
    )
    refresh_document_rows(es, [doc_id])
    log_change("archive", doc_id, chunks=result["updated"], reason=reason)
    return result["updated"]


def restore_document(es: Elasticsearch, doc_id: str) -> int:
    """archived_at, archive_reason 필드를 제거해 복원. 복원된 청크 수 반환."""
    script_source = (
        "ctx._source.remove('archived_at'); ctx._source.remove('archive_reason');"
    )
    result = es.update_by_query(
        index=settings.es_index,
        query={
            "bool": {
                "must": [
                    {"term": {"doc_id": doc_id}},
                    {"exists": {"field": "archived_at"}},
                ]
            }
        },
        script={"source": script_source, "lang": "painless"},
        refresh=True,
    )
    refresh_document_rows(es, [doc_id])
    log_change("restore", doc_id, chunks=result["updated"])
    return result["updated"]


def purge_archived(es: Elasticsearch, before: datetime | None = None) -> int:
    """archived_at이 있는 청크를 물리 삭제. before 지정 시 그 시점 이전만.
    주의: 비가역. 명시 요청 시에만 호출.
    """
    must: list[dict] = [{"exists": {"field": "archived_at"}}]
    if before is not None:
        must.append({"range": {"archived_at": {"lt": before.isoformat()}}})
    query = {"bool": {"must": must}}
    doc_ids = (
        [bucket["key"]["doc_id"] for bucket in _composite_buckets(es, "ids", query=query)]
        if es.indices.exists(index=doc_index_name())
        else []
    )
    result = es.delete_by_query(
        index=settings.es_index,
        query=query,
        refresh=True,
    )
    refresh_document_rows(es, doc_ids)
    log_change("purge", "*", chunks=result["deleted"])
    return result["deleted"]


def list_documents(
    es: Elasticsearch,
    category: str | None = None,
    include_archived: bool = False,
    include_versions: bool = False,
) -> list[dict]:
    """저장된 고유 문서 목록 반환. 기본적으로 archived 문서는 제외. 이미지·오디오는 list_media가 다룬다.

    문서 색인이 있으면 그 행을 읽는다. 없으면(`pkb doc-index` 전) 청크를 doc_id로 묶어
    세는데, 문서가 많으면 카테고리 없이 부를 때 시간 초과가 난다. 반환 형태는 같다.
    """
    clauses: list[dict] = [*exclude_media_filter(None), *access.search_filter()]
    if not include_archived:
        clauses.append({"bool": {"must_not": {"exists": {"field": "archived_at"}}}})
    query: dict = {"bool": {"must": clauses}}

    if es.indices.exists(index=doc_index_name()):
        docs = _import_versions(access.readable(_list_document_rows(es, query)), include_versions)
        return [doc for doc in docs if not category or doc.get("category") == category]

    docs = []
    sub_aggs = {
        "meta": {"top_hits": {"size": 1, "_source": list(_LIST_FIELDS)}},
        "chunk_count": {"value_count": {"field": "chunk_index"}},
    }
    for bucket in _composite_buckets(es, "docs", query=query, sub_aggs=sub_aggs):
        hit = bucket["meta"]["hits"]["hits"][0]["_source"]
        hit["chunks"] = bucket["chunk_count"]["value"]
        docs.append(hit)
    visible = _import_versions(access.readable(docs), include_versions)
    return [doc for doc in visible if not category or doc.get("category") == category]


def _import_versions(docs: list[dict], include_versions: bool) -> list[dict]:
    """Group only visible immutable import versions; timestamps never determine latest."""
    groups: dict[str, list[dict]] = {}
    normal = []
    from pkb.import_versions import validate_identity

    for doc in docs:
        identity = doc.get("import_document_id")
        if identity is None:
            normal.append(doc)
            continue
        validate_identity(identity, doc.get("import_version"))
        groups.setdefault(identity, []).append(doc)
    for versions in groups.values():
        count = len(versions)
        selected = versions if include_versions else [max(versions, key=lambda d: d["import_version"])]
        normal.extend({**doc, "version_count": count} for doc in selected)
    return sorted(normal, key=lambda doc: doc["doc_id"])


def _list_document_rows(es: Elasticsearch, query: dict) -> list[dict]:
    """문서 색인 행을 doc_id 순으로 끝까지 읽어 list_documents 형태로 돌려준다."""
    docs: list[dict] = []
    search_after: list | None = None
    while True:
        kwargs = {
            "index": doc_index_name(),
            "query": query,
            "size": _SEARCH_PAGE_SIZE,
            "source_includes": [*_LIST_FIELDS, "chunk_count"],
            "sort": [{"doc_id": "asc"}],
        }
        if search_after is not None:
            kwargs["search_after"] = search_after
        hits = es.search(**kwargs)["hits"]["hits"]
        for hit in hits:
            source = hit["_source"]
            source["chunks"] = source.pop("chunk_count", 0)
            docs.append(source)
        if len(hits) < _SEARCH_PAGE_SIZE:
            break
        search_after = hits[-1]["sort"]
    return docs


def count_documents(es: Elasticsearch) -> int:
    """인덱스 내 전체 문서(청크) 수."""
    try:
        result = es.count(index=settings.es_index)
        return result["count"]
    except NotFoundError:
        return 0
