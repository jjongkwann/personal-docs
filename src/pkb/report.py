"""PKB 상태 점검(doctor) 리포트 생성. CLI/MCP 공용.

조치 후보(만료 미아카이브·purge 대상·고아 개념)는 결정적으로 나열만 한다 —
판단·해소는 Claude 세션 몫.
"""

from dataclasses import dataclass

# purge 후보 판정: 아카이브 후 경과일. ES date math에 박히는 리터럴 상수 (설정 아님).
PURGE_CANDIDATE_DAYS = 30

LAUNCHD_LABEL = "dev.jongkwan.pkb-mcp"


@dataclass(frozen=True)
class HealthReport:
    text: str
    ok: bool


def _sh(cmd: list[str]) -> str:
    import subprocess

    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _launchd_restart_warning(info: str, uptime: str) -> bool:
    """현재 정상 실행 중인 수동 재시작과 짧은 간격의 비정상 재기동을 구분."""
    fields: dict[str, str] = {}
    for line in info.splitlines():
        stripped = line.strip()
        if " = " in stripped:
            key, value = stripped.split(" = ", 1)
            fields.setdefault(key, value)

    runs = fields.get("runs", "")
    state = fields.get("state", "")
    last_exit = fields.get("last exit code", "")
    if not runs.isdigit() or int(runs) < 3:
        return False
    if state and state != "running":
        return True

    # 실행 중이어도 비정상 종료가 짧은 시간 안에 반복됐다면 크래시 루프로 본다.
    if last_exit in {"", "0", "143"}:  # 143=SIGTERM, launchctl kickstart의 정상 종료
        return False
    parts = uptime.strip().split(":")
    try:
        if "-" in parts[0]:
            days, hours = (int(v) for v in parts[0].split("-", 1))
            seconds = days * 86400 + hours * 3600
        elif len(parts) == 3:
            seconds = int(parts[0]) * 3600
        else:
            seconds = 0
        seconds += int(parts[-2]) * 60 + int(parts[-1])
    except (ValueError, IndexError):
        return False
    return seconds < 300


def _server_status() -> tuple[list[str], bool]:
    """공유 HTTP MCP 서버의 프로세스 상태.

    doctor는 서버 안(MCP 도구)에서도 밖(CLI)에서도 불리므로, 자기 프로세스가 아니라
    포트를 기준으로 본다. 메모리는 RSS가 아니라 footprint — 유휴 프로세스는 페이지가
    압축·스왑돼 RSS가 수십 MB로 보이지만 실제로는 GB를 물고 있다.
    """
    import socket

    from pkb.config import resolve_device
    from pkb.config import settings as _settings

    port = _settings.mcp_port
    lines = [f"=== MCP 서버 (http://127.0.0.1:{port}/mcp) ==="]

    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            lines.append("⚠ LISTEN 안 함 — Claude/Codex/Gemini 모두 pkb 도구를 쓸 수 없다")
            lines.append(f"  → launchctl load ~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
            return lines, False

    pid = _sh(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).split("\n")[0]
    uptime = _sh(["ps", "-o", "etime=", "-p", pid]) if pid else ""
    footprint = ""
    if pid:
        # top의 MEM은 압축분을 포함한 footprint. 마지막 줄이 해당 pid의 값.
        out = _sh(["top", "-l", "1", "-pid", pid, "-stats", "mem"]).splitlines()
        footprint = out[-1].strip() if out else ""

    lines.append(f"LISTEN  pid {pid or '?'}  가동 {uptime or '?'}  메모리 {footprint or '?'}")
    lines.append(
        f"device: embedding={resolve_device(_settings.embedding_device)}"
        f" rerank={resolve_device(_settings.rerank_device)}"
        f"  warmup_on_start={_settings.warmup_on_start}"
    )

    # launchd 재시작 횟수 — 크래시 루프(포트 충돌 등)는 여기서만 드러난다
    import os

    info = _sh(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
    runs = next((ln.split("=")[1].strip() for ln in info.splitlines() if "runs =" in ln), "")
    if runs:
        warn = (
            "  ⚠ 재기동 반복 — pkb-mcp.err.log 확인"
            if _launchd_restart_warning(info, uptime)
            else ""
        )
        lines.append(f"launchd: {LAUNCHD_LABEL}  누적 기동 {runs}회{warn}")
    else:
        lines.append(f"launchd: {LAUNCHD_LABEL} 미등록 (수동 기동 중)")

    return lines, True


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


def build_health_report_status(es) -> HealthReport:
    """PKB 상태 문자열과 필수 구성요소(MCP·ES·인덱스)의 정상 여부를 반환."""
    from pkb.config import settings as _settings
    from pkb.store import count_chunks_without_hash, count_documents

    lines = ["=== PKB Doctor ==="]
    server_lines, healthy = _server_status()
    lines.extend(server_lines)
    lines.append("")

    # ES 연결
    try:
        info = es.info()
        lines.append(f"ES: {info['version']['number']} ({_settings.es_host})")
    except Exception as e:
        lines.append(f"ES: 연결 실패 — {e}")
        return HealthReport("\n".join(lines), ok=False)

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
            healthy = False
    except Exception as e:
        lines.append(f"인덱스 조회 실패: {e}")
        healthy = False

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
        from pkb.graph.schema import graph_connection

        db_path = Path(_settings.graph_db_path)
        if not db_path.exists():
            lines.append("그래프 DB 없음 (아직 빌드되지 않음)")
        else:
            with graph_connection(_settings.graph_db_path) as conn:
                s = gstore.stats(conn)
                evidence_covered, evidence_total = gstore.edge_evidence_coverage(conn)
                evidence_rebuild_active = gstore.edge_evidence_rebuild_active(conn)
                orphans = gstore.orphan_concept_slugs(conn)
            if not s["concepts"]:
                lines.append("그래프가 비어 있음 (개념 0개)")
            else:
                for k, v in s.items():
                    lines.append(f"  {k}: {v}")
                if evidence_rebuild_active:
                    lines.append(
                        "  ⚠ edge evidence staging 재구축 중 — 기존 그래프 서비스 유지, "
                        "전량 추출 뒤 `pkb graph finalize-evidence --yes` 필요"
                    )
                elif evidence_covered < evidence_total:
                    lines.append(
                        f"  ⚠ edge evidence coverage: {evidence_covered}/{evidence_total} "
                        "— `pkb graph reset-evidence --yes` 후 전량 재추출 필요"
                    )
                if orphans:
                    lines.append(f"  고아 개념(멘션 0): {len(orphans)}")
                    for slug in orphans[:10]:  # ES 측 후보 나열과 동일한 10건 캡
                        lines.append(f"    - {slug}")
                    if len(orphans) > 10:
                        lines.append(f"    ... 외 {len(orphans) - 10}개")

            # 그래프 미추출 청크 — ES 청크 전량을 SQLite 추출 마커와 대조 (graph_list_chunks의
            # pending_only와 같은 판정). ES 조회 실패는 조용히 생략.
            try:
                from pkb.graph.services import scan_pending_chunks

                with graph_connection(_settings.graph_db_path) as conn:
                    pending, total_chunks = scan_pending_chunks(es, conn)
                lines.append(f"그래프 미추출 청크: {len(pending)} / {total_chunks}")
            except Exception:
                pass
    except Exception as e:
        lines.append(f"그래프 통계 조회 실패: {e}")

    return HealthReport("\n".join(lines), ok=healthy)


def build_health_report(es) -> str:
    """하위호환 문자열 API. 종료코드가 필요한 CLI는 build_health_report_status를 사용."""
    return build_health_report_status(es).text
