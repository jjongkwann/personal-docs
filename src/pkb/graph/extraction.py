"""Bounded incremental Ollama extraction with durable execution provenance.

A stored item passed structural and source-surface checks, not independent fact checking.
Existing source hashes determine pending work; changing a model does not silently replace
previously extracted chunks. Full evidence replacement remains an explicit rebuild operation.
"""

from __future__ import annotations

import json
import uuid

from pkb.config import settings
from pkb.graph import store as graph_store
from pkb.graph.extraction_history import (
    add_items,
    finish_run,
    start_run,
    update_items,
)
from pkb.graph.extraction_history import (
    json_text as _json,
)
from pkb.graph.extraction_history import (
    now as _now,
)
from pkb.graph.extraction_history import (
    text_hash as _hash,
)
from pkb.graph.ollama import generation_model
from pkb.graph.rebuild import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ExtractedBatch,
    _ollama_extract,
    _storage_payload,
    build_system_prompt,
)
from pkb.graph.schema import graph_connection
from pkb.graph.services import (
    corpus_vocabulary,
    graph_input_hash,
    legacy_concept_hints,
    load_pending_batch,
    store_concepts,
)
from pkb.store import get_client


def runs(*, run_id: str = "", limit: int = 20) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("limit는 1~100이어야 합니다.")
    with graph_connection(settings.graph_db_path, normalize=False) as conn:
        rows = conn.execute(
            "SELECT * FROM graph_extraction_runs "
            + ("WHERE run_id = ? " if run_id else "")
            + "ORDER BY created_at DESC LIMIT ?",
            (run_id, limit) if run_id else (limit,),
        ).fetchall()
        result = []
        for row in rows:
            record = dict(row)
            record.pop("system_prompt")  # Inspect metadata without dumping the corpus vocabulary.
            record["options"] = json.loads(record.pop("options_json"))
            record["items"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM graph_extraction_items WHERE run_id = ? ORDER BY doc_id, chunk_index",
                    (row["run_id"],),
                )
            ]
            if run_id:
                record["events"] = [
                    dict(item)
                    for item in conn.execute(
                        "SELECT * FROM graph_extraction_events WHERE run_id = ? ORDER BY id",
                        (run_id,),
                    )
                ]
            result.append(record)
        return result


def extract_pending(
    *,
    doc_id: str = "",
    max_chunks: int = 20,
    batch_size: int = 1,
    retries: int = 1,
    endpoint: str | None = None,
    model: str | None = None,
    context_tokens: int | None = None,
    output_tokens: int | None = None,
    timeout: int | None = None,
    resume: str = "",
    progress=print,
    _selected_chunks: list[dict] | None = None,
) -> dict:
    """Extract at most max_chunks pending items, or retry a saved run's unfinished selection.

    Resume uses the recorded endpoint/model/options and refuses changed model digests,
    extraction contracts or source inputs. It never resets evidence or extraction markers.
    """
    if not 1 <= max_chunks <= 1000 or not 1 <= batch_size <= 8 or not 0 <= retries <= 5:
        raise ValueError("max_chunks=1~1000, batch_size=1~8, retries=0~5이어야 합니다.")
    if _selected_chunks is not None and (resume or doc_id or len(_selected_chunks) > max_chunks):
        raise ValueError("내부 선택 청크는 resume/doc_id와 함께 쓰거나 max_chunks를 넘을 수 없습니다.")
    es = get_client()
    schema_hash = _hash(_json(ExtractedBatch.model_json_schema()))
    with graph_connection(settings.graph_db_path) as conn:
        if graph_store.edge_evidence_rebuild_active(conn):
            raise ValueError("전량 evidence rebuild 진행 중입니다. 먼저 해당 작업을 완료하세요.")
        saved = (
            conn.execute("SELECT * FROM graph_extraction_runs WHERE run_id = ?", (resume,)).fetchone()
            if resume
            else None
        )
        if resume and saved is None:
            raise ValueError(f"추출 실행을 찾을 수 없습니다: {resume}")
        if saved:
            if any(value is not None for value in (endpoint, model, context_tokens, output_tokens, timeout)) or doc_id:
                raise ValueError("resume은 저장된 모델·주소·입력 범위를 사용합니다. 변경하려면 새 실행을 만드세요.")
            options = json.loads(saved["options_json"])
            if options.get("mode") != "incremental":
                raise ValueError(
                    "정상 증분 추출 실행만 --resume으로 재개할 수 있습니다. "
                    "전량 rebuild는 rebuild-evidence-local 명령으로 pending부터 이어서 실행하세요."
                )
            endpoint, model = saved["endpoint"], saved["model"]
            prompt = saved["system_prompt"]
            if (saved["prompt_version"], saved["schema_version"], saved["schema_hash"]) != (
                PROMPT_VERSION,
                SCHEMA_VERSION,
                schema_hash,
            ):
                raise ValueError("추출 계약이 변경되어 이 실행을 재개할 수 없습니다. 새 실행을 만드세요.")
            if saved["prompt_hash"] != _hash(prompt):
                raise ValueError("저장된 추출 프롬프트 hash가 일치하지 않습니다.")
        else:
            endpoint = endpoint or settings.graph_ollama_endpoint
            model = model or settings.graph_ollama_model
            options = {
                "doc_id": doc_id,
                "max_chunks": max_chunks,
                "batch_size": batch_size,
                "context_tokens": settings.graph_ollama_context if context_tokens is None else context_tokens,
                "output_tokens": settings.graph_ollama_output if output_tokens is None else output_tokens,
                "timeout": settings.graph_ollama_timeout if timeout is None else timeout,
            }
            # Keep room for source chunks on a 4K-context inference server.
            prompt = build_system_prompt(corpus_vocabulary(conn, limit=20))
    if (
        not 1024 <= options["context_tokens"] <= 131072
        or not 128 <= options["output_tokens"] < options["context_tokens"]
    ):
        raise ValueError("context는 1024~131072, output은 128 이상 context 미만이어야 합니다.")
    if options["timeout"] <= 0:
        raise ValueError("timeout은 양수여야 합니다.")
    selected = generation_model(endpoint, model, timeout=min(options["timeout"], 30))
    if selected.get("context_limit") and options["context_tokens"] > selected["context_limit"]:
        raise ValueError(f"요청 context가 모델 한도 {selected['context_limit']}를 넘습니다.")
    if saved and selected["digest"] != saved["model_digest"]:
        raise ValueError("설치 모델 digest가 달라졌습니다. 기존 실행을 재개하지 말고 새 실행을 만드세요.")
    run_id = resume or uuid.uuid4().hex
    with graph_connection(settings.graph_db_path) as conn:
        if not saved:
            if _selected_chunks is None:
                chunks, pending, _ = load_pending_batch(
                    es,
                    conn,
                    limit=max_chunks,
                    query={"term": {"doc_id": doc_id}} if doc_id else None,
                )
            else:
                # The worker already selected due inputs before applying its
                # bound. Their exact versions are checked again before inference.
                chunks = [{**chunk, "input_hash": graph_input_hash(chunk)} for chunk in _selected_chunks]
                pending = len(chunks)
            if any(not chunk.get("content_hash") or not chunk.get("input_hash") for chunk in chunks):
                raise ValueError("추출 입력 hash가 없습니다. 먼저 해당 문서를 재색인하세요.")
            start_run(
                conn,
                run_id=run_id,
                endpoint=endpoint,
                model=model,
                digest=selected["digest"],
                options={**options, "mode": "incremental"},
                prompt_version=PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
                prompt=prompt,
                schema=ExtractedBatch.model_json_schema(),
                pending=pending,
            )
            add_items(conn, run_id, chunks)
        else:
            conn.execute(
                "UPDATE graph_extraction_runs SET status = 'running', updated_at = ?, error = NULL WHERE run_id = ?",
                (_now(), run_id),
            )
        items = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM graph_extraction_items WHERE run_id = ? AND status != 'stored' "
                "ORDER BY doc_id, chunk_index",
                (run_id,),
            )
        ]
    progress(f"run_id={run_id} selected={len(items)} model={model} digest={selected['digest']}")
    try:
        for start in range(0, len(items), options["batch_size"]):
            rows = items[start : start + options["batch_size"]]
            docs = es.mget(
                index=settings.es_index,
                ids=[f"{r['doc_id']}_{r['chunk_index']}" for r in rows],
                source_excludes=["embedding"],
            )["docs"]
            chunks = []
            for row, doc in zip(rows, docs, strict=True):
                source = doc.get("_source") or {}
                key = (row["doc_id"], row["chunk_index"])
                if (
                    not doc.get("found")
                    or source.get("source_type") in {"image", "audio"}
                    or source.get("import_extraction") in {"unsupported", "empty"}
                    or (source.get("content_hash"), graph_input_hash(source))
                    != (row["content_hash"], row["input_hash"])
                ):
                    update_items(
                        run_id,
                        [key],
                        "stale",
                        error="원문이 변경·삭제되었거나 추출 대상에서 제외되었습니다. 새 실행에서 확인하세요.",
                    )
                    continue
                # Exactly the source fields versioned by graph_input_hash; unrelated
                # ES/frontmatter fields must not become unversioned model inputs.
                chunk = {field: source.get(field) for field in (
                    "doc_id", "chunk_index", "content_hash", "content", "title", "section_path", "category",
                )}
                chunk["content"] = source.get("content", "")
                chunk["section_path"] = source.get("section_path", "")
                chunks.append({**chunk, "input_hash": row["input_hash"]})
            if not chunks:
                continue
            keys = [(c["doc_id"], c["chunk_index"]) for c in chunks]
            with graph_connection(settings.graph_db_path) as conn:
                hints = legacy_concept_hints(conn, keys)
            for attempt in range(retries + 1):
                update_items(run_id, keys, "running", attempt=True)
                try:
                    batch, raw = _ollama_extract(
                        chunks,
                        hints,
                        model=model,
                        endpoint=endpoint,
                        timeout=options["timeout"],
                        context_tokens=options["context_tokens"],
                        output_tokens=options["output_tokens"],
                        system_prompt=prompt,
                    )
                    if len(chunks) > 1 and not any(item.concepts or item.relations for item in batch.items):
                        raise ValueError("다중 청크 배치가 전부 빈 추출을 반환했습니다. 배치를 줄여 검토하세요.")
                    payload = _storage_payload(batch, chunks)
                    message = store_concepts(payload)
                    if message.startswith("오류:") or "저장 거부" in message:
                        raise ValueError(message)
                    # Verify the exact markers, rather than mistaking a partial textual result for success.
                    with graph_connection(settings.graph_db_path) as conn:
                        markers = graph_store.extracted_input_markers(conn)
                    if any(markers.get(key) != c["input_hash"] for key, c in zip(keys, chunks, strict=True)):
                        raise ValueError(f"저장 마커 검증 실패: {message}")
                    filtered = json.loads(payload)["items"]
                    result = {
                        "checks": "schema, ontology and source-surface checks; not independent factual verification",
                        "concepts_dropped": sum(len(i.concepts) for i in batch.items)
                        - sum(len(i["concepts"]) for i in filtered),
                        "relations_dropped": sum(len(i.relations) for i in batch.items)
                        - sum(len(i["relations"]) for i in filtered),
                        "store": message,
                        "prompt_eval_count": raw.get("prompt_eval_count"),
                        "eval_count": raw.get("eval_count"),
                        "total_duration": raw.get("total_duration"),
                        "input_budget": raw.get("input_budget"),
                    }
                    update_items(run_id, keys, "stored", result=result)
                    break
                except Exception as exc:
                    update_items(run_id, keys, "failed", error=f"{type(exc).__name__}: {exc}")
                    if attempt == retries:
                        progress(f"run_id={run_id} failed={keys}: {exc}")
        with graph_connection(settings.graph_db_path) as conn:
            counts = dict(
                conn.execute(
                    "SELECT status, COUNT(*) FROM graph_extraction_items WHERE run_id = ? GROUP BY status", (run_id,)
                )
            )
            status = "completed" if all(key == "stored" for key in counts) else "partial"
            conn.execute(
                "UPDATE graph_extraction_runs SET status = ?, updated_at = ? WHERE run_id = ?", (status, _now(), run_id)
            )
        return {
            "run_id": run_id,
            "status": status,
            "items": counts,
            "selection_complete": status == "completed",
            "scope": "selected pending chunks only",
        }
    except BaseException as exc:
        finish_run(
            run_id, "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", f"{type(exc).__name__}: {exc}"
        )
        raise
