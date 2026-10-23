"""Independent graph extraction worker; indexed source versions are the durable queue.

Imports never wait for inference. Failed input versions have persistent backoff,
while extraction runs/items/events retain the generation and storage provenance.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from pkb.config import settings
from pkb.embedding_runtime import EmbeddingBusyError, embedding_operation
from pkb.graph import store as graph_store
from pkb.graph.discovery import DiscoveryCache
from pkb.graph.extraction import extract_pending, runs
from pkb.graph.extraction_history import now
from pkb.graph.schema import graph_connection
from pkb.graph.services import graph_input_hash
from pkb.store import get_client

MAX_CHUNKS = 5
RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 3600
STATUS_KEY = "graph_worker_status"


class WorkerAlreadyRunningError(RuntimeError):
    """Another automatic worker owns this graph database."""


@contextmanager
def worker_lock():
    """Hold one OS lock for this database, automatically released after a crash."""
    from pkb.embedding_runtime import runtime_directory

    # The active graph filename changes on model selection; worker identity must
    # remain stable so a service restart cannot create a second writer.
    path = runtime_directory() / "graph"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path) + ".worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkerAlreadyRunningError("이 그래프 DB의 자동 추출 worker가 이미 실행 중입니다.") from exc
        yield
    finally:
        os.close(descriptor)


def _heartbeat(status: str, **details) -> dict:
    record = {"status": status, "updated_at": now(), "pid": os.getpid(), "error": None,
              "current_doc_id": None, **details}
    try:
        with embedding_operation(), graph_connection(settings.graph_db_path, normalize=False) as conn:
            conn.execute(
                "INSERT INTO graph_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (STATUS_KEY, json.dumps(record, ensure_ascii=False)),
            )
    except EmbeddingBusyError:
        record.update(status="paused", reason="embedding_migration")
    return record


def worker_status() -> dict:
    """Read the last heartbeat and retry schedule without modifying the graph.

    ``heartbeat`` is null before the first run. ``retries`` describes current
    failed input versions, not completed extraction history. next_attempt_at is
    a Unix timestamp; heartbeat.updated_at is ISO 8601 UTC. A heartbeat alone
    does not prove the process is still alive.
    """
    result = {
        "enabled": settings.graph_auto_extract,
        "heartbeat": None,
        "retries": {"items": 0, "ready": 0, "cooling_down": 0, "next_attempt_at": None},
    }
    path = Path(settings.graph_db_path).expanduser().resolve()
    if not path.is_file():
        return result
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "graph_meta" in tables:
            row = conn.execute("SELECT value FROM graph_meta WHERE key = ?", (STATUS_KEY,)).fetchone()
            if row:
                result["heartbeat"] = json.loads(row[0])
        if "graph_extraction_retries" in tables:
            stamp = time.time()
            count, ready, next_at = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(next_attempt_at <= ?), 0), MIN(next_attempt_at) "
                "FROM graph_extraction_retries", (stamp,),
            ).fetchone()
            result["retries"] = {"items": count, "ready": ready, "cooling_down": count - ready,
                                 "next_attempt_at": next_at}
    finally:
        conn.close()
    return result


def _failed(chunk: dict, error: str, stamp: float) -> None:
    key = (chunk["doc_id"], chunk["chunk_index"], chunk.get("input_hash") or graph_input_hash(chunk))
    with graph_connection(settings.graph_db_path, normalize=False) as conn:
        row = conn.execute(
            "SELECT attempts FROM graph_extraction_retries WHERE doc_id = ? AND chunk_index = ? AND input_hash = ?",
            key,
        ).fetchone()
        attempts = (row[0] if row else 0) + 1
        delay = min(MAX_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(attempts - 1, 7))
        conn.execute(
            "INSERT INTO graph_extraction_retries VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(doc_id, chunk_index, input_hash) DO UPDATE SET "
            "attempts = excluded.attempts, next_attempt_at = excluded.next_attempt_at, error = excluded.error",
            (*key, attempts, stamp + delay, error),
        )


def worker_tick(*, stop: threading.Event | None = None, progress=print,
                discovery: DiscoveryCache | None = None) -> dict:
    """Migration and extraction never read/write different embedding generations."""
    try:
        with embedding_operation():
            return _worker_tick(stop=stop, progress=progress, discovery=discovery or DiscoveryCache())
    except EmbeddingBusyError:
        return {"status": "paused", "reason": "embedding_migration", "error": None}


def _worker_tick(*, stop: threading.Event | None = None, progress=print, discovery: DiscoveryCache) -> dict:
    """Process at most five due source versions; caller holds worker_lock."""
    with graph_connection(settings.graph_db_path, normalize=False) as conn:
        if graph_store.edge_evidence_rebuild_active(conn):
            return _heartbeat("paused", reason="rebuild")
        es = get_client()
        pending, total, discovery_complete = discovery.pending(es, conn)
        retry_rows = conn.execute("SELECT * FROM graph_extraction_retries").fetchall()
        current = {(c["doc_id"], c["chunk_index"], c["input_hash"]) for c in pending}
        # These rows only schedule retries. Never remove extraction history,
        # markers, concepts or evidence when retiring an obsolete input version.
        if discovery_complete:
            conn.executemany(
                "DELETE FROM graph_extraction_retries WHERE doc_id = ? AND chunk_index = ? AND input_hash = ?",
                [(row["doc_id"], row["chunk_index"], row["input_hash"]) for row in retry_rows
                 if (row["doc_id"], row["chunk_index"], row["input_hash"]) not in current],
            )
    scheduled = {(row["doc_id"], row["chunk_index"], row["input_hash"]): row["next_attempt_at"]
                 for row in retry_rows}
    stamp = time.time()

    def priority(chunk):
        next_at = scheduled.get((chunk["doc_id"], chunk["chunk_index"], chunk["input_hash"]))
        return (next_at is not None, next_at or 0, chunk["doc_id"], chunk["chunk_index"])

    eligible = [c for c in pending
                if scheduled.get((c["doc_id"], c["chunk_index"], c["input_hash"]), 0) <= stamp]
    # Slow requests can make old failures due again before the next tick. New
    # inputs get a first attempt before retries consume the bounded batch.
    selected = sorted(eligible, key=priority)[:MAX_CHUNKS]
    summary = {"pending": len(pending), "total": total, "selected": len(selected), "stored": 0,
               "failed": 0, "stale": 0, "run_ids": [], "discovery_complete": discovery_complete}
    last_error = None
    if not selected:
        return _heartbeat(
            "idle" if not pending and discovery_complete else "waiting",
            reason="source_changed" if not discovery_complete else ("retry_backoff" if pending else None), **summary,
        )
    for chunk in selected:
        if stop is not None and stop.is_set():
            break
        # Recheck between requests so an explicit rebuild pauses this worker.
        with graph_connection(settings.graph_db_path, normalize=False) as conn:
            rebuilding = graph_store.edge_evidence_rebuild_active(conn)
        if rebuilding:
            return _heartbeat("paused", reason="rebuild", **summary)
        _heartbeat("running", current_doc_id=chunk["doc_id"], **summary)
        try:
            doc = es.mget(index=settings.es_index,
                          ids=[f"{chunk['doc_id']}_{chunk['chunk_index']}"],
                          source_excludes=["embedding"])["docs"][0]
            if doc.get("error"):
                raise RuntimeError(f"Graph source lookup failed: {doc['error']}")
            source = doc.get("_source") or {}
            if (not doc.get("found") or source.get("source_type") in {"image", "audio"}
                    or source.get("import_extraction") in {"unsupported", "empty"}
                    or source.get("content_hash") != chunk["content_hash"]
                    or graph_input_hash(source) != chunk["input_hash"]):
                summary["stale"] += 1
                discovery.invalidate()
                discovery_complete = summary["discovery_complete"] = False
                continue
            chunk = source
            result = extract_pending(max_chunks=1, batch_size=1, retries=0,
                                     _selected_chunks=[chunk], progress=progress)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            _failed(chunk, error, time.time())
            summary["failed"] += 1
            last_error = error
            progress(f"graph worker scheduled retry for {chunk['doc_id']}[{chunk['chunk_index']}]: {error}")
            # A bad source can fail before execution history is created. Its
            # cooldown must not become a global delay that makes this same
            # sorted input due again before fresh inputs ever get a turn.
            continue
        summary["run_ids"].append(result["run_id"])
        item = runs(run_id=result["run_id"], limit=1)[0]["items"][0]
        status = item["status"]
        if status == "stored":
            summary["stored"] += 1
            with graph_connection(settings.graph_db_path, normalize=False) as conn:
                conn.execute(
                    "DELETE FROM graph_extraction_retries WHERE doc_id = ? AND chunk_index = ? AND input_hash = ?",
                    (chunk["doc_id"], chunk["chunk_index"], graph_input_hash(chunk)),
                )
        elif status == "stale":
            summary["stale"] += 1
            discovery.invalidate()
            discovery_complete = summary["discovery_complete"] = False
        else:
            summary["failed"] += 1
            last_error = item["error"] or "추출 결과를 저장하지 못했습니다."
            _failed(chunk, last_error, time.time())
    return _heartbeat("idle" if discovery_complete else "waiting", error=last_error,
                      reason=None if discovery_complete else "source_changed",
                      last_batch="partial" if summary["failed"] else "completed", **summary)


def _wait(stop: threading.Event, delay: float, heartbeat: dict) -> None:
    """Remain interruptible and keep liveness visible during long backoff."""
    deadline = time.monotonic() + delay
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0 or stop.wait(min(20, remaining)):
            return
        _heartbeat(heartbeat["status"], **{
            key: value for key, value in heartbeat.items() if key not in {"status", "updated_at", "pid"}
        })


def run_worker(*, interval: int = 20, once: bool = False) -> dict:
    """Run a singleton worker until SIGTERM/Ctrl+C; finish the current request first.

    GRAPH_AUTO_EXTRACT must be true. once runs one bounded tick. Global service
    failures back off up to one hour, while per-input failures never block new
    eligible versions. SIGTERM cannot interrupt an already active HTTP request;
    its configured Ollama timeout remains the upper bound.
    """
    if interval <= 0:
        raise ValueError("worker interval은 양수여야 합니다.")
    if not settings.graph_auto_extract:
        return {"status": "disabled", "enabled": False}
    stop = threading.Event()
    handlers = {}
    last = {"status": "running"}
    failures = 0
    discovery = DiscoveryCache()

    def request_stop(signum, frame):
        stop.set()

    def progress(message):
        print(message, flush=True)

    with worker_lock():
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                handlers[signum] = signal.signal(signum, request_stop)
        try:
            _heartbeat("running", phase="starting")
            while not stop.is_set():
                delay = interval
                try:
                    last = worker_tick(stop=stop, progress=progress, discovery=discovery)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    delay = max(interval, min(MAX_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(failures - 1, 7)))
                    error = f"{type(exc).__name__}: {exc}"
                    last = _heartbeat("waiting", error=error, next_poll_at=time.time() + delay)
                    progress(f"graph worker error; retry in {delay}s: {error}")
                if once:
                    break
                _wait(stop, delay, last)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            _heartbeat("stopped", last_status=last["status"], error=last.get("error"))
    return last
