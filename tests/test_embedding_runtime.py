"""Model selection keeps the old stores intact until the single manifest commit."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_reembed import FakeES, _chunk, _source

from pkb import embedding_runtime as runtime
from pkb.config import settings
from pkb.graph import store as graph_store
from pkb.graph.schema import graph_connection, init_schema


@pytest.fixture
def migration(monkeypatch, tmp_path):
    for name in runtime._FIELDS:
        monkeypatch.setattr(settings, name, getattr(settings, name))
    source_graph = tmp_path / "original.sqlite"
    init_schema(str(source_graph), normalize=False)
    with graph_connection(str(source_graph), normalize=False) as conn:
        graph_store.upsert_concept(conn, name="BM25", description="랭킹 함수", embedding=[1.0, 0.0])
    monkeypatch.setattr(settings, "graph_db_path", str(source_graph))
    monkeypatch.setattr(settings, "es_index", "original")
    source = {key: getattr(settings, key) for key in runtime._FIELDS}
    monkeypatch.setattr(runtime, "_baseline", source)
    es = FakeES({"original": _source(_chunk("data/a.md", 0, "검색 본문"))})
    monkeypatch.setattr(es.indices, "refresh", lambda **kwargs: None, raising=False)

    def count(*, index, query=None):
        values = list(es.data[index].values())
        if query:
            values = [doc for doc in values if "media_model" in doc]
            if "bool" in query:
                values = [doc for doc in values if doc["media_model"] != "google/embeddinggemma-2"]
        return {"count": len(values)}

    monkeypatch.setattr(es, "count", count, raising=False)
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.reembed.backfill_document_index", lambda es: {"documents": 1})
    selected = {"model": "embed:latest", "canonical_id": "ollama/embed:latest", "dimensions": 3,
                "digest": "sha256:a", "query_prefix": "", "document_prefix": ""}
    monkeypatch.setattr("pkb.ollama_embeddings.inspect_model", lambda *a, **k: dict(selected))
    monkeypatch.setattr("pkb.embeddings.embed_documents", lambda texts, **kwargs: [[0.0, 1.0, 0.0] for _ in texts])
    request = {"id": "testjob", "endpoint": "http://windows:11434", "model": "embed:latest", "source": source}
    return request, es, source_graph, selected


def test_migration_publishes_matching_stores_and_preserves_originals(migration):
    request, es, source_graph, _ = migration
    original = copy.deepcopy(es.data["original"])
    original_bytes = source_graph.read_bytes()

    runtime.run_migration(request)

    status = runtime.embedding_status()
    assert status["job"]["status"] == "completed"
    assert status["active"] == {"provider": "ollama", "endpoint": "http://windows:11434",
                                "model": "embed:latest", "dimensions": 3, "digest": "sha256:a"}
    manifest = runtime._read("active.json")
    assert manifest["previous"] == request["source"]
    active = manifest["settings"]
    assert active["es_index"] == "pkb_embedding_testjob"
    assert es.mappings[active["es_index"]]["properties"]["embedding"]["dims"] == 3
    assert es.data[active["es_index"]]["data/a.md_0"]["embedding"] == [0.0, 1.0, 0.0]
    assert es.data["original"] == original
    assert source_graph.read_bytes() == original_bytes
    with graph_connection(active["graph_db_path"], normalize=False) as conn:
        vector = conn.execute("SELECT embedding FROM concepts").fetchone()[0]
        assert graph_store._unpack_embedding(vector) == [0., 1., 0.]
    # A long-running process refreshes both addresses and the provider together.
    assert settings.es_index == "original"
    with runtime.embedding_operation():
        assert settings.es_index == active["es_index"]
        assert settings.graph_db_path == active["graph_db_path"]
        assert settings.embedding_provider == "ollama"


def test_graph_failure_never_activates_partial_migration(migration, monkeypatch):
    request, es, source_graph, _ = migration
    before = source_graph.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("concept inference offline")

    monkeypatch.setattr(graph_store, "reembed_concepts", fail)
    runtime.run_migration(request)

    assert runtime._read("active.json") is None
    assert runtime.embedding_status()["job"]["status"] == "failed"
    assert "concept inference offline" in runtime.embedding_status()["job"]["error"]
    assert "pkb_embedding_testjob" in es.data  # partial data retained for diagnosis
    assert source_graph.read_bytes() == before
    assert settings.es_index == "original"


def test_unsupported_media_rejected_before_any_new_index(migration):
    request, es, _, _ = migration
    es.data["original"]["media"] = _chunk("data/a.png", 0, "image", media_model="google/embeddinggemma-2")
    runtime.run_migration(request)
    assert runtime._read("active.json") is None
    assert runtime.embedding_status()["job"]["status"] == "failed"
    assert set(es.data) == {"original"}


def test_verified_gemma_media_preserved_with_original_vectors(migration, monkeypatch):
    request, es, _, selected = migration
    selected.update(canonical_id="google/embeddinggemma-2", dimensions=768)
    media = _chunk("data/a.png", 0, "image", media_model="google/embeddinggemma-2")
    media["embedding"] = [1.0] + [0.0] * 767
    es.data["original"]["media"] = copy.deepcopy(media)
    monkeypatch.setattr("pkb.embeddings.embed_documents",
                        lambda texts, **kwargs: [[0., 1.] + [0.] * 766 for _ in texts])
    runtime.run_migration(request)
    assert runtime.embedding_status()["job"]["status"] == "completed"
    assert es.data["pkb_embedding_testjob"]["media"] == media


def test_exclusive_gate_blocks_operations_but_status_remains_available(migration):
    from pkb.graph.worker import worker_tick

    runtime._write("job.json", {"id": "pending", "status": "running", "phase": "chunks"})
    descriptor = runtime._lock(True)
    try:
        with pytest.raises(runtime.EmbeddingBusyError), runtime.embedding_operation():
            pytest.fail("must not enter old generation")
        assert runtime.embedding_status()["job"]["status"] == "running"
        assert worker_tick()["reason"] == "embedding_migration"
    finally:
        os.close(descriptor)
    # OS crash releases the lock; the next status read records a terminal failure.
    assert runtime.embedding_status()["job"]["status"] == "failed"


def test_gate_is_shared_with_a_separate_python_process(migration):
    script = """
import sys
from pathlib import Path
from pkb import embedding_runtime as runtime
from pkb.config import settings
runtime._directory = Path(sys.argv[1])
try:
    with runtime.embedding_operation():
        print('entered')
except runtime.EmbeddingBusyError:
    print('busy')
"""
    descriptor = runtime._lock(True)
    try:
        child = subprocess.run([sys.executable, "-c", script, str(runtime.runtime_directory())],
                               capture_output=True, text=True, check=True, timeout=20)
        assert child.stdout.strip() == "busy"
    finally:
        os.close(descriptor)


def test_completed_manifest_recovers_crash_before_job_status(migration):
    request, _, _, _ = migration
    runtime.run_migration(request)
    runtime._write("job.json", {"id": "testjob", "status": "running", "phase": "activating"})
    assert runtime.embedding_status()["job"]["status"] == "completed"


def test_configure_persists_request_and_hands_off_lock(migration, monkeypatch):
    captured = {}

    def spawn(args, **kwargs):
        captured.update(args=args, **kwargs)
        captured["inherited"] = os.dup(kwargs["pass_fds"][0])
        with pytest.raises(runtime.EmbeddingBusyError):
            runtime._lock(False)

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    status = runtime.configure_embedding("windows", "embed:latest")
    try:
        assert status["job"]["status"] == "queued"
        assert runtime.embedding_status()["job"]["status"] == "queued"
        request = runtime._read(f"request-{status['job']['id']}.json")
        assert request["endpoint"] == "http://windows:11434"
        assert request["source"]["es_index"] == "original"
        assert captured["start_new_session"] is True
        assert Path(captured["args"][3]) == runtime.runtime_directory()
    finally:
        os.close(captured["inherited"])


@pytest.mark.parametrize("provider", ["local", "ollama"])
def test_media_listing_advertises_gated_active_backend(migration, monkeypatch, provider):
    from pkb.mcp_server import list_media

    monkeypatch.setattr(settings, "embedding_provider", provider)
    monkeypatch.setattr(settings, "embedding_ollama_endpoint", "http://windows:11434")
    monkeypatch.setattr(settings, "embedding_ollama_model", "gemma:latest")
    monkeypatch.setattr(settings, "embedding_ollama_digest", "sha256:a")
    monkeypatch.setattr("pkb.media.list_media",
                        lambda *args: {"total": 0, "model": "google/embeddinggemma-2", "media": []})
    result = json.loads(list_media())
    assert result["model"] == "google/embeddinggemma-2"
    backend = result["embedding_backend"]
    assert backend["provider"] == provider
    if provider == "ollama":
        assert backend == {"provider": "ollama", "endpoint": "http://windows:11434", "model": "gemma:latest",
                           "digest": "sha256:a", "dimensions": settings.embedding_dims,
                           "batch_size": settings.embedding_batch_size}
    else:
        assert backend == {"provider": "local"}


def test_management_tools_registered_in_core_and_require_admin():
    from pkb.mcp_server import CORE_TOOLS, mcp

    names = {"embedding_status", "embedding_models", "configure_embedding"}
    assert names <= CORE_TOOLS
    assert names <= set(mcp._tool_manager._tools)
    # The registry tracks access annotations used by the policy system.
    from pkb import mcp_server

    for name in names:
        assert getattr(mcp_server, name).pkb_access == "admin"
