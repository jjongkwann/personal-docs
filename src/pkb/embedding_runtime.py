"""Durable embedding selection and an atomic index/graph configuration switch.

Normal operations hold a shared OS lock for their entire read/write transaction.
The detached migration worker inherits an exclusive lock, builds new stores, then
publishes one manifest. Failed or interrupted jobs never change the active stores.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

_directory: Path | None = None
_baseline: dict = {}
_mutex = threading.RLock()
_local = threading.local()
_FIELDS = (
    "embedding_provider", "embedding_ollama_endpoint", "embedding_ollama_model", "embedding_ollama_digest",
    "embedding_ollama_query_prefix", "embedding_ollama_document_prefix",
    "embedding_model", "embedding_dims", "embedding_revision", "embedding_task", "embedding_batch_size",
    "embedding_query_prompt", "embedding_document_prompt", "embedding_normalize",
    "embedding_text_only", "embedding_trust_remote_code", "media_embedding_model",
    "es_index", "graph_db_path",
)


class EmbeddingBusyError(RuntimeError):
    """A model migration or another operation currently owns the runtime."""


def initialize_settings(settings) -> None:
    """Called once after Settings construction, before store/model imports."""
    global _directory, _baseline
    _directory = Path(settings.graph_db_path).expanduser().resolve().parent / ".embedding-runtime"
    _baseline = {name: getattr(settings, name) for name in _FIELDS}
    refresh_settings(settings)


def _root() -> Path:
    if _directory is None:
        from pkb.config import settings

        initialize_settings(settings)
    assert _directory is not None
    return _directory


def runtime_directory() -> Path:
    """Stable location shared by the server, CLI and background graph worker."""
    return _root()


def _read(name: str) -> dict | None:
    path = _root() / name
    return json.loads(path.read_text()) if path.is_file() else None


def _write(name: str, value: dict) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(root / name)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_settings(settings) -> None:
    manifest = _read("active.json")
    if manifest:
        for name, value in manifest["settings"].items():
            if name not in _FIELDS:
                raise ValueError(f"Unknown active embedding setting: {name}")
            setattr(settings, name, value)


def _lock(exclusive: bool) -> int:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / "operation.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise EmbeddingBusyError("임베딩 모델 전환 또는 다른 작업이 진행 중입니다. 잠시 후 다시 시도하세요.") from exc
    return descriptor


@contextmanager
def embedding_operation():
    """Refresh the selected stores while holding the shared cross-process gate."""
    from pkb.config import settings

    if getattr(_local, "depth", 0):
        _local.depth += 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    with _mutex:
        descriptor = _lock(False)
        try:
            refresh_settings(settings)
            _local.depth = 1
            yield
        finally:
            _local.depth = 0
            os.close(descriptor)


def _active_view(config: dict) -> dict:
    return {
        "provider": config["embedding_provider"],
        "endpoint": config["embedding_ollama_endpoint"] if config["embedding_provider"] == "ollama" else "",
        "model": config["embedding_ollama_model"] if config["embedding_provider"] == "ollama"
        else config["embedding_model"],
        "dimensions": config["embedding_dims"],
        "digest": config["embedding_ollama_digest"] if config["embedding_provider"] == "ollama" else "",
    }


def embedding_status() -> dict:
    """Read atomic JSON files without waiting for models, ES or a migration."""
    _root()
    manifest = _read("active.json")
    config = manifest["settings"] if manifest else _baseline
    job = _read("job.json")
    if job and job["status"] in {"queued", "running"}:
        try:
            descriptor = _lock(True)
        except EmbeddingBusyError:
            pass
        else:
            try:
                # Re-read after obtaining the lock: a worker may just have completed.
                job = _read("job.json")
                manifest = _read("active.json")
                if job and job["status"] in {"queued", "running"}:
                    if manifest and manifest.get("job_id") == job["id"]:
                        job.update(status="completed", phase="completed", error=None)
                    else:
                        job.update(status="failed", phase="interrupted",
                                   error="Migration worker stopped before activation; previous data remains active.")
                    _write("job.json", job)
                if manifest:
                    config = manifest["settings"]
            finally:
                os.close(descriptor)
    return {"active": _active_view(config), "job": job}


def embedding_models(endpoint: str) -> dict:
    from pkb.ollama_embeddings import list_models, normalize_endpoint

    endpoint = normalize_endpoint(endpoint)
    return {"endpoint": endpoint, "models": list_models(endpoint)}


def configure_embedding(endpoint: str, model: str) -> dict:
    """Start a durable detached job; the old configuration stays active until done."""
    from pkb.config import settings
    from pkb.ollama_embeddings import normalize_endpoint

    endpoint = normalize_endpoint(endpoint)
    if not model.strip():
        raise ValueError("임베딩 모델을 선택하세요.")
    if not _mutex.acquire(blocking=False):
        raise EmbeddingBusyError("다른 PKB 작업이 진행 중입니다. 완료 후 다시 시도하세요.")
    descriptor = None
    try:
        descriptor = _lock(True)
        refresh_settings(settings)
        source = {name: getattr(settings, name) for name in _FIELDS}
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "status": "queued", "phase": "validating", "processed": 0,
               "total": 0, "error": None}
        request = {"id": job_id, "endpoint": endpoint, "model": model.strip(), "source": source}
        _write(f"request-{job_id}.json", request)
        _write("job.json", job)
        # The lock is inherited before the parent closes its descriptor: there is
        # no unprotected queued interval, even if the server exits immediately.
        with (_root() / f"job-{job_id}.log").open("ab") as output:
            subprocess.Popen(
                [sys.executable, "-m", "pkb.embedding_runtime", str(_root()), job_id, str(descriptor)],
                pass_fds=(descriptor,), stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        return {"active": _active_view(source), "job": job}
    except Exception as exc:
        if descriptor is not None and "job" in locals():
            job.update(status="failed", phase="starting", error=f"{type(exc).__name__}: {exc}")
            _write("job.json", job)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _mutex.release()


def _copy_graph(source: str, target: Path) -> None:
    from pkb.graph.schema import init_schema

    path = Path(source).expanduser().resolve()
    if path.is_file():
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as old, sqlite3.connect(target) as new:
            old.backup(new)
    init_schema(str(target), normalize=False)


def run_migration(request: dict) -> None:
    """Only the inherited exclusive-lock subprocess may call this function."""
    from pkb.config import settings
    from pkb.embeddings import embed_documents
    from pkb.graph.schema import graph_connection
    from pkb.graph.store import reembed_concepts
    from pkb.ingest import embedding_identity
    from pkb.ollama_embeddings import inspect_model
    from pkb.reembed import reembed_index
    from pkb.store import get_client

    job = {"id": request["id"], "status": "running", "phase": "validating", "processed": 0,
           "total": 0, "error": None}

    def progress(phase: str, processed: int = 0, total: int = 0):
        job.update(phase=phase, processed=processed, total=total)
        _write("job.json", job)

    source = request["source"]
    try:
        progress("validating")
        selected = inspect_model(request["endpoint"], request["model"], probe=True)
        es = get_client()
        es.indices.refresh(index=source["es_index"])
        # Only the measured EmbeddingGemma 2 / 768 family is compatible with
        # existing image/audio vectors. Other models must not lose those rows.
        compatible_media = (selected["canonical_id"] == "google/embeddinggemma-2"
                            and selected["dimensions"] == 768)
        media_query = {"exists": {"field": "media_model"}}
        if compatible_media:
            media_query = {"bool": {"filter": [media_query], "must_not": [
                {"term": {"media_model": "google/embeddinggemma-2"}},
            ]}}
        incompatible = es.count(index=source["es_index"], query=media_query)["count"]
        if incompatible:
            raise ValueError(f"기존 미디어 청크 {incompatible}개의 벡터 공간이 호환되지 않아 전환을 중단했습니다.")
        total = es.count(index=source["es_index"])["count"]
        config = {
            **source, "embedding_provider": "ollama", "embedding_ollama_endpoint": request["endpoint"],
            "embedding_ollama_digest": selected["digest"], "embedding_model": selected["canonical_id"],
            "embedding_ollama_model": selected["model"],
            "embedding_ollama_query_prefix": selected["query_prefix"],
            "embedding_ollama_document_prefix": selected["document_prefix"],
            "embedding_dims": selected["dimensions"], "embedding_revision": "", "embedding_task": "",
            "embedding_query_prompt": "", "embedding_document_prompt": "", "embedding_normalize": True,
            "embedding_text_only": False, "embedding_trust_remote_code": False,
            "media_embedding_model": "google/embeddinggemma-2" if compatible_media else "",
        }
        for name, value in config.items():
            setattr(settings, name, value)
        target_index = f"pkb_embedding_{request['id']}"
        target_graph = _root() / f"graph-{request['id']}.sqlite"
        progress("chunks", total=total)
        result = reembed_index(
            es, source=source["es_index"], target=target_index, batch_size=32, strict_media=True,
            progress=lambda result: progress("chunks", result.scanned, total),
        )
        if not result.complete or result.media_dropped:
            raise RuntimeError("청크 재임베딩이 완료되지 않았습니다.")
        es.indices.refresh(index=target_index)
        if es.count(index=target_index)["count"] != total:
            raise RuntimeError("원본과 새 색인의 청크 수가 다릅니다.")
        progress("concepts")
        _copy_graph(source["graph_db_path"], target_graph)
        with graph_connection(str(target_graph), normalize=False) as conn:
            graph = reembed_concepts(conn, embed_documents, embedding_identity(), batch_size=32,
                                     progress=lambda done, count: progress("concepts", done, count))
            if not graph["complete"]:
                raise RuntimeError("개념 재임베딩이 완료되지 않았습니다.")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("새 그래프 무결성 검사에 실패했습니다.")
        # A tag can be replaced during a long migration. Never activate mixed
        # generations; the adapter also checks the pinned digest on each batch.
        current = inspect_model(request["endpoint"], request["model"], probe=True)
        if current["digest"] != selected["digest"] or current["dimensions"] != selected["dimensions"]:
            raise RuntimeError("재임베딩 중 Ollama 모델이 변경되었습니다.")
        config.update(es_index=target_index, graph_db_path=str(target_graph))
        progress("activating")
        _write("active.json", {"job_id": request["id"], "settings": config, "previous": source})
        job.update(status="completed", phase="completed", error=None)
        _write("job.json", job)
    except Exception as exc:
        active = _read("active.json")
        if active and active.get("job_id") == request["id"]:
            job.update(status="completed", phase="completed", error=None)
        else:
            job.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        _write("job.json", job)
    finally:
        for name, value in source.items():
            setattr(settings, name, value)


if __name__ == "__main__":
    # Config imports this module under its canonical name. Use that same module
    # instance for all runtime state rather than the separate __main__ globals.
    from pkb import embedding_runtime as runtime
    from pkb.config import settings as current_settings

    runtime.initialize_settings(current_settings)
    runtime._directory = Path(sys.argv[1])
    inherited = int(sys.argv[3])
    try:
        fcntl.flock(inherited, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime.run_migration(runtime._read(f"request-{sys.argv[2]}.json"))
    finally:
        os.close(inherited)
