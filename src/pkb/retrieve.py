from time import perf_counter

from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.embeddings import embed

RRF_K = 60  # Reciprocal Rank Fusion 상수 (Elastic 기본값)
MAX_CHUNKS_PER_DOC = 2  # 최종 결과에서 문서당 허용할 최대 청크 수 (다양성 캡)

# Search profiles are deliberately small and deterministic.  They map to the
# optional frontmatter contract fields, while ``all`` keeps the historical
# behavior (including legacy documents that have none of those fields).
RETRIEVAL_PROFILES = frozenset({"all", "curated", "evidence", "source"})


def normalize_profile(profile: str | None) -> str:
    """Return a validated retrieval profile name.

    ``None``/an empty string means ``all`` for backwards compatibility with
    callers that do not know about profiles yet.  Unknown names fail early so
    an MCP caller cannot accidentally receive an unfiltered result set.
    """
    if profile is None:
        normalized = "all"
    elif isinstance(profile, str):
        normalized = profile.strip().lower() or "all"
    else:
        raise ValueError(f"검색 프로필은 문자열이어야 합니다: {profile!r}")
    if normalized not in RETRIEVAL_PROFILES:
        allowed = ", ".join(sorted(RETRIEVAL_PROFILES))
        raise ValueError(f"알 수 없는 검색 프로필: {profile!r} (허용: {allowed})")
    return normalized


def _resolve_profile(profile: str | None, retrieval_profile: str | None) -> str:
    """Resolve the public ``profile``/``retrieval_profile`` aliases."""
    if profile is not None and retrieval_profile is not None:
        left = normalize_profile(profile)
        right = normalize_profile(retrieval_profile)
        if left != right:
            raise ValueError("profile과 retrieval_profile이 서로 다릅니다")
        return left
    return normalize_profile(profile if profile is not None else retrieval_profile)


def profile_filter(profile: str | None) -> list[dict]:
    """Return Elasticsearch filter clauses for a retrieval profile.

    ``curated`` is the maintained knowledge view: concept/guide/MOC notes in
    canonical or active status.  ``evidence`` adds research notes to that same
    active/canonical set.  ``source`` selects source documents regardless of
    status.  ``all`` contributes no clause.  Returning a list mirrors the
    lifecycle and doc-prefix helpers and makes composition in BM25/kNN queries
    explicit.
    """
    normalized = normalize_profile(profile)
    if normalized == "all":
        return []
    if normalized == "curated":
        return [
            {"terms": {"doc_type": ["concept", "guide", "moc"]}},
            {"terms": {"status": ["canonical", "active"]}},
        ]
    if normalized == "evidence":
        return [
            {"terms": {"doc_type": ["concept", "guide", "moc", "research"]}},
            {"terms": {"status": ["canonical", "active"]}},
        ]
    return [{"term": {"doc_type": "source"}}]


# Private alias kept for the style of the existing query-shape helpers; the
# public name above is what application/MCP code should call.
_profile_filter = profile_filter


def canonical_group_key(candidate: dict) -> str:
    """Return a stable grouping key for a hit.

    Documents sharing ``canonical_id`` represent one logical source.  Legacy
    chunks without that field fall back to their physical ``doc_id`` so the
    canonical grouping option is safe to enable on a mixed index.
    """
    canonical_id = candidate.get("canonical_id")
    if canonical_id is not None:
        canonical_id = str(canonical_id).strip()
    if canonical_id:
        return canonical_id
    return str(candidate.get("doc_id") or candidate.get("_id") or "")


def apply_canonical_boost(candidates: list[dict], boost: float) -> list[dict]:
    """Apply an optional score multiplier to metadata-bearing canonical hits.

    A positive ``boost`` is interpreted as a relative multiplier (``0.15``
    means +15%).  It is intentionally a post-fusion operation so BM25/kNN
    scores remain comparable.  Hits without ``canonical_id`` are left alone,
    preserving legacy ranking behavior.  The input list is sorted and returned
    for convenient use in the search pipeline.
    """
    if not boost:
        return candidates
    if boost < 0:
        raise ValueError("canonical_boost는 음수가 될 수 없습니다")
    factor = 1.0 + float(boost)
    for candidate in candidates:
        canonical_id = candidate.get("canonical_id")
        if canonical_id is None or not str(canonical_id).strip():
            continue
        score = candidate.get("score")
        if score is None:
            continue
        candidate["score"] = float(score) * factor
    candidates.sort(key=lambda item: -item.get("score", 0.0))
    return candidates


def _cap_per_doc(
    candidates: list[dict],
    top_k: int,
    max_per_doc: int = MAX_CHUNKS_PER_DOC,
    group_by_canonical: bool = False,
) -> list[dict]:
    """정렬된 후보를 순회하며 문서/정본별 max_per_doc개까지만 취해 top_k를 채운다.

    한 문서가 상위권을 독점하는 것을 막기 위한 다양성 캡. 캡 적용 후에도
    top_k에 미달하면, 캡에 걸려 건너뛴 후보들로 원래 순서대로 채운다.
    ``group_by_canonical=True``면 ``canonical_id``가 같은 물리 문서를 한
    그룹으로 취급한다. canonical_id가 없는 레거시 청크는 doc_id로
    fallback하므로 기존 결과와 호환된다.
    """
    counts: dict[str, int] = {}
    selected: list[dict] = []
    skipped: list[dict] = []
    for c in candidates:
        if len(selected) >= top_k:
            break
        group_key = canonical_group_key(c) if group_by_canonical else c.get("doc_id")
        count = counts.get(group_key, 0)
        if count < max_per_doc:
            selected.append(c)
            counts[group_key] = count + 1
        else:
            skipped.append(c)
    if len(selected) < top_k:
        selected.extend(skipped[: top_k - len(selected)])
    return selected


def cap_per_canonical(
    candidates: list[dict], top_k: int, max_per_canonical: int = 1
) -> list[dict]:
    """Cap sorted hits by logical ``canonical_id`` (one by default).

    This convenience wrapper is useful to callers that need strict one-result
    per canonical document while ``hybrid_search(canonical_group=True)`` keeps
    the historical two-chunk diversity cap unless explicitly configured by
    the caller.
    """
    return _cap_per_doc(
        candidates,
        top_k,
        max_per_doc=max_per_canonical,
        group_by_canonical=True,
    )


_cap_per_canonical = cap_per_canonical


def _lifecycle_filter(include_archived: bool) -> list[dict]:
    """아카이브/만료 문서 제외 필터. include_archived=True면 빈 리스트 반환.

    기본 조건:
      - archived_at 필드가 없어야 함 (아카이브되지 않음)
      - expires_at이 없거나 현재 시점보다 미래여야 함
    """
    if include_archived:
        return []
    return [
        {"bool": {"must_not": {"exists": {"field": "archived_at"}}}},
        {
            "bool": {
                "should": [
                    {"bool": {"must_not": {"exists": {"field": "expires_at"}}}},
                    {"range": {"expires_at": {"gt": "now"}}},
                ],
                "minimum_should_match": 1,
            }
        },
    ]


def _exclude_doc_prefix_filter(exclude_doc_prefix: str | None) -> list[dict]:
    """제외할 doc_id 접두사(예: obsidian/) must_not prefix 필터. 없으면 빈 리스트."""
    if not exclude_doc_prefix:
        return []
    return [{"bool": {"must_not": [{"prefix": {"doc_id": exclude_doc_prefix}}]}}]


def _bm25_query(
    query_text: str,
    category: str | None,
    include_archived: bool = False,
    exclude_doc_prefix: str | None = None,
    profile: str | None = None,
    retrieval_profile: str | None = None,
) -> dict:
    bm25: dict = {
        "bool": {
            "should": [
                {"match": {"content": {"query": query_text, "boost": 1.0}}},
                {"match": {"title": {"query": query_text, "boost": 0.5}}},
                {"match": {"section_path": {"query": query_text, "boost": 0.3}}},
            ],
        }
    }
    filters: list[dict] = []
    if category:
        filters.append({"term": {"category": category}})
    filters.extend(profile_filter(_resolve_profile(profile, retrieval_profile)))
    filters.extend(_lifecycle_filter(include_archived))
    filters.extend(_exclude_doc_prefix_filter(exclude_doc_prefix))
    if filters:
        bm25["bool"]["filter"] = filters
    return bm25


def _knn_query(
    query_vector: list[float],
    k: int,
    category: str | None,
    include_archived: bool = False,
    exclude_doc_prefix: str | None = None,
    profile: str | None = None,
    retrieval_profile: str | None = None,
) -> dict:
    knn: dict = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": k,
        "num_candidates": k * 5,
    }
    filters: list[dict] = []
    if category:
        filters.append({"term": {"category": category}})
    filters.extend(profile_filter(_resolve_profile(profile, retrieval_profile)))
    filters.extend(_lifecycle_filter(include_archived))
    filters.extend(_exclude_doc_prefix_filter(exclude_doc_prefix))
    if filters:
        knn["filter"] = filters
    return knn


def _source_to_dict(hit: dict) -> dict:
    source = hit["_source"]
    source["_id"] = hit["_id"]
    return source


def hybrid_search(
    es: Elasticsearch,
    query_text: str,
    category: str | None = None,
    top_k: int = 5,
    candidate_k: int = settings.candidate_k,
    rerank: bool = False,
    expand_context: int = 0,
    log: bool = True,
    include_archived: bool = False,
    exclude_doc_prefix: str | None = None,
    variants: list[str] | None = None,
    query_vector_out: list[list[float]] | None = None,
    profile: str | None = None,
    canonical_group: bool = False,
    canonical_boost: float = 0.0,
    retrieval_profile: str | None = None,
) -> list[dict]:
    """하이브리드 검색.

    Args:
        top_k: 최종 반환 수
        candidate_k: 각 검색(BM25/kNN)에서 가져올 후보 수 (리랭크/RRF 용)
        rerank: True면 CrossEncoder 재순위 수행 후 top_k 반환
        expand_context: N>0이면 각 결과 전후 N개 청크를 neighbors 필드로 함께 반환
        exclude_doc_prefix: 지정하면 해당 doc_id 접두사 문서를 검색에서 제외 (예: "obsidian/")
        variants: 쿼리 변형(최대 3개) — 각 변형으로 검색해 RRF 점수를 _id별 합산 병합
            (RAG-Fusion). 리랭크는 원 query_text 기준 1회만 수행.
        query_vector_out: 지정하면 원 쿼리 벡터를 1개 append. 그래프 어휘 검색 등 후속
            단계가 같은 인코딩을 재사용하기 위한 내부 adapter hook.
        profile: ``all``(기본), ``curated``, ``evidence``, ``source`` 중 하나.
            프로필은 frontmatter의 status/doc_type 필터로 변환된다.
        canonical_group: True면 canonical_id가 같은 물리 문서를 한 다양성 그룹으로
            묶는다. canonical_id가 없는 레거시 청크는 doc_id로 fallback한다.
        canonical_boost: canonical_id가 있는 결과의 상대 점수 가산율(예: 0.15 = +15%).
    """
    profile = _resolve_profile(profile, retrieval_profile)
    if canonical_boost < 0:
        raise ValueError("canonical_boost는 음수가 될 수 없습니다")
    timings: dict[str, float] = {}
    t_total = perf_counter()

    # 원 쿼리 + 공백/중복 제거한 변형 최대 3개
    queries = [query_text]
    for v in variants or []:
        v = v.strip()
        if v and v not in queries and len(queries) < 4:
            queries.append(v)

    t = perf_counter()
    query_vectors = embed(queries)  # 변형 포함 1회 배치 인코딩
    if query_vector_out is not None:
        query_vector_out.append(query_vectors[0])
    timings["embed_ms"] = round((perf_counter() - t) * 1000, 2)

    fetch_k = candidate_k

    t = perf_counter()
    merged: dict[str, dict] = {}
    for i, (q, vec) in enumerate(zip(queries, query_vectors, strict=True)):
        hits = _rrf_search(
            es, q, vec, category, fetch_k,
            timings=timings if i == 0 else None,  # 세부 타이밍은 원 쿼리만 기록
            include_archived=include_archived,
            exclude_doc_prefix=exclude_doc_prefix,
            profile=profile,
        )
        for hit in hits:
            if hit["_id"] in merged:
                merged[hit["_id"]]["score"] += hit["score"]
            else:
                merged[hit["_id"]] = hit
    candidates = sorted(merged.values(), key=lambda h: -h["score"])
    # Apply before reranking only when no reranker will overwrite ``score``;
    # reranked searches apply the same operation below after CrossEncoder
    # scores are available.
    if not rerank:
        apply_canonical_boost(candidates, canonical_boost)
    timings["retrieve_ms"] = round((perf_counter() - t) * 1000, 2)

    if rerank:
        from pkb.rerank import rerank as _rerank_fn

        t = perf_counter()
        candidates = _rerank_fn(query_text, candidates, top_k=len(candidates))
        apply_canonical_boost(candidates, canonical_boost)
        candidates = _cap_per_doc(candidates, top_k, group_by_canonical=canonical_group)
        timings["rerank_ms"] = round((perf_counter() - t) * 1000, 2)
    else:
        candidates = _cap_per_doc(candidates, top_k, group_by_canonical=canonical_group)

    if expand_context > 0:
        t = perf_counter()
        candidates = _attach_neighbors(es, candidates, window=expand_context)
        timings["expand_ms"] = round((perf_counter() - t) * 1000, 2)

    timings["total_ms"] = round((perf_counter() - t_total) * 1000, 2)

    if log:
        try:
            from pkb.search_log import log_search

            log_search(
                query=query_text,
                category=category,
                top_k=top_k,
                fusion="rrf",
                reranked=rerank,
                results=candidates,
                latency_ms=timings,
                variants=queries[1:],
            )
        except Exception:
            pass  # 로깅 실패는 검색을 막지 않음

    return candidates


def _attach_neighbors(
    es: Elasticsearch, hits: list[dict], window: int = 1
) -> list[dict]:
    """각 hit의 전후 window개 청크를 neighbors 필드로 부착 (동일 doc_id 내).
    검색 결과를 상위 맥락과 함께 반환할 때 사용. msearch 한 번으로 전 히트를 조회한다."""
    searchable: list[tuple[dict, int]] = []
    searches: list[dict] = []
    for hit in hits:
        doc_id = hit.get("doc_id")
        ci = hit.get("chunk_index")
        if doc_id is None or ci is None:
            hit["neighbors"] = []
            continue

        start = max(0, ci - window)
        end = ci + window
        searchable.append((hit, ci))
        searches.extend([
            {},
            {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"doc_id": doc_id}},
                            {"range": {"chunk_index": {"gte": start, "lte": end}}},
                        ]
                    }
                },
                "size": window * 2 + 1,
                "_source": {"excludes": ["embedding"]},
                "sort": [{"chunk_index": {"order": "asc"}}],
            },
        ])

    if not searches:
        return hits

    result = es.msearch(index=settings.es_index, searches=searches)
    responses = result.get("responses", [])
    for (hit, ci), response in zip(searchable, responses, strict=False):
        neighbors = []
        for nh in response.get("hits", {}).get("hits", []):
            src = nh["_source"]
            if src.get("chunk_index") == ci:
                continue  # 자기 자신 제외
            neighbors.append(
                {
                    "chunk_index": src.get("chunk_index"),
                    "section_path": src.get("section_path"),
                    "content": src.get("content"),
                }
            )
        hit["neighbors"] = neighbors
    for hit, _ in searchable[len(responses):]:
        hit["neighbors"] = []
    return hits


def _rrf_search(
    es: Elasticsearch,
    query_text: str,
    query_vector: list[float],
    category: str | None,
    candidate_k: int,
    timings: dict[str, float] | None = None,
    include_archived: bool = False,
    exclude_doc_prefix: str | None = None,
    profile: str | None = None,
    retrieval_profile: str | None = None,
) -> list[dict]:
    """BM25와 kNN을 각각 실행 → Reciprocal Rank Fusion으로 결합.

    timings이 주어지면 bm25_ms/knn_ms/fusion_ms/candidate_count/rrf_top_gap 기록.
    include_archived=False(기본)면 archived/expired 문서는 검색에서 제외.
    exclude_doc_prefix가 주어지면 해당 doc_id 접두사 문서도 검색에서 제외.
    profile은 BM25/kNN 양쪽에 동일한 frontmatter 필터를 적용한다.
    """
    # BM25·kNN을 msearch 한 요청으로 묶는다 — HTTP 왕복 2회→1회.
    # (ES 내부 하위 검색 비용은 동일. Basic 라이선스라 네이티브 RRF retriever는 403 — 클라이언트 RRF 유지)
    t = perf_counter()
    result = es.msearch(
        index=settings.es_index,
        searches=[
            {},
            {
                "query": _bm25_query(
                    query_text, category, include_archived=include_archived,
                    exclude_doc_prefix=exclude_doc_prefix,
                    profile=profile,
                    retrieval_profile=retrieval_profile,
                ),
                "size": candidate_k,
                "_source": {"excludes": ["embedding"]},
            },
            {},
            {
                "knn": _knn_query(
                    query_vector, candidate_k, category, include_archived=include_archived,
                    exclude_doc_prefix=exclude_doc_prefix,
                    profile=profile,
                    retrieval_profile=retrieval_profile,
                ),
                "size": candidate_k,
                "_source": {"excludes": ["embedding"]},
            },
        ],
    )
    bm25_result, knn_result = result["responses"]
    for name, resp in (("bm25", bm25_result), ("knn", knn_result)):
        if resp.get("error"):
            raise RuntimeError(f"{name} 검색 실패: {resp['error']}")
    if timings is not None:
        timings["msearch_ms"] = round((perf_counter() - t) * 1000, 2)
        # 하위 검색별 서버측 소요시간 (ES took, ms)
        timings["bm25_ms"] = bm25_result.get("took", 0)
        timings["knn_ms"] = knn_result.get("took", 0)

    t = perf_counter()
    # doc_id(_id) → {rrf_score, source}
    combined: dict[str, dict] = {}
    for rank, hit in enumerate(bm25_result["hits"]["hits"]):
        doc_id = hit["_id"]
        rrf = 1.0 / (RRF_K + rank + 1)
        combined[doc_id] = {"score": rrf, "source": _source_to_dict(hit)}

    for rank, hit in enumerate(knn_result["hits"]["hits"]):
        doc_id = hit["_id"]
        rrf = 1.0 / (RRF_K + rank + 1)
        if doc_id in combined:
            combined[doc_id]["score"] += rrf
        else:
            combined[doc_id] = {"score": rrf, "source": _source_to_dict(hit)}

    sorted_hits = sorted(combined.values(), key=lambda x: -x["score"])
    results = []
    for item in sorted_hits:
        source = item["source"]
        source["score"] = item["score"]
        results.append(source)

    if timings is not None:
        timings["fusion_ms"] = round((perf_counter() - t) * 1000, 2)
        timings["candidate_count"] = len(sorted_hits)
        if len(sorted_hits) >= 2:
            timings["rrf_top_gap"] = round(
                sorted_hits[0]["score"] - sorted_hits[1]["score"], 6
            )
        else:
            timings["rrf_top_gap"] = 0.0
    return results
