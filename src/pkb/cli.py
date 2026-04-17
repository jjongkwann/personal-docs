from pathlib import Path

import typer

from pkb.config import settings

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

        files = find_ingestable_files(vault)
        typer.echo(f"Obsidian 초기 인제스트: {len(files)}개 파일 감지 ({vault})")
        total = ingest_files(
            files,
            base_dir=vault,
            doc_id_prefix="obsidian/",
            category_override="obsidian",
        )
        typer.echo(f"Obsidian 인제스트 완료: {total}개 청크")


@app.command()
def convert(
    input_path: Path = typer.Argument(..., help="변환할 파일 경로 (PDF, DOCX, PPTX, XLSX, HTML)"),
    category: str = typer.Option("auto", help="저장할 카테고리 (auto/about/career/study/writing/misc). auto는 Claude가 자동 분류"),
    output: Path = typer.Option(None, help="저장 경로 (기본: data/<category>/<파일명>.md)"),
    ingest: bool = typer.Option(True, help="변환 후 자동 인제스트"),
):
    """PDF/DOCX/PPTX/XLSX/HTML을 마크다운으로 변환하여 data/에 저장."""
    from pkb.ingest import SUPPORTED_EXTENSIONS, classify_category, read_file_as_text

    input_path = input_path.resolve()
    if not input_path.exists():
        typer.echo(f"파일을 찾을 수 없습니다: {input_path}")
        raise typer.Exit(1)
    if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        typer.echo(f"지원하지 않는 형식입니다: {input_path.suffix}")
        raise typer.Exit(1)

    # 텍스트 추출 먼저 (자동 분류에도 필요)
    text = read_file_as_text(input_path)

    # 카테고리 자동 분류
    if category == "auto":
        typer.echo("카테고리 자동 분류 중...")
        category = classify_category(text)
        typer.echo(f"→ {category}")

    # 출력 경로 결정
    if output is None:
        output = Path.cwd() / "data" / category / f"{input_path.stem}.md"
    else:
        output = output.resolve()

    # data/ 하위인지 검증
    data_root = (Path.cwd() / "data").resolve()
    if not output.is_relative_to(data_root):
        typer.echo(f"출력 경로는 data/ 하위여야 합니다: {output}")
        raise typer.Exit(1)

    output.parent.mkdir(parents=True, exist_ok=True)
    # 원본 파일 정보를 주석 헤더로 추가
    header = f"<!-- source: {input_path.name} | converted: {input_path.suffix} → .md -->\n\n"
    output.write_text(header + text, encoding="utf-8")
    typer.echo(f"변환 완료: {output} ({len(text)}자)")

    if ingest:
        from pkb.embeddings import embed
        from pkb.ingest import process_file
        from pkb.store import add_chunks, delete_document, get_client

        base_dir = Path.cwd()
        chunks = process_file(output, base_dir)
        if not chunks:
            typer.echo("인제스트할 내용이 없습니다.")
            return

        es = get_client()
        delete_document(es, chunks[0]["doc_id"])
        texts = [c["content"] for c in chunks]
        vectors = embed(texts)
        for chunk, vector in zip(chunks, vectors):
            chunk["embedding"] = vector
        count = add_chunks(es, chunks)
        typer.echo(f"인제스트 완료: {count}개 청크")


@app.command()
def add(
    path: Path = typer.Argument(..., help="파일 또는 디렉터리 경로 (md, txt, pdf, docx, pptx, xlsx, html 지원)"),
    tags: str = typer.Option("", help="쉼표 구분 태그 (예: python,backend)"),
):
    """문서를 인제스트하여 ES에 저장."""
    from pkb.embeddings import embed
    from pkb.ingest import find_ingestable_files, process_file
    from pkb.store import add_chunks, delete_document, get_client

    base_dir = Path.cwd()
    path = path.resolve()
    files = find_ingestable_files(path)
    if not files:
        typer.echo(f"인제스트 가능한 파일을 찾을 수 없습니다: {path}")
        raise typer.Exit(1)

    es = get_client()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    total_chunks = 0
    for file_path in files:
        chunks = process_file(file_path, base_dir)
        if not chunks:
            continue

        if tag_list:
            for chunk in chunks:
                chunk["tags"] = tag_list

        # 기존 문서 삭제 후 재인덱싱
        doc_id = chunks[0]["doc_id"]
        delete_document(es, doc_id)

        # 임베딩 생성
        texts = [c["content"] for c in chunks]
        vectors = embed(texts)
        for chunk, vector in zip(chunks, vectors):
            chunk["embedding"] = vector

        count = add_chunks(es, chunks)
        total_chunks += count
        typer.echo(f"  {file_path.name}: {count}개 청크")

    typer.echo(f"\n총 {len(files)}개 파일, {total_chunks}개 청크 인제스트 완료.")


@app.command()
def list(
    category: str = typer.Option(None, help="카테고리 필터 (about, career, study, writing)"),
):
    """저장된 문서 목록 표시."""
    from pkb.store import get_client, list_documents

    es = get_client()
    docs = list_documents(es, category)
    if not docs:
        typer.echo("저장된 문서가 없습니다.")
        return

    typer.echo(f"{'문서 ID':<40} {'카테고리':<10} {'청크':<6} {'수정일'}")
    typer.echo("-" * 75)
    for doc in docs:
        typer.echo(
            f"{doc['doc_id']:<40} {doc['category']:<10} {doc['chunks']:<6} {doc.get('date_modified', '-')}"
        )
    typer.echo(f"\n총 {len(docs)}개 문서")


@app.command()
def query(
    question: str = typer.Argument(..., help="검색 질문"),
    category: str = typer.Option(None, help="카테고리 필터"),
    top_k: int = typer.Option(settings.default_top_k, help="결과 수"),
    rerank: bool = typer.Option(None, help="CrossEncoder 재순위 사용 (기본: 설정값)"),
    fusion: str = typer.Option(None, help="하이브리드 결합 방식: rrf 또는 native (기본: 설정값)"),
):
    """하이브리드 검색 (BM25 + kNN + RRF + 옵션 리랭커)."""
    from pkb.retrieve import hybrid_search
    from pkb.store import get_client

    es = get_client()
    results = hybrid_search(
        es, question,
        category=category, top_k=top_k,
        candidate_k=settings.candidate_k,
        fusion=fusion if fusion is not None else settings.fusion,
        rerank=rerank if rerank is not None else settings.rerank_enabled,
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
def chat():
    """대화형 RAG 모드. 개인 데이터 기반 대화."""
    from pkb.agent import chat as agent_chat
    from pkb.agent import create_agent

    typer.echo("PKB 대화 모드 (종료: quit/exit/q)")
    typer.echo("저장된 개인 데이터를 기반으로 질문에 답하고, 파일을 작성합니다.\n")

    agent = create_agent()
    history: list[dict] = []

    while True:
        try:
            user_input = input("질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("\n종료합니다.")
            break

        if not user_input:
            continue
        if user_input in ("quit", "exit", "q"):
            typer.echo("종료합니다.")
            break

        try:
            response, history = agent_chat(agent, user_input, history)
            typer.echo(f"\n{response}\n")
        except Exception as e:
            typer.echo(f"\n오류: {e}\n")


@app.command()
def delete(
    doc_id: str = typer.Argument(..., help="삭제할 문서 ID"),
):
    """문서 및 모든 청크 삭제."""
    from pkb.store import delete_document, get_client

    es = get_client()
    deleted = delete_document(es, doc_id)
    typer.echo(f"'{doc_id}' 삭제 완료 ({deleted}개 청크).")


@app.command()
def watch():
    """data/와 OBSIDIAN_PATH를 감시하여 변경 시 자동 재인제스트."""
    import time

    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    from pkb.ingest import SUPPORTED_EXTENSIONS, ingest_files
    from pkb.store import delete_document, get_client

    es = get_client()

    class IngestHandler(FileSystemEventHandler):
        def __init__(self, base_dir: Path, doc_id_prefix: str = "", category_override: str | None = None):
            self.base_dir = base_dir
            self.doc_id_prefix = doc_id_prefix
            self.category_override = category_override

        def _is_supported(self, path: str) -> bool:
            return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

        def _rel_doc_id(self, path: str) -> str:
            try:
                rel = str(Path(path).resolve().relative_to(self.base_dir))
            except ValueError:
                return ""
            return f"{self.doc_id_prefix}{rel}" if self.doc_id_prefix else rel

        def on_created(self, event):
            if event.is_directory or not self._is_supported(event.src_path):
                return
            self._reingest(event.src_path)

        def on_modified(self, event):
            if event.is_directory or not self._is_supported(event.src_path):
                return
            self._reingest(event.src_path)

        def on_deleted(self, event):
            if event.is_directory or not self._is_supported(event.src_path):
                return
            doc_id = self._rel_doc_id(event.src_path)
            if doc_id:
                deleted = delete_document(es, doc_id)
                typer.echo(f"[삭제] {doc_id} ({deleted}개 청크)")

        def on_moved(self, event):
            if event.is_directory:
                return
            if self._is_supported(event.src_path):
                old_id = self._rel_doc_id(event.src_path)
                if old_id:
                    delete_document(es, old_id)
                    typer.echo(f"[이동-삭제] {old_id}")
            if self._is_supported(event.dest_path):
                self._reingest(event.dest_path)

        def _reingest(self, path: str):
            file_path = Path(path).resolve()
            if not file_path.exists():
                return
            try:
                total = ingest_files(
                    [file_path],
                    base_dir=self.base_dir,
                    doc_id_prefix=self.doc_id_prefix,
                    category_override=self.category_override,
                )
                if total:
                    doc_id = self._rel_doc_id(path)
                    typer.echo(f"[인제스트] {doc_id} ({total}개 청크)")
            except Exception as e:
                typer.echo(f"[오류] {path}: {e}")

    observer = Observer()

    # data/ 감시
    data_dir = (Path.cwd() / "data").resolve()
    if data_dir.is_dir():
        observer.schedule(
            IngestHandler(base_dir=Path.cwd().resolve()),
            str(data_dir), recursive=True,
        )
        typer.echo(f"감시 시작: {data_dir}")

    # Obsidian 감시
    if settings.obsidian_path:
        vault = Path(settings.obsidian_path).expanduser().resolve()
        if vault.is_dir():
            observer.schedule(
                IngestHandler(
                    base_dir=vault,
                    doc_id_prefix="obsidian/",
                    category_override="obsidian",
                ),
                str(vault), recursive=True,
            )
            typer.echo(f"감시 시작: {vault} (category=obsidian)")

    observer.start()
    typer.echo("감시 중... (Ctrl+C로 종료)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        typer.echo("\n종료합니다.")
        observer.stop()
    observer.join()


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="서버 호스트 (로컬 전용; LAN 노출하려면 0.0.0.0)"),
    port: int = typer.Option(8000, help="서버 포트"),
):
    """웹 서버 실행."""
    import uvicorn

    from pkb.web import app as web_app  # noqa: F811

    typer.echo(f"PKB 웹 서버 시작: http://{host}:{port}")
    uvicorn.run(web_app, host=host, port=port)


if __name__ == "__main__":
    app()
