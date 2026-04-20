import contextlib

from elasticsearch import Elasticsearch, NotFoundError

from pkb.config import settings

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
            # Lifecycle: 사용자 지정 만료일(expires_at)과 실제 아카이브 시점(archived_at).
            # 둘 다 null이면 검색에 정상 포함. 쿼리 필터는 retrieve._lifecycle_filter 참고.
            "expires_at": {"type": "date"},
            "archived_at": {"type": "date"},
            "archive_reason": {"type": "keyword"},
        }
    },
}


def get_client() -> Elasticsearch:
    return Elasticsearch(settings.es_host)


def create_index(es: Elasticsearch) -> None:
    if not es.indices.exists(index=settings.es_index):
        es.indices.create(index=settings.es_index, body=INDEX_SETTINGS)


def delete_index(es: Elasticsearch) -> None:
    with contextlib.suppress(NotFoundError):
        es.indices.delete(index=settings.es_index)


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


def delete_document(es: Elasticsearch, doc_id: str) -> int:
    """doc_id에 해당하는 모든 청크 삭제. 삭제된 수를 반환."""
    result = es.delete_by_query(
        index=settings.es_index,
        query={"term": {"doc_id": doc_id}},
        refresh=True,
    )
    return result["deleted"]


def list_documents(es: Elasticsearch, category: str | None = None) -> list[dict]:
    """저장된 고유 문서 목록 반환."""
    query: dict = {"match_all": {}}
    if category:
        query = {"term": {"category": category}}

    result = es.search(
        index=settings.es_index,
        query=query,
        aggs={
            "docs": {
                "terms": {"field": "doc_id", "size": 10000},
                "aggs": {
                    "meta": {
                        "top_hits": {
                            "size": 1,
                            "_source": [
                                "doc_id",
                                "source_path",
                                "category",
                                "title",
                                "tags",
                                "date_modified",
                            ],
                        }
                    },
                    "chunk_count": {"value_count": {"field": "chunk_index"}},
                },
            }
        },
        size=0,
    )

    docs = []
    for bucket in result["aggregations"]["docs"]["buckets"]:
        hit = bucket["meta"]["hits"]["hits"][0]["_source"]
        hit["chunks"] = bucket["chunk_count"]["value"]
        docs.append(hit)
    return docs


def count_documents(es: Elasticsearch) -> int:
    """인덱스 내 전체 문서(청크) 수."""
    try:
        result = es.count(index=settings.es_index)
        return result["count"]
    except NotFoundError:
        return 0
