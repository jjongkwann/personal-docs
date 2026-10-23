"""Current document extraction state, derived from indexed inputs and durable markers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pkb.config import settings
from pkb.graph.services import graph_input_hash, scan_pending_chunks
from pkb.store import get_client


def status_record(doc_id: str, status: str, total: int, pending: int, *, failed=0, updated_at=None) -> dict:
    messages = {
        "queued": "그래프 추출을 기다리고 있습니다.",
        "running": "Windows Ollama로 개념과 관계를 추출하고 있습니다.",
        "completed": "현재 문서의 그래프 반영이 완료됐습니다.",
        "retrying": "일부 내용을 처리하지 못해 자동 재시도를 기다리고 있습니다.",
        "waiting": "자동 처리 서버의 연결 또는 재시작을 기다리고 있습니다.",
        "disabled": "자동 그래프 처리가 꺼져 있습니다.",
        "not_applicable": "추출된 본문이 없어 그래프 처리 대상이 아닙니다.",
    }
    return {
        "doc_id": doc_id, "status": status, "total_chunks": total,
        "completed_chunks": total - pending, "pending_chunks": pending, "failed_chunks": failed,
        "updated_at": updated_at, "message": messages[status], "model": settings.graph_ollama_model,
    }


def document_status(doc_id: str) -> dict:
    """Read one authorized document. The MCP caller must enforce its read permission first."""
    if not doc_id.strip():
        raise ValueError("doc_id가 필요합니다.")
    es = get_client()
    first = es.search(
        index=settings.es_index, query={"term": {"doc_id": doc_id}}, size=1,
        source_includes=["import_extraction", "source_type"],
    )["hits"]["hits"]
    if not first:
        raise ValueError("색인된 문서를 찾을 수 없습니다.")
    source = first[0]["_source"]
    if source.get("import_extraction") in {"unsupported", "empty"} or source.get("source_type") in {"image", "audio"}:
        return status_record(doc_id, "not_applicable", 0, 0)
    uri = Path(settings.graph_db_path).expanduser().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        pending, total = scan_pending_chunks(es, conn, query={"term": {"doc_id": doc_id}})
        versions = {(s["chunk_index"], graph_input_hash(s)) for s in pending}
        rows = conn.execute(
            "SELECT i.*, r.status AS run_status FROM graph_extraction_items i "
            "JOIN graph_extraction_runs r USING (run_id) WHERE i.doc_id = ? ORDER BY i.updated_at DESC",
            (doc_id,),
        ).fetchall()
        latest = {}
        for row in rows:
            key = row["chunk_index"], row["input_hash"]
            if key in versions:
                latest.setdefault(key, row)
        stamp = max((r["updated_at"] for r in rows), default=None)
        scheduled_failures = {
            (r["chunk_index"], r["input_hash"])
            for r in conn.execute("SELECT chunk_index, input_hash FROM graph_extraction_retries WHERE doc_id = ?",
                                  (doc_id,))
        }
        heartbeat_row = conn.execute("SELECT value FROM graph_meta WHERE key = 'graph_worker_status'").fetchone()
    failed = len(({key for key, r in latest.items() if r["status"] == "failed"} | scheduled_failures) & versions)
    if not pending:
        state = "completed" if total else "not_applicable"
    elif not settings.graph_auto_extract:
        state = "disabled"
    else:
        heartbeat = json.loads(heartbeat_row[0]) if heartbeat_row else {}
        now = datetime.now(UTC)
        try:
            age = (now - datetime.fromisoformat(heartbeat["updated_at"])).total_seconds()
            live = 0 <= age <= max(90, settings.graph_ollama_timeout + 60)
        except (KeyError, ValueError, TypeError):
            live = False
        if live and heartbeat.get("reason") == "retry_backoff":
            state = "retrying" if failed else "queued"
        elif not live or heartbeat.get("status") in {"waiting", "paused", "stopped", "disabled"}:
            state = "waiting"
        elif heartbeat.get("status") == "running" and heartbeat.get("current_doc_id") == doc_id:
            state = "running"
        else:
            state = "retrying" if failed else "queued"
    return status_record(doc_id, state, total, len(pending), failed=failed, updated_at=stamp)
