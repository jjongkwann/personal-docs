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
            category_override="obsidian",
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
                category_override="obsidian",
            )
            typer.echo(f"   → {format_delta_stats(vault_stats)}")

    typer.echo("\n완료.")


@app.command()
def sync(
    yes: bool = typer.Option(False, "--yes", "-y", help="대량 삭제 확인 생략"),
):
    """데이터 코퍼스·Obsidian 볼트를 ES와 재조정 (업서트 + 원본에 없는 유령 문서 정리).

    설정이 곧 기대 상태: OBSIDIAN_PATH를 제거했다면 잔존 obsidian/* 문서를 정리한다.
    """
    from pkb.ingest import format_delta_stats, reconcile
    from pkb.store import (
        PRUNE_CONFIRM_THRESHOLD,
        delete_document,
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
        for doc_id in stale:
            delete_document(es, doc_id)
        typer.echo(f"   정리: 원본에 없는 문서 {len(stale)}개 삭제")

    # 1) data 코퍼스
    root = data_dir()
    if root.is_dir():
        stats, stale = reconcile(es, root, "data/")
        typer.echo(f"1. data 코퍼스 동기화: {stats['files']}개 파일 ({root})")
        typer.echo(f"   → {format_delta_stats(stats)}")
        prune(stale)
    else:
        # 루트 소실은 연동 해제가 아니라 설정 오류일 가능성이 높다 → 정리하지 않음
        typer.echo(f"1. data 코퍼스 루트 없음 — 건너뜀 (정리 안 함): {root}")

    # 2) Obsidian 볼트
    if settings.obsidian_path:
        vault = Path(settings.obsidian_path).expanduser().resolve()
        if vault.is_dir():
            stats, stale = reconcile(
                es, vault, "obsidian/", category_override="obsidian", exclude=root
            )
            typer.echo(f"2. Obsidian 동기화: {stats['files']}개 파일 ({vault})")
            typer.echo(f"   → {format_delta_stats(stats)}")
            prune(stale)
        else:
            typer.echo(f"2. OBSIDIAN_PATH 디렉터리 없음 — 건너뜀 (정리 안 함): {vault}")
    else:
        typer.echo("2. Obsidian 연동 꺼짐 (OBSIDIAN_PATH 미설정)")
        prune(sorted(list_doc_ids(es, "obsidian/")))

    from pkb.mcp_server import _graph_prune_summary

    graph_summary = _graph_prune_summary(es)
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
    from pkb.ingest import SUPPORTED_EXTENSIONS, read_file_as_text

    input_path = input_path.resolve()
    if not input_path.exists():
        typer.echo(f"파일을 찾을 수 없습니다: {input_path}")
        raise typer.Exit(1)
    if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        typer.echo(f"지원하지 않는 형식입니다: {input_path.suffix}")
        raise typer.Exit(1)

    text = read_file_as_text(input_path)

    # 출력 경로 결정
    data_root = data_dir()
    output = (
        data_root / category / f"{input_path.stem}.md"
        if output is None
        else output.resolve()
    )

    # data 코퍼스 하위인지 검증
    if not output.is_relative_to(data_root):
        typer.echo(f"출력 경로는 data 코퍼스({data_root}) 하위여야 합니다: {output}")
        raise typer.Exit(1)

    output.parent.mkdir(parents=True, exist_ok=True)
    # 원본 파일 정보를 주석 헤더로 추가
    header = f"<!-- source: {input_path.name} | converted: {input_path.suffix} → .md -->\n\n"
    output.write_text(header + text, encoding="utf-8")
    typer.echo(f"변환 완료: {output} ({len(text)}자)")

    if ingest:
        from pkb.ingest import format_delta_stats, ingest_files

        stats = ingest_files([output], base_dir=data_root, doc_id_prefix="data/")
        typer.echo(f"인제스트 완료: {format_delta_stats(stats)}")


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
):
    """파일을 작성하고 ES에 인제스트. mcp write_file과 동일 동작(data/ 하위 .md만)."""
    from pkb.mcp_server import _resolve_data_path

    if content is None:
        content = sys.stdin.read()

    full_path = _resolve_data_path(file_path)
    if full_path is None:
        typer.echo(f"오류: data/ 하위 경로에만 파일을 작성할 수 있습니다. (입력: {file_path})")
        raise typer.Exit(1)
    if full_path.suffix != ".md":
        typer.echo(f"오류: 마크다운(.md) 파일만 작성 가능합니다. (입력: {file_path})")
        raise typer.Exit(1)

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    typer.echo(f"파일 저장 완료: {file_path} ({len(content)}자)")

    if ingest:
        from pkb.ingest import format_delta_stats, ingest_files

        stats = ingest_files([full_path], base_dir=data_dir(), doc_id_prefix="data/")
        typer.echo(f"인제스트: {format_delta_stats(stats)}")


@app.command()
def show(
    doc_id: str = typer.Argument(..., help="조회할 문서 ID (예: data/study/rag/1.2.10_BM25.md)"),
    content: bool = typer.Option(
        False, "--content/--no-content", help="청크 본문 포함 여부 (기본: 목차만)"
    ),
    chunks: str = typer.Option("", "--chunks", help='특정 청크만 조회 (예: "3-7")'),
):
    """문서 조회 (기본: 메타+목차, section_path 포함). mcp get_document과 동일."""
    from pkb.mcp_server import _render_document
    from pkb.store import get_client

    es = get_client()
    result = es.search(
        index=settings.es_index,
        query={"term": {"doc_id": doc_id}},
        size=500,
        source_excludes=["embedding"],
        sort=[{"chunk_index": {"order": "asc"}}],
    )
    hits = result["hits"]["hits"]
    if not hits:
        typer.echo(f"문서를 찾을 수 없습니다: {doc_id}")
        raise typer.Exit(1)

    sources = [h["_source"] for h in hits]
    typer.echo(_render_document(doc_id, sources, content, chunks))


@app.command("reindex-doc")
def reindex_doc(
    doc_id: str = typer.Argument(..., help="재인제스트할 문서 ID"),
):
    """단일 문서를 원본 파일로부터 재인제스트. mcp reindex_document과 동일.

    doc_id가 "obsidian/..."으로 시작하면 OBSIDIAN_PATH 하위 파일로 처리.
    """
    from pkb.ingest import format_delta_stats, ingest_files
    from pkb.mcp_server import _resolve_data_path

    if doc_id.startswith("obsidian/"):
        if not settings.obsidian_path:
            typer.echo("오류: OBSIDIAN_PATH가 설정되지 않았습니다.")
            raise typer.Exit(1)
        vault = Path(settings.obsidian_path).expanduser().resolve()
        rel = doc_id[len("obsidian/"):]
        file_path = (vault / rel).resolve()
        base_dir = vault
        prefix = "obsidian/"
        cat = "obsidian"
    else:
        base_dir = data_dir()
        file_path = _resolve_data_path(doc_id)
        if file_path is None:
            typer.echo(f"오류: 알 수 없는 doc_id 형식입니다 (data/ 또는 obsidian/ 접두사 필요): {doc_id}")
            raise typer.Exit(1)
        prefix = "data/"
        cat = None

    if not file_path.exists():
        typer.echo(f"원본 파일을 찾을 수 없습니다: {file_path}")
        raise typer.Exit(1)

    stats = ingest_files(
        [file_path], base_dir=base_dir, doc_id_prefix=prefix, category_override=cat
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
        exclude_categories=["obsidian"] if not include_obsidian else None,
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
        typer.echo(f"    카테고리: {r['category']} | 점수: {r['score']:.4f}")
        typer.echo(f"{'─'*60}")
        # 내용 미리보기 (처음 300자)
        preview = r["content"][:300]
        if len(r["content"]) > 300:
            preview += "..."
        typer.echo(preview)


@app.command()
def delete(
    doc_id: str = typer.Argument(..., help="삭제할 문서 ID"),
):
    """문서 및 모든 청크 삭제 (하드 삭제, 비가역)."""
    from pathlib import Path

    from pkb.store import delete_document, get_client

    es = get_client()
    deleted = delete_document(es, doc_id)
    typer.echo(f"'{doc_id}' 삭제 완료 ({deleted}개 청크).")

    if Path(settings.graph_db_path).exists():
        from pkb.graph import store as gstore
        from pkb.graph.schema import get_connection, init_schema

        init_schema(settings.graph_db_path)
        with get_connection(settings.graph_db_path) as conn:
            result = gstore.purge_document(conn, doc_id)
        if result["mentions_pruned"] or result["documents_pruned"]:
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
    from pkb.store import archive_document, get_client

    es = get_client()
    n = archive_document(es, doc_id, reason=reason or None)
    if n == 0:
        typer.echo(f"아카이브 대상 없음 (doc_id={doc_id})")
        raise typer.Exit(1)
    msg = f"아카이브 완료: '{doc_id}' ({n}개 청크)"
    if reason:
        msg += f" | 사유: {reason}"
    typer.echo(msg)


@app.command()
def restore(
    doc_id: str = typer.Argument(..., help="복구할 문서 ID"),
):
    """아카이브된 문서를 복구해 검색에 다시 노출."""
    from pkb.store import get_client, restore_document

    es = get_client()
    n = restore_document(es, doc_id)
    if n == 0:
        typer.echo(f"복구할 아카이브 없음 (doc_id={doc_id})")
        raise typer.Exit(1)
    typer.echo(f"복구 완료: '{doc_id}' ({n}개 청크)")


@app.command()
def doctor():
    """PKB 시스템 상태 점검. ES 연결, 인덱스, 문서 수, 설정, 개념 그래프 통계 확인."""
    from pkb.report import build_health_report
    from pkb.store import get_client

    typer.echo(build_health_report(get_client()))


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


graph_app = typer.Typer(help="개념 그래프 빌드/조회 (SQLite 기반 Graph RAG 보조)")
app.add_typer(graph_app, name="graph")


@graph_app.command("stats")
def graph_stats():
    """그래프 통계 출력."""
    from pkb.graph import store as gstore
    from pkb.graph.schema import get_connection, init_schema

    init_schema(settings.graph_db_path)
    with get_connection(settings.graph_db_path) as conn:
        s = gstore.stats(conn)
    typer.echo(f"DB: {settings.graph_db_path}")
    for k, v in s.items():
        typer.echo(f"  {k}: {v}")


@graph_app.command("sync-notes")
def graph_sync_notes(
    yes: bool = typer.Option(False, "--yes", "-y", help="대량 정리 확인 생략"),
):
    """SQLite 개념그래프를 data/concepts/<slug>.md 볼트 노트로 동기화 (단방향, ES 미색인).
    mcp sync_concept_notes과 동일.
    """
    from pkb.graph.notes import sync_concept_notes
    from pkb.graph.schema import get_connection, init_schema

    init_schema(settings.graph_db_path)
    with get_connection(settings.graph_db_path) as conn:
        result = sync_concept_notes(conn, confirm_prune=yes)

    typer.echo(
        f"개념 노트 동기화: created={result['created']} updated={result['updated']} "
        f"skipped={result['skipped']} failed={result['failed']} pruned={result['pruned']}"
    )
    if result["pending_prune"]:
        typer.echo(f"정리 보류 {len(result['pending_prune'])}개 (대량이라 확인 필요):")
        for p in result["pending_prune"][:10]:
            typer.echo(f"  - {p}")
        if len(result["pending_prune"]) > 10:
            typer.echo(f"  ... 외 {len(result['pending_prune']) - 10}개")
        typer.echo("삭제하려면 --yes로 재호출하세요.")


if __name__ == "__main__":
    app()
