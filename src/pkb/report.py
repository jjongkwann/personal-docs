"""PKB 상태 점검(doctor) 리포트 생성. CLI/MCP 공용.

조치 후보(만료 미아카이브·purge 대상·고아 개념)는 결정적으로 나열만 한다 —
판단·해소는 Claude 세션 몫.
"""

# purge 후보 판정: 아카이브 후 경과일. ES date math에 박히는 리터럴 상수 (설정 아님).
PURGE_CANDIDATE_DAYS = 30


def _doc_id_agg(es, query: dict) -> tuple[int, list[str]]:
    """query에 걸리는 청크 총수 + doc_id 최대 10건을 terms 집계로 반환. 조치 후보 나열용."""
    from pkb.config import settings as _settings

    result = es.search(
        index=_settings.es_index,
        size=0,
        track_total_hits=True,  # 기본 10000 상한 포화 방지 — 총수는 정확히
        query=query,
        aggs={"by_doc": {"terms": {"field": "doc_id", "size": 10}}},
    )
    doc_ids = [b["key"] for b in result["aggregations"]["by_doc"]["buckets"]]
    return result["hits"]["total"]["value"], doc_ids


def build_health_report(es) -> str:
    """PKB 시스템 상태 점검 문자열 생성: ES 연결/인덱스/설정 + 개념 그래프 통계."""
    from pkb.config import settings as _settings
    from pkb.store import count_chunks_without_hash, count_documents

    lines = ["=== PKB Doctor ==="]

    # ES 연결
    try:
        info = es.info()
        lines.append(f"ES: {info['version']['number']} ({_settings.es_host})")
    except Exception as e:
        lines.append(f"ES: 연결 실패 — {e}")
        return "\n".join(lines)

    # 인덱스
    try:
        if es.indices.exists(index=_settings.es_index):
            count = count_documents(es)
            lines.append(f"인덱스 '{_settings.es_index}': {count}개 청크")

            # 카테고리별 집계
            agg = es.search(
                index=_settings.es_index,
                size=0,
                aggs={"by_cat": {"terms": {"field": "category", "size": 20}}},
            )
            for bucket in agg["aggregations"]["by_cat"]["buckets"]:
                lines.append(f"  - {bucket['key']}: {bucket['doc_count']}")

            # Lifecycle 집계: archived 청크 수 + 조치 후보(만료 미아카이브 / purge 대상) 나열
            try:
                archived = es.count(
                    index=_settings.es_index,
                    query={"exists": {"field": "archived_at"}},
                )["count"]
                expired, expired_docs = _doc_id_agg(
                    es,
                    {
                        "bool": {
                            "must": [
                                {"exists": {"field": "expires_at"}},
                                {"range": {"expires_at": {"lte": "now"}}},
                            ],
                            "must_not": [{"exists": {"field": "archived_at"}}],
                        }
                    },
                )
                lines.append(f"  archived: {archived}  expired(still-visible): {expired}")
                lines.extend(f"    - {doc_id}" for doc_id in expired_docs)

                purge_total, purge_docs = _doc_id_agg(
                    es,
                    {"range": {"archived_at": {"lte": f"now-{PURGE_CANDIDATE_DAYS}d/d"}}},
                )
                if purge_total:
                    lines.append(
                        f"  purge 후보(archived {PURGE_CANDIDATE_DAYS}일 경과): {purge_total}"
                    )
                    lines.extend(f"    - {doc_id}" for doc_id in purge_docs)
                    lines.append("    → `pkb purge-archived`로 정리 (CLI 전용)")
            except Exception:
                pass

            # 설정 vs 인덱스 드리프트 (연동 껐는데 문서 잔존)
            try:
                if not _settings.obsidian_path:
                    from pkb.store import list_doc_ids

                    leftover = len(list_doc_ids(es, "obsidian/"))
                    if leftover:
                        lines.append(
                            f"  ⚠️ OBSIDIAN_PATH 미설정인데 obsidian 문서 {leftover}개 잔존"
                            " — `pkb sync`로 정리"
                        )
            except Exception:
                pass

            # 델타 임베딩 마이그레이션 진행도
            try:
                no_hash = count_chunks_without_hash(es)
                if no_hash:
                    lines.append(
                        f"  chunks without content_hash: {no_hash} / {count} "
                        f"(touch 또는 reindex로 점진 백필)"
                    )
                else:
                    lines.append("  content_hash: 모든 청크 백필 완료")
            except Exception:
                pass
        else:
            lines.append(f"인덱스 '{_settings.es_index}': 없음. `pkb init` 필요")
    except Exception as e:
        lines.append(f"인덱스 조회 실패: {e}")

    # 설정
    lines.append("\n=== 설정 ===")
    lines.append(f"embedding_model: {_settings.embedding_model}")
    lines.append(f"rerank_model: {_settings.rerank_model}")
    lines.append(f"rerank_enabled: {_settings.rerank_enabled}")
    lines.append(f"candidate_k: {_settings.candidate_k}")
    lines.append(f"chunk_size: {_settings.chunk_size}, overlap: {_settings.chunk_overlap}")
    lines.append(f"data_root: {_settings.data_root}")
    lines.append(f"obsidian_path: {_settings.obsidian_path or '(미설정)'}")
    if _settings.obsidian_path:
        from pathlib import Path as _Path

        from pkb.config import data_dir as _data_dir

        vault = _Path(_settings.obsidian_path).expanduser().resolve()
        if not _data_dir().is_relative_to(vault):
            lines.append("  ⚠ 그래프뷰 상동 비활성(DATA_ROOT가 볼트 밖)")

    # 개념 그래프
    lines.append("\n=== 개념 그래프 ===")
    try:
        from pathlib import Path

        from pkb.graph import store as gstore
        from pkb.graph.schema import get_connection, init_schema

        db_path = Path(_settings.graph_db_path)
        if not db_path.exists():
            lines.append("그래프 DB 없음 (아직 빌드되지 않음)")
        else:
            init_schema(_settings.graph_db_path)  # extracted_chunks 등 신규 테이블 백필
            with get_connection(_settings.graph_db_path) as conn:
                s = gstore.stats(conn)
                orphans = gstore.orphan_concept_slugs(conn)
                by_idx, legacy = gstore.extracted_markers(conn)
            if not s["concepts"]:
                lines.append("그래프가 비어 있음 (개념 0개)")
            else:
                for k, v in s.items():
                    lines.append(f"  {k}: {v}")
                if orphans:
                    from pkb.config import data_dir
                    from pkb.ingest import CONCEPTS_DIR_NAME

                    concepts_dir = data_dir() / CONCEPTS_DIR_NAME
                    lines.append(f"  고아 개념(멘션 0): {len(orphans)}")
                    for slug in orphans[:10]:  # ES 측 후보 나열과 동일한 10건 캡
                        note = " (노트 잔존)" if (concepts_dir / f"{slug}.md").exists() else ""
                        lines.append(f"    - {slug}{note}")
                    if len(orphans) > 10:
                        lines.append(f"    ... 외 {len(orphans) - 10}개")

            # 그래프 미추출 청크 — ES 청크 전량을 SQLite 추출 마커와 대조 (graph_list_chunks의
            # pending_only와 같은 판정). ES 조회 실패는 조용히 생략.
            try:
                # ponytail: 개인 규모 상한(10000) — 초과 시 search_after로 업그레이드
                scan = es.search(
                    index=_settings.es_index,
                    size=10000,
                    track_total_hits=True,
                    source_includes=["doc_id", "chunk_index", "content_hash"],
                )
                total_chunks = scan["hits"]["total"]["value"]
                pending = sum(
                    1
                    for h in scan["hits"]["hits"]
                    if gstore.is_pending(h["_source"], by_idx, legacy)
                )
                lines.append(f"그래프 미추출 청크: {pending} / {total_chunks}")
            except Exception:
                pass
    except Exception as e:
        lines.append(f"그래프 통계 조회 실패: {e}")

    return "\n".join(lines)
