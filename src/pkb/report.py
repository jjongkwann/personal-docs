"""PKB 상태 점검(doctor) 리포트 생성. CLI/MCP 공용."""


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

            # Lifecycle 집계: archived / expired 청크 수
            try:
                archived = es.count(
                    index=_settings.es_index,
                    query={"exists": {"field": "archived_at"}},
                )["count"]
                expired = es.count(
                    index=_settings.es_index,
                    query={
                        "bool": {
                            "must": [
                                {"exists": {"field": "expires_at"}},
                                {"range": {"expires_at": {"lte": "now"}}},
                            ],
                            "must_not": [{"exists": {"field": "archived_at"}}],
                        }
                    },
                )["count"]
                lines.append(f"  archived: {archived}  expired(still-visible): {expired}")
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
        from pkb.graph.schema import get_connection

        db_path = Path(_settings.graph_db_path)
        if not db_path.exists():
            lines.append("그래프 DB 없음 (아직 빌드되지 않음)")
        else:
            with get_connection(_settings.graph_db_path) as conn:
                s = gstore.stats(conn)
            if not s["concepts"]:
                lines.append("그래프가 비어 있음 (개념 0개)")
            else:
                for k, v in s.items():
                    lines.append(f"  {k}: {v}")
    except Exception as e:
        lines.append(f"그래프 통계 조회 실패: {e}")

    return "\n".join(lines)
