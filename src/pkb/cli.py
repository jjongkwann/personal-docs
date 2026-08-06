# cli.py에는 내장 `list`를 가리는 `list` 커맨드가 있어, 런타임 평가 어노테이션(list[str] 등)이
# 함수 첨자 TypeError를 일으킨다 — 지연 평가로 모듈 전체를 방어.
from __future__ import annotations

import sys
from pathlib import Path

import typer

from pkb.config import data_dir, settings

app = typer.Typer(help="PKB - Personal Knowledge Base")


@app.command()
def init(
    ingest_obsidian: bool = typer.Option(
        True, help="OBSIDIAN_PATH가 설정돼 있으면 초기 인제스트 수행"
    ),
):
    """ES 인덱스 초기화 (+ Obsidian 볼트 초기 인제스트)."""
    from pkb.ingest import find_ingestable_files, ingest_files
    from pkb.store import create_index, get_client

    es = get_client()
    create_index(es)
    typer.echo(f"인덱스 '{settings.es_index}' 생성 완료.")

    if ingest_obsidian and settings.obsidian_path:
        vault = Path(settings.obsidian_path).expanduser().resolve()
        if not vault.is_dir():
            typer.echo(f"OBSIDIAN_PATH 디렉터리를 찾을 수 없습니다: {vault}")
            raise typer.Exit(1)

        files = find_ingestable_files(vault, exclude=data_dir())
        typer.echo(f"Obsidian 초기 인제스트: {len(files)}개 파일 감지 ({vault})")
        from pkb.ingest import format_delta_stats
        stats = ingest_files(
            files,
            base_dir=vault,
            doc_id_prefix="obsidian/",
        )
        typer.echo(f"Obsidian 인제스트 완료: {format_delta_stats(stats)}")


@app.command()
def reindex(
    confirm: bool = typer.Option(False, "--yes", "-y", help="확인 없이 바로 실행"),
):
    """ES 인덱스를 완전히 삭제하고 재생성한 뒤 data/ + OBSIDIAN_PATH 전체 재인제스트.

    매핑 스키마가 바뀌었거나 (예: section_path 추가) 인덱스 상태가 꼬였을 때 사용.
    """
    from pkb.ingest import find_ingestable_files, format_delta_stats, ingest_files
    from pkb.store import create_index, delete_index, get_client

    if not confirm:
        typer.echo("⚠️  인덱스를 완전히 삭제하고 재생성합니다.")
        typer.echo(f"   인덱스: {settings.es_index}")
        typer.echo(f"   대상: {settings.data_root} + {settings.obsidian_path or '(OBSIDIAN_PATH 미설정)'}")
        if not typer.confirm("진행하시겠습니까?"):
            typer.echo("취소.")
            raise typer.Exit(0)

    es = get_client()
    typer.echo("1. 인덱스 삭제...")
    delete_index(es)
    typer.echo("2. 인덱스 재생성 (새 매핑)...")
    create_index(es)

    # data 코퍼스 재인제스트
    data_root = data_dir()
    if data_root.is_dir():
        data_files = find_ingestable_files(data_root)
        typer.echo(f"3. data 재인제스트: {len(data_files)}개 파일 ({data_root})")
        data_stats = ingest_files(data_files, base_dir=data_root, doc_id_prefix="data/")
        typer.echo(f"   → {format_delta_stats(data_stats)}")

    # Obsidian 재인제스트
    if settings.obsidian_path:
        vault = Path(settings.obsidian_path).expanduser().resolve()
        if vault.is_dir():
            vault_files = find_ingestable_files(vault, exclude=data_root)
            typer.echo(f"4. Obsidian 재인제스트: {len(vault_files)}개 파일")
            vault_stats = ingest_files(
                vault_files,
                base_dir=vault,
                doc_id_prefix="obsidian/",
            )
            typer.echo(f"   → {format_delta_stats(vault_stats)}")

    typer.echo("\n완료.")


@app.command("index-switch")
def index_switch(
    new_index: str = typer.Argument(
        ..., help="전환할 물리 인덱스 이름 (예: pkb_documents_v2)"
    ),
):
    """읽기 alias(ES_INDEX)를 새 물리 인덱스로 원자 전환합니다.

    모델·차원 변경 시: 새 물리 인덱스를 ES_INDEX 오버라이드로 채운 뒤 이 명령으로 전환.
    """
    from pkb.store import get_client, switch_read_alias

    try:
        old = switch_read_alias(get_client(), new_index)
    except ValueError as e:
        typer.echo(f"오류: {e}")
        raise typer.Exit(1) from None
    prev = f" (이전: {', '.join(old)})" if old else " (신규 alias)"
    typer.echo(f"alias 전환 완료: {settings.es_index} → {new_index}{prev}")


@app.command()
def sync(
    yes: bool = typer.Option(False, "--yes", "-y", help="대량 삭제 확인 생략"),
):
    """데이터 코퍼스·Obsidian 볼트를 ES와 재조정 (업서트 + 원본에 없는 유령 문서 정리).

    설정이 곧 기대 상태: OBSIDIAN_PATH를 제거했다면 잔존 obsidian/* 문서를 정리한다.
    """
    from pkb.ingest import format_delta_stats
    from pkb.operations import graph_prune_summary, prune_documents, sync_tree
    from pkb.store import (
        PRUNE_CONFIRM_THRESHOLD,
        get_client,
        list_doc_ids,
    )

    es = get_client()

    def prune(stale: list[str]):
        if not stale:
            return
        if len(stale) > PRUNE_CONFIRM_THRESHOLD and not yes:
            typer.echo(f"   원본에 없는 문서 {len(stale)}개:")
            for d in stale[:10]:
                typer.echo(f"     - {d}")
            if len(stale) > 10:
                typer.echo(f"     ... 외 {len(stale) - 10}개")
            if not typer.confirm(f"   {len(stale)}개 삭제하시겠습니까?"):
                typer.echo("   삭제 보류.")
                return
        prune_documents(es, stale)
        typer.echo(f"   정리: 원본에 없는 문서 {len(stale)}개 삭제")

    # 1) data 코퍼스
    root = data_dir()
    if root.is_dir():
        outcome = sync_tree(es, root, "data/")
        typer.echo(
            f"1. data 코퍼스 동기화: {outcome.stats['files']}개 파일 ({outcome.root})"
        )
        typer.echo(f"   → {format_delta_stats(outcome.stats)}")
        prune([*outcome.stale])
    else:
        # 루트 소실은 연동 해제가 아니라 설정 오류일 가능성이 높다 → 정리하지 않음
        typer.echo(f"1. data 코퍼스 루트 없음 — 건너뜀 (정리 안 함): {root}")

    # 2) Obsidian 볼트
    if settings.obsidian_path:
        vault = Path(settings.obsidian_path).expanduser().resolve()
        if vault.is_dir():
            outcome = sync_tree(es, vault, "obsidian/", exclude=root)
            typer.echo(
                f"2. Obsidian 동기화: {outcome.stats['files']}개 파일 ({outcome.root})"
            )
            typer.echo(f"   → {format_delta_stats(outcome.stats)}")
            prune([*outcome.stale])
        else:
            typer.echo(f"2. OBSIDIAN_PATH 디렉터리 없음 — 건너뜀 (정리 안 함): {vault}")
    else:
        typer.echo("2. Obsidian 연동 꺼짐 (OBSIDIAN_PATH 미설정)")
        prune(sorted(list_doc_ids(es, "obsidian/")))

    graph_summary = graph_prune_summary(es)
    if graph_summary:
        typer.echo(graph_summary)

    typer.echo("\n완료.")


@app.command()
def convert(
    input_path: Path = typer.Argument(..., help="변환할 파일 경로 (PDF, DOCX, PPTX, XLSX, HTML)"),
    category: str = typer.Option(
        "misc",
        help="저장 폴더 = 카테고리 (하위 경로 가능, 예: study/payments)",
    ),
    output: Path = typer.Option(None, help="저장 경로 (기본: data/<category>/<파일명>.md)"),
    ingest: bool = typer.Option(True, help="변환 후 자동 인제스트"),
):
    """PDF/DOCX/PPTX/XLSX/HTML을 마크다운으로 변환하여 data/에 저장."""
    from pkb.ingest import format_delta_stats
    from pkb.operations import (
        OperationError,
        TranscriptionRequiredError,
        convert_and_ingest,
    )

    try:
        outcome = convert_and_ingest(
            input_path,
            category=category,
            output=output,
            ingest=ingest,
        )
    except TranscriptionRequiredError as exc:
        typer.echo(f"{exc} — 직접 전사 필요")
        raise typer.Exit(1) from exc
    except OperationError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo(f"변환 완료: {outcome.output_path} ({outcome.chars}자)")
    if outcome.stats is not None:
        typer.echo(f"인제스트 완료: {format_delta_stats(outcome.stats)}")


@app.command()
def add(
    path: Path = typer.Argument(
        None, help="파일/디렉터리 경로 (기본: data 코퍼스 전체). data 코퍼스 하위여야 함."
    ),
    tags: str = typer.Option("", help="쉼표 구분 태그 (예: python,backend)"),
):
    """문서를 인제스트하여 ES에 저장."""
    from pkb.ingest import find_ingestable_files, format_delta_stats, ingest_files

    base_dir = data_dir()
    if path is None:
        path = base_dir
    elif not path.is_absolute() and (str(path) == "data" or str(path).startswith("data/")):
        # doc_id 형식 ("data/...") — 코퍼스 루트가 어디 있든 동일하게 동작
        path = (base_dir / str(path).removeprefix("data").lstrip("/")).resolve()
    else:
        path = path.resolve()
    if not path.is_relative_to(base_dir):
        typer.echo(f"data 코퍼스({base_dir}) 하위 경로만 인제스트할 수 있습니다: {path}")
        raise typer.Exit(1)
    files = find_ingestable_files(path)
    if not files:
        typer.echo(f"인제스트 가능한 파일을 찾을 수 없습니다: {path}")
        raise typer.Exit(1)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    stats = ingest_files(
        files, base_dir=base_dir, doc_id_prefix="data/", tag_override=tag_list
    )
    typer.echo(f"\n총 {len(files)}개 파일 처리 — {format_delta_stats(stats)}")


@app.command()
def write(
    file_path: str = typer.Argument(..., help="저장할 파일 경로 (예: data/writing/note.md)"),
    content: str = typer.Option(
        None, help="파일에 작성할 내용. 생략하면 stdin에서 읽음"
    ),
    ingest: bool = typer.Option(True, help="저장 후 자동 인제스트"),
    dry_run: bool = typer.Option(False, help="파일·인덱스를 바꾸지 않고 diff/hash만 표시"),
    expected_hash: str = typer.Option("", help="기존 파일 SHA-256 (낙관적 잠금)"),
    strict_policy: bool = typer.Option(False, help="curated 경로 frontmatter 계약 강제"),
):
    """파일을 작성하고 ES에 인제스트. mcp write_file과 동일 동작(data/ 하위 .md만)."""
    from pkb.ingest import format_delta_stats
    from pkb.operations import OperationError, write_and_ingest

    if content is None:
        content = sys.stdin.read()

    try:
        outcome = write_and_ingest(
            file_path,
            content,
            ingest=ingest,
            dry_run=dry_run,
            expected_hash=expected_hash,
            strict_policy=strict_policy,
        )
    except OperationError as exc:
        typer.echo(f"오류: {exc}")
        raise typer.Exit(1) from exc

    action = "쓰기 미리보기" if outcome.dry_run else "파일 저장 완료"
    typer.echo(f"{action}: {file_path} ({outcome.chars}자)")
    typer.echo(f"previous_hash: {outcome.previous_hash or '<missing>'}")
    typer.echo(f"content_hash: {outcome.content_hash}")
    if outcome.dry_run and outcome.diff:
        typer.echo(outcome.diff)
    if outcome.stats is not None:
        typer.echo(f"인제스트: {format_delta_stats(outcome.stats)}")


@app.command()
def show(
    doc_id: str = typer.Argument(..., help="조회할 문서 ID (예: data/study/rag/1.2.10_BM25.md)"),
    content: bool = typer.Option(
        False, "--content/--no-content", help="청크 본문 포함 여부 (기본: 목차만)"
    ),
    chunks: str = typer.Option("", "--chunks", help='특정 청크만 조회 (예: "3-7")'),
):
    """문서 조회 (기본: 메타+목차, section_path 포함). mcp get_document과 동일."""
    from pkb.documents import fetch_document_sources, render_document
    from pkb.store import get_client

    es = get_client()
    sources = fetch_document_sources(es, doc_id)
    if not sources:
        typer.echo(f"문서를 찾을 수 없습니다: {doc_id}")
        raise typer.Exit(1)

    typer.echo(render_document(doc_id, sources, content, chunks))


@app.command("reindex-doc")
def reindex_doc(
    doc_id: str = typer.Argument(..., help="재인제스트할 문서 ID"),
):
    """단일 문서를 원본 파일로부터 재인제스트. mcp reindex_document과 동일.

    doc_id가 "obsidian/..."으로 시작하면 OBSIDIAN_PATH 하위 파일로 처리.
    """
    from pkb.documents import DocumentPathError, resolve_reindex_target
    from pkb.ingest import format_delta_stats, ingest_files

    try:
        target = resolve_reindex_target(doc_id)
    except DocumentPathError as e:
        typer.echo(f"오류: {e}")
        raise typer.Exit(1) from e

    if not target.file_path.exists():
        typer.echo(f"원본 파일을 찾을 수 없습니다: {target.file_path}")
        raise typer.Exit(1)

    stats = ingest_files(
        [target.file_path], base_dir=target.base_dir, doc_id_prefix=target.doc_id_prefix
    )
    typer.echo(f"재인제스트 완료: {doc_id} — {format_delta_stats(stats)}")


@app.command()
def list(
    category: str = typer.Option(None, help="카테고리 필터 (about, career, study, writing)"),
    limit: int = typer.Option(50, help="표시할 최대 문서 수 (0=전체)"),
):
    """저장된 문서 목록 표시 (date_modified 내림차순, 기본 상위 50개)."""
    from pkb.store import get_client, list_documents

    es = get_client()
    docs = list_documents(es, category)
    if not docs:
        typer.echo("저장된 문서가 없습니다.")
        return

    docs.sort(key=lambda d: d.get("date_modified") or "", reverse=True)
    shown = docs if limit <= 0 else docs[:limit]

    typer.echo(f"총 {len(docs)}개 문서 (표시 {len(shown)}개 — limit·category로 조절)")
    typer.echo(f"{'문서 ID':<40} {'카테고리':<10} {'청크':<6} {'수정일'}")
    typer.echo("-" * 75)
    for doc in shown:
        mtime = doc.get("date_modified", "-")
        typer.echo(
            f"{doc['doc_id']:<40} {doc['category']:<10} {doc['chunks']:<6} {mtime}"
        )


@app.command()
def query(
    question: str = typer.Argument(..., help="검색 질문"),
    category: str = typer.Option(None, help="카테고리 필터"),
    top_k: int = typer.Option(settings.default_top_k, help="결과 수"),
    rerank: bool = typer.Option(None, help="CrossEncoder 재순위 사용 (기본: 설정값)"),
    expand: int = typer.Option(None, help="전후 N청크 neighbors 포함 (기본: 설정값)"),
    include_obsidian: bool = typer.Option(
        True, "--include-obsidian/--no-obsidian", help="Obsidian 볼트 노트 포함 여부"
    ),
    profile: str = typer.Option("all", help="검색 프로필: all|curated|evidence|source"),
    canonical_group: bool = typer.Option(True, help="canonical_id 기준 결과 다양화"),
    canonical_boost: float = typer.Option(0.15, help="canonical_id 문서 상대 점수 가산율"),
):
    """하이브리드 검색 (BM25 + kNN + RRF + 옵션 리랭커)."""
    from pkb.retrieve import hybrid_search
    from pkb.store import get_client

    es = get_client()
    results = hybrid_search(
        es, question,
        category=category, top_k=top_k,
        candidate_k=settings.candidate_k,
        rerank=rerank if rerank is not None else settings.rerank_enabled,
        expand_context=expand if expand is not None else settings.expand_context,
        exclude_doc_prefix="obsidian/" if not include_obsidian else None,
        profile=profile,
        canonical_group=canonical_group,
        canonical_boost=canonical_boost,
    )

    if not results:
        typer.echo("검색 결과가 없습니다.")
        return

    for i, r in enumerate(results, 1):
        typer.echo(f"\n{'='*60}")
        typer.echo(f"[{i}] {r['source_path']} (chunk #{r['chunk_index']})")
        sp = r.get('section_path', '')
        if sp:
            typer.echo(f"    섹션: {sp}")
        contract = ""
        if r.get("doc_type"):
            contract += f" | 유형: {r['doc_type']}"
        if r.get("canonical_id"):
            contract += f" | 정본: {r['canonical_id']}"
        typer.echo(f"    카테고리: {r['category']} | 점수: {r['score']:.4f}{contract}")
        typer.echo(f"{'─'*60}")
        # 내용 미리보기 (처음 300자)
        preview = r["content"][:300]
        if len(r["content"]) > 300:
            preview += "..."
        typer.echo(preview)


def _graph_purge(doc_id: str) -> dict | None:
    """그래프 DB가 있으면 doc_id 관련 mentions/documents 정리. 없으면 None."""
    if not Path(settings.graph_db_path).exists():
        return None
    from pkb.graph import store as gstore
    from pkb.graph.schema import graph_connection

    with graph_connection(settings.graph_db_path) as conn:
        return gstore.purge_document(conn, doc_id)


@app.command()
def delete(
    doc_id: str = typer.Argument(..., help="삭제할 문서 ID"),
):
    """문서 및 모든 청크 삭제 (하드 삭제, 비가역)."""
    from pkb.store import delete_document, get_client

    es = get_client()
    deleted = delete_document(es, doc_id)
    typer.echo(f"'{doc_id}' 삭제 완료 ({deleted}개 청크).")

    result = _graph_purge(doc_id)
    if result and (result["mentions_pruned"] or result["documents_pruned"]):
        typer.echo(
            f"그래프 정리: mentions {result['mentions_pruned']}·"
            f"documents {result['documents_pruned']}"
        )


@app.command()
def archive(
    doc_id: str = typer.Argument(..., help="아카이브할 문서 ID"),
    reason: str = typer.Option("", help="아카이브 사유 (선택)"),
):
    """문서를 soft delete(검색에서 제외, 복구 가능)로 아카이브."""
    from pkb.documents import archive_document

    try:
        result = archive_document(doc_id, reason)
    except Exception as e:
        typer.echo(f"오류: {type(e).__name__}: {e}")
        raise typer.Exit(1) from e
    typer.echo(result.message)
    if not result.found:
        raise typer.Exit(1)


@app.command()
def restore(
    doc_id: str = typer.Argument(..., help="복구할 문서 ID"),
):
    """아카이브된 문서를 복구해 검색에 다시 노출."""
    from pkb.documents import restore_document

    try:
        result = restore_document(doc_id)
    except Exception as e:
        typer.echo(f"오류: {type(e).__name__}: {e}")
        raise typer.Exit(1) from e
    typer.echo(result.message)
    if not result.found:
        raise typer.Exit(1)


@app.command()
def doctor():
    """PKB 시스템 상태 점검. ES 연결, 인덱스, 문서 수, 설정, 개념 그래프 통계 확인."""
    from pkb.report import build_health_report_status
    from pkb.store import get_client

    report = build_health_report_status(get_client())
    typer.echo(report.text)
    if not report.ok:
        raise typer.Exit(1)


@app.command("eval")
def eval_cmd(
    gold: Path = typer.Option(
        None, help="골드셋 JSONL 경로 (기본: <DATA_ROOT>/.eval/gold.jsonl)"
    ),
):
    """검색 품질 평가 — 골드셋을 4개 모드(bm25/knn/rrf/rrf+rerank)로 돌려 recall@k/MRR 비교."""
    from pkb.eval import MODES, TOP_K, evaluate, load_gold
    from pkb.store import get_client

    gold_path = gold.resolve() if gold is not None else data_dir() / ".eval" / "gold.jsonl"
    if not gold_path.is_file():
        typer.echo(f"골드셋 파일이 없습니다: {gold_path}")
        typer.echo('라인당 {"query": "...", "doc_id": "data/..."} JSONL로 작성하세요 (docs/usage.md 참고).')
        raise typer.Exit(1)

    rows = load_gold(gold_path)
    if not rows:
        typer.echo(f"골드셋이 비어 있습니다: {gold_path}")
        raise typer.Exit(1)

    es = get_client()
    typer.echo(f"골드셋 {len(rows)}개 쿼리 × {len(MODES)}개 모드 평가 (top_k={TOP_K})\n")
    typer.echo(evaluate(es, rows))


@app.command("purge-archived")
def purge_archived_cmd(
    before: str = typer.Option(
        "",
        help="이 시점 이전에 아카이브된 것만 삭제 (ISO 날짜, 예: 2024-01-01). 빈 값이면 전체.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="확인 프롬프트 생략"),
):
    """아카이브된 문서를 물리 삭제 (비가역!). 명시 요청 시에만 사용."""
    from datetime import UTC, datetime

    from pkb.store import get_client, purge_archived

    before_dt: datetime | None = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before).replace(tzinfo=UTC)
        except ValueError:
            typer.echo(f"잘못된 ISO 날짜: {before}")
            raise typer.Exit(1) from None

    scope = f"archived_at < {before_dt.isoformat()}" if before_dt else "모든 아카이브"
    if not yes:
        typer.confirm(
            f"PURGE: {scope} 를 영구 삭제합니다. 계속하시겠습니까?",
            abort=True,
        )

    es = get_client()
    n = purge_archived(es, before=before_dt)
    typer.echo(f"Purge 완료: {n}개 청크 물리 삭제")


def count_stale(files: list[Path], last_sync_epoch: float) -> int:
    """last_sync 이후 수정된(mtime 비교) 파일 수.

    mtime 스캔은 삭제를 감지하지 못한다 — 삭제 정리는 sync의 prune이 처리.
    """
    return sum(1 for f in files if f.stat().st_mtime > last_sync_epoch)


@app.command()
def stale(
    quiet: bool = typer.Option(False, "--quiet", help="stale일 때만 출력 (SessionStart 훅용)"),
):
    """마지막 sync 이후 수정된 원본 파일 수 보고. 훅 안전을 위해 항상 exit 0 (fail-open)."""
    try:
        import json
        from datetime import datetime

        from pkb.ingest import find_ingestable_files
        from pkb.search_log import LAST_SYNC_FILE

        if not LAST_SYNC_FILE.is_file():
            typer.echo("PKB: sync 기록 없음 (미동기화) — `uv run pkb sync` 권장")
            return
        ts = datetime.fromisoformat(
            json.loads(LAST_SYNC_FILE.read_text(encoding="utf-8"))["ts"]
        ).timestamp()

        files = find_ingestable_files(data_dir())
        if settings.obsidian_path:
            vault = Path(settings.obsidian_path).expanduser().resolve()
            files += find_ingestable_files(vault, exclude=data_dir())

        n = count_stale(files, ts)
        if n:
            typer.echo(f"PKB: 마지막 sync 이후 수정된 파일 {n}개 — `uv run pkb sync` 권장")
        elif not quiet:
            typer.echo("PKB: 원본 변경 없음 (fresh)")
    except Exception:
        pass  # fail-open — 훅에서 세션을 깨면 안 됨


def snapshot(root: Path, exclude: Path | None = None) -> dict[Path, tuple[float, int]]:
    """인제스트 대상 파일들의 {path: (mtime, size)} 스냅샷. watch의 변경 감지 기준."""
    from pkb.ingest import find_ingestable_files

    out: dict[Path, tuple[float, int]] = {}
    for f in find_ingestable_files(root, exclude=exclude):
        try:
            st = f.stat()
        except OSError:
            continue  # 스캔 도중 삭제됨 — 다음 폴링에서 삭제로 잡힘
        out[f] = (st.st_mtime, st.st_size)
    return out


def snapshot_diff(
    old: dict[Path, tuple[float, int]], new: dict[Path, tuple[float, int]]
) -> tuple[list[Path], list[Path]]:
    """(추가·수정된 파일, 삭제된 파일) 반환. mtime 또는 size가 다르면 수정으로 판정."""
    changed = sorted(p for p, sig in new.items() if old.get(p) != sig)
    deleted = sorted(p for p in old if p not in new)
    return changed, deleted


@app.command()
def watch(
    interval: int = typer.Option(10, help="폴링 간격 (초)"),
):
    """원본 파일 변경을 폴링해 자동 반영 (mtime+size 스냅샷 비교). Ctrl+C로 종료.

    시작 시 sync와 같은 reconcile 1회로 기준선을 잡고, 이후엔 변경 파일만 델타 인제스트한다.
    """
    import time
    from datetime import UTC, datetime

    from pkb.ingest import format_delta_stats, ingest_files, reconcile, write_sync_marker
    from pkb.store import PRUNE_CONFIRM_THRESHOLD, delete_document, get_client

    es = get_client()
    data_root = data_dir()

    trees: list[tuple[Path, str, Path | None]] = []  # (root, doc_id prefix, exclude)
    if data_root.is_dir():
        trees.append((data_root, "data/", None))
    if settings.obsidian_path:
        vault = Path(settings.obsidian_path).expanduser().resolve()
        if vault.is_dir():
            trees.append((vault, "obsidian/", data_root))
    if not trees:
        typer.echo(f"감시할 원본 트리가 없습니다: {data_root} / OBSIDIAN_PATH")
        raise typer.Exit(1)

    for root, prefix, exclude in trees:
        stats, stale_docs = reconcile(es, root, prefix, exclude=exclude)
        typer.echo(f"기준선 {prefix}: {format_delta_stats(stats)}")
        if stale_docs:
            typer.echo(f"   원본에 없는 문서 {len(stale_docs)}개 — `pkb sync`로 정리하세요.")

    snaps = {prefix: snapshot(root, exclude) for root, prefix, exclude in trees}
    typer.echo(f"감시 시작 (interval={interval}s, Ctrl+C로 종료)")

    # ponytail: 별도 디바운스 없음 — 폴링 간격이 곧 디바운스이고 슬롯 델타가 중복 인제스트를
    # 무해화한다. 편집기 저장 폭주가 문제되면 mtime 안정화 대기 추가.
    try:
        while True:
            time.sleep(interval)
            poll_ts = datetime.now(UTC).isoformat()  # 마커 ts는 스냅샷 직전 캡처 (reconcile과 동일 이유)
            applied = False
            for root, prefix, exclude in trees:
                new_snap = snapshot(root, exclude)
                changed, removed = snapshot_diff(snaps[prefix], new_snap)
                if changed:
                    stats = ingest_files(changed, base_dir=root, doc_id_prefix=prefix)
                    applied = True
                    typer.echo(
                        f"[{prefix}] 변경 {len(changed)}개 — {format_delta_stats(stats)}"
                    )
                if len(removed) > PRUNE_CONFIRM_THRESHOLD:
                    typer.echo(
                        f"[{prefix}] 삭제 {len(removed)}개 감지 — "
                        f"임계({PRUNE_CONFIRM_THRESHOLD}) 초과라 자동 삭제 안 함. "
                        "`pkb sync`로 확인 후 정리하세요."
                    )
                else:
                    for p in removed:
                        doc_id = f"{prefix}{p.relative_to(root)}"
                        n = delete_document(es, doc_id)
                        _graph_purge(doc_id)
                        applied = True
                        typer.echo(f"[{prefix}] 삭제: {doc_id} ({n}개 청크)")
                snaps[prefix] = new_snap
            if applied:
                write_sync_marker(poll_ts)  # 델타 반영 성공 — pkb stale 오탐 방지
    except KeyboardInterrupt:
        typer.echo("\nwatch 종료.")


graph_app = typer.Typer(help="개념 그래프 빌드/조회 (SQLite 기반 Graph RAG 보조)")
app.add_typer(graph_app, name="graph")


@graph_app.command("stats")
def graph_stats():
    """그래프 통계 출력."""
    from pkb.graph import store as gstore
    from pkb.graph.schema import graph_connection

    with graph_connection(settings.graph_db_path) as conn:
        s = gstore.stats(conn)
    typer.echo(f"DB: {settings.graph_db_path}")
    for k, v in s.items():
        typer.echo(f"  {k}: {v}")


def _echo_graph_json(result: dict) -> None:
    import json

    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


def _graph_relations(value: str) -> list[str]:
    return [relation.strip() for relation in value.split(",") if relation.strip()]


@graph_app.command("explain")
def graph_explain(
    concept: str = typer.Argument(..., help="개념 이름, slug 또는 alias"),
    edge_limit: int = typer.Option(30, min=1, max=100, help="방향별 관계 수 상한"),
    evidence_limit: int = typer.Option(5, min=0, max=20, help="관계별 근거 수 상한"),
    mention_limit: int = typer.Option(20, min=0, max=100, help="언급 출처 수 상한"),
):
    """개념 하나의 설명·관계·언급 출처를 근거와 함께 조회."""
    from pkb.graph import query as graph_queries
    from pkb.graph.schema import graph_connection

    with graph_connection(settings.graph_db_path) as conn:
        result = graph_queries.explain(
            conn,
            concept,
            edge_limit=edge_limit,
            evidence_limit=evidence_limit,
            mention_limit=mention_limit,
        )
    _echo_graph_json(result)


@graph_app.command("path")
def graph_path(
    source: str = typer.Argument(..., help="시작 개념 이름, slug 또는 alias"),
    target: str = typer.Argument(..., help="도착 개념 이름, slug 또는 alias"),
    max_hops: int = typer.Option(4, min=1, max=8, help="최대 탐색 거리"),
    directed: bool = typer.Option(False, help="저장된 src→dst 방향만 탐색"),
    relation: str = typer.Option("", "--relation", "-r", help="관계 타입 필터(쉼표 구분)"),
    evidence_limit: int = typer.Option(3, min=0, max=20, help="관계별 근거 수 상한"),
):
    """두 개념 사이의 최단 관계 경로를 조회."""
    from pkb.graph import query as graph_queries
    from pkb.graph.schema import graph_connection

    with graph_connection(settings.graph_db_path) as conn:
        result = graph_queries.shortest_path(
            conn,
            source,
            target,
            max_hops=max_hops,
            directed=directed,
            relations=_graph_relations(relation),
            evidence_limit=evidence_limit,
        )
    _echo_graph_json(result)


@graph_app.command("query")
def graph_query(
    query: str = typer.Argument(..., help="관계를 탐색할 자연어 질문 또는 키워드"),
    depth: int = typer.Option(2, min=0, max=4, help="시드에서 확장할 깊이"),
    seed_limit: int = typer.Option(3, min=1, max=10, help="시작 개념 수"),
    max_nodes: int = typer.Option(30, min=1, max=100, help="반환 노드 상한"),
    min_similarity: float = typer.Option(0.4, min=0.0, max=1.0, help="최소 의미 유사도"),
    relation: str = typer.Option("", "--relation", "-r", help="관계 타입 필터(쉼표 구분)"),
    evidence_limit: int = typer.Option(3, min=0, max=20, help="관계별 근거 수 상한"),
):
    """자연어 질문으로 개념 시드를 찾고 주변 관계 하위 그래프를 조회."""
    from pkb.embeddings import embed
    from pkb.graph import query as graph_queries
    from pkb.graph.schema import graph_connection

    if not query.strip():
        raise typer.BadParameter("query는 비어 있을 수 없습니다.")
    query_embedding = embed([query])[0]
    with graph_connection(settings.graph_db_path) as conn:
        result = graph_queries.query_subgraph(
            conn,
            query,
            query_embedding=query_embedding,
            depth=depth,
            seed_limit=seed_limit,
            max_nodes=max_nodes,
            min_similarity=min_similarity,
            relations=_graph_relations(relation),
            evidence_limit=evidence_limit,
        )
    _echo_graph_json(result)


@graph_app.command("affected")
def graph_affected(
    concept: str = typer.Argument(..., help="시작 개념 이름, slug 또는 alias"),
    max_depth: int = typer.Option(2, min=1, max=6, help="최대 탐색 깊이"),
    max_nodes: int = typer.Option(30, min=1, max=100, help="반환 노드 상한"),
    relation: str = typer.Option("", "--relation", "-r", help="관계 타입 필터(쉼표 구분)"),
    evidence_limit: int = typer.Option(3, min=0, max=20, help="관계별 근거 수 상한"),
):
    """저장된 src→dst 방향으로 이어지는 하위 개념을 조회."""
    from pkb.graph import query as graph_queries
    from pkb.graph.schema import graph_connection

    with graph_connection(settings.graph_db_path) as conn:
        result = graph_queries.affected(
            conn,
            concept,
            max_depth=max_depth,
            max_nodes=max_nodes,
            relations=_graph_relations(relation),
            evidence_limit=evidence_limit,
        )
    _echo_graph_json(result)


@graph_app.command("map")
def graph_map(
    concept: str = typer.Option("", "--concept", "-c", help="중심 개념 이름, slug 또는 alias"),
    query: str = typer.Option("", "--query", "-q", help="자연어 질문/키워드로 시드 탐색"),
    path: tuple[str, str] = typer.Option(
        (None, None), "--path", help="두 개념 사이 최단 경로 (예: --path BM25 RAG)"
    ),
    depth: int = typer.Option(1, min=0, max=2, help="확장 깊이 (기본 1-hop, 최대 2-hop)"),
    max_nodes: int = typer.Option(30, min=1, max=100, help="표시 노드 상한"),
    relation: str = typer.Option("", "--relation", "-r", help="관계 타입 필터(쉼표 구분)"),
    evidence_limit: int = typer.Option(5, min=0, max=20, help="관계별 근거 수 상한"),
    out: Path | None = typer.Option(None, help="출력 HTML 경로 (기본 data/.graph/evidence-map.html)"),
    open_browser: bool = typer.Option(False, "--open", help="생성 후 브라우저로 열기"),
):
    """개념·질문·경로 중심 Evidence Map HTML 스냅샷 생성 (오프라인 자급식)."""
    from pkb.graph import viewmap
    from pkb.graph.schema import graph_connection

    concept, query = concept.strip(), query.strip()
    path_pair = tuple(path) if all(path) else None
    if sum(map(bool, (concept, query, path_pair))) != 1:
        raise typer.BadParameter("--concept, --query, --path 중 정확히 하나를 지정하세요.")

    query_embedding = None
    if query:
        from pkb.embeddings import embed

        query_embedding = embed([query])[0]

    with graph_connection(settings.graph_db_path) as conn:
        try:
            model = viewmap.build(
                conn,
                concept=concept or None,
                query=query or None,
                query_embedding=query_embedding,
                path=path_pair,
                depth=depth,
                max_nodes=max_nodes,
                relations=_graph_relations(relation),
                evidence_limit=evidence_limit,
            )
        except ValueError as exc:
            typer.echo(f"오류: {exc}")
            raise typer.Exit(1) from exc

    out_path = out or Path(settings.graph_db_path).parent / "evidence-map.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(viewmap.render(model), encoding="utf-8")
    typer.echo(
        f"Evidence Map 생성: {out_path} "
        f"(nodes={len(model['nodes'])}, edges={len(model['edges'])})"
    )
    if model["message"]:
        typer.echo(f"안내: {model['message']}")
    if open_browser:
        import webbrowser

        webbrowser.open(out_path.resolve().as_uri())


@graph_app.command("reset-evidence")
def graph_reset_evidence(
    yes: bool = typer.Option(False, "--yes", "-y", help="evidence·추출 마커 초기화 승인"),
):
    """기존 그래프를 유지한 채 edge evidence staging 재구축을 시작."""
    from pkb.graph import store as gstore
    from pkb.graph.schema import graph_connection

    if not yes:
        typer.echo("staging evidence·추출 마커를 비워 전 청크를 재추출 대상으로 만듭니다.")
        typer.echo("기존 관계·멘션·개념·큐레이션은 전환 완료까지 유지됩니다.")
        typer.echo("실행하려면 --yes를 지정하세요.")
        raise typer.Exit(1)

    with graph_connection(settings.graph_db_path) as conn:
        result = gstore.prepare_edge_evidence_rebuild(conn)
    typer.echo(
        "그래프 evidence 재구축 준비 완료: "
        f"기존 edges={result['edges_preserved']} mentions={result['mentions_preserved']} 유지, "
        f"evidence={result['edge_evidence']} markers={result['markers']} 초기화"
    )
    typer.echo("이제 graph_list_chunks(..., pending_only=True) → graph_store_concepts를 반복하세요.")


@graph_app.command("finalize-evidence")
def graph_finalize_evidence(
    yes: bool = typer.Option(False, "--yes", "-y", help="staging 그래프로 원자 전환 승인"),
):
    """전 청크 추출 완료 후 staging evidence를 서비스 그래프로 원자 전환."""
    from pkb.graph import store as gstore
    from pkb.graph.schema import graph_connection
    from pkb.graph.services import scan_pending_chunks
    from pkb.store import get_client

    if not yes:
        typer.echo("전량 추출 완료를 검증한 뒤 기존 관계를 staging evidence 집계로 교체합니다.")
        typer.echo("실행하려면 --yes를 지정하세요.")
        raise typer.Exit(1)

    with graph_connection(settings.graph_db_path) as conn:
        if not gstore.edge_evidence_rebuild_active(conn):
            typer.echo("진행 중인 edge evidence 재구축이 없습니다.")
            raise typer.Exit(1)
        pending, total = scan_pending_chunks(get_client(), conn)
        if pending:
            typer.echo(f"전환 보류: 미추출 청크 {len(pending)} / {total}")
            raise typer.Exit(1)
        result = gstore.finalize_edge_evidence_rebuild(conn)

    typer.echo(
        "edge evidence 전환 완료: "
        f"edges {result['edges_before']}→{result['edges_after']}, "
        f"evidence={result['edge_evidence']}"
    )


@graph_app.command("rebuild-evidence-local")
def graph_rebuild_evidence_local(
    yes: bool = typer.Option(False, "--yes", "-y", help="로컬 LLM 장시간 실행 승인"),
    model: str = typer.Option("gpt-oss:20b", help="설치된 Ollama 모델"),
    batch_size: int = typer.Option(8, min=1, max=8, help="생성 1회당 청크 수"),
    max_batches: int = typer.Option(0, min=0, help="0=완료까지, 양수=해당 배치만 실행"),
    endpoint: str = typer.Option("http://127.0.0.1:11434", help="Ollama API 주소"),
):
    """Ollama structured output으로 pending evidence를 재시작 가능하게 전량 추출."""
    from pkb.graph.rebuild import rebuild_with_ollama

    if not yes:
        typer.echo("설치된 로컬 생성 모델을 장시간 실행해 pending 청크를 전량 추출합니다.")
        typer.echo("진행 상황은 data/.logs/graph-evidence-rebuild.jsonl에 기록됩니다.")
        typer.echo("실행하려면 --yes를 지정하세요.")
        raise typer.Exit(1)

    try:
        result = rebuild_with_ollama(
            model=model,
            batch_size=batch_size,
            max_batches=max_batches,
            endpoint=endpoint,
            progress=typer.echo,
        )
    except Exception as exc:
        typer.echo(f"재구축 중단: {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc

    if result["complete"]:
        typer.echo(
            f"재구축 완료(전환 대기): batches={result['batches']} "
            f"chunks={result['chunks']} 기존 edges={result['edges_before']} "
            f"evidence={result['edge_evidence']}"
        )
        typer.echo("품질을 검토한 뒤 `pkb graph finalize-evidence --yes`를 실행하세요.")
    else:
        typer.echo(
            f"지정 범위 완료: batches={result['batches']} chunks={result['chunks']} "
            "(재실행하면 pending부터 계속)"
        )


if __name__ == "__main__":
    app()
