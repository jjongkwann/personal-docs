"""Automatic extraction recovers from failures without blocking fresh inputs."""

from __future__ import annotations

import json
import signal
import threading

import pytest

from pkb.graph import extraction, worker
from pkb.graph.discovery import DiscoveryCache
from pkb.graph.rebuild import ExtractedBatch, ExtractedConcept, ExtractedItem
from pkb.graph.schema import graph_connection, init_schema


class FakeES:
    def __init__(self):
        self.indices = self
        self.uuid = "source-one"
        self.search_calls = 0
        self.mget_calls = 0
        self.stats_calls = 0
        self.refresh_calls = 0
        self._version = None
        self._seq = -1
        self.chunks = [
            {"doc_id": f"data/rag/{name}.md", "chunk_index": 0, "content_hash": f"h-{name}",
             "content": "BM25는 검색 알고리즘이다.", "title": "BM25", "section_path": "검색", "category": "rag"}
            for name in ("a", "b")
        ]

    def stats(self, **kwargs):
        self.stats_calls += 1
        version = json.dumps(self.chunks, sort_keys=True)
        if version != self._version:
            self._seq += 1
            self._version = version
        return {"_shards": {"failed": 0}, "indices": {"physical": {
            "uuid": self.uuid, "shards": {"0": [{
                "routing": {"primary": True, "state": "STARTED"},
                "seq_no": {"max_seq_no": self._seq, "local_checkpoint": self._seq,
                           "global_checkpoint": self._seq},
            }]},
        }}}

    def refresh(self, **kwargs):
        self.refresh_calls += 1
        return {"_shards": {"failed": 0}}

    def search(self, *, size, search_after=None, **kwargs):
        self.search_calls += 1
        rows = sorted(self.chunks, key=lambda c: (c["doc_id"], c["chunk_index"]))
        if search_after:
            rows = [c for c in rows if [c["doc_id"], c["chunk_index"]] > search_after]
        return {"hits": {"hits": [{"_source": dict(c), "sort": [c["doc_id"], c["chunk_index"]]}
                                   for c in rows[:size]]}}

    def mget(self, *, ids, **kwargs):
        self.mget_calls += 1
        by_id = {f"{c['doc_id']}_{c['chunk_index']}": c for c in self.chunks}
        return {"docs": [{"found": key in by_id, "_source": dict(by_id.get(key, {}))} for key in ids]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    db = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr(worker.settings, "graph_db_path", db)
    monkeypatch.setattr(worker.settings, "graph_auto_extract", True)
    init_schema(db)
    es = FakeES()
    monkeypatch.setattr(worker, "get_client", lambda: es)
    monkeypatch.setattr(extraction, "get_client", lambda: es)
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.embeddings.embed_documents", lambda texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(extraction, "generation_model", lambda *a, **k: {"digest": "sha256:one"})
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])

    def extract(chunks, hints, **kwargs):
        return ExtractedBatch(items=[ExtractedItem(
            doc_id=c["doc_id"], chunk_index=c["chunk_index"],
            concepts=[ExtractedConcept(name="BM25", kind="algorithm")],
        ) for c in chunks]), {}

    monkeypatch.setattr(extraction, "_ollama_extract", extract)
    return es, db, clock


def tick(discovery=None):
    with worker.worker_lock():
        return worker.worker_tick(progress=lambda _: None, discovery=discovery)


def test_failed_first_chunk_does_not_starve_new_input(env, monkeypatch):
    es, db, clock = env
    monkeypatch.setattr(worker, "MAX_CHUNKS", 1)
    original = extraction._ollama_extract

    def fail_first(chunks, *args, **kwargs):
        if chunks[0]["doc_id"].endswith("a.md"):
            raise ValueError("invalid structured output")
        return original(chunks, *args, **kwargs)

    monkeypatch.setattr(extraction, "_ollama_extract", fail_first)
    assert tick()["failed"] == 1
    clock[0] += 1
    assert tick()["stored"] == 1
    with graph_connection(db, normalize=False) as conn:
        assert conn.execute("SELECT doc_id FROM extracted_chunks").fetchone()[0].endswith("b.md")
    assert worker.worker_status()["retries"]["cooling_down"] == 1


def test_restart_preserves_backoff_and_offline_input_recovers(env, monkeypatch):
    es, db, clock = env
    es.chunks = es.chunks[:1]
    original = extraction.generation_model
    monkeypatch.setattr(extraction, "generation_model", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    assert worker.run_worker(once=True)["failed"] == 1
    assert worker.worker_status()["retries"]["next_attempt_at"] == 1030
    monkeypatch.setattr(extraction, "generation_model", original)
    # A fresh run_worker call holds a fresh lock; cooldown lives in SQLite.
    assert worker.run_worker(once=True)["selected"] == 0
    clock[0] += 31
    assert worker.run_worker(once=True)["stored"] == 1
    assert worker.worker_status()["retries"]["items"] == 0
    assert worker.worker_status()["heartbeat"]["status"] == "stopped"


def test_retry_delay_grows_and_changed_input_is_immediately_eligible(env, monkeypatch):
    es, db, clock = env
    es.chunks = es.chunks[:1]
    original = extraction._ollama_extract
    monkeypatch.setattr(extraction, "_ollama_extract", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    tick()
    clock[0] += 31
    tick()
    assert worker.worker_status()["retries"]["next_attempt_at"] == 1091
    es.chunks[0]["title"] = "새 제목"
    monkeypatch.setattr(extraction, "_ollama_extract", original)
    assert tick()["stored"] == 1
    assert worker.worker_status()["retries"]["items"] == 0
    with graph_connection(db, normalize=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_extraction_runs").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM graph_extraction_events WHERE status = 'failed'").fetchone()[0] == 2


def test_duplicate_worker_refuses_lock_without_changing_heartbeat(env):
    with worker.worker_lock(), pytest.raises(worker.WorkerAlreadyRunningError):
        worker.run_worker(once=True)
    assert worker.worker_status()["heartbeat"] is None
    assert worker.run_worker(once=True)["stored"] == 2


def test_rebuild_pauses_without_calling_elasticsearch_or_ollama(env, monkeypatch):
    _, db, _ = env
    with graph_connection(db, normalize=False) as conn:
        conn.execute("INSERT INTO graph_meta VALUES ('edge_evidence_rebuild', 'active')")
    monkeypatch.setattr(worker, "get_client", lambda: pytest.fail("rebuild must pause before scanning"))
    result = worker.run_worker(once=True)
    assert result["status"] == "paused"
    assert result["reason"] == "rebuild"


def test_stop_finishes_current_request_and_leaves_other_chunks_pending(env, monkeypatch):
    stop = threading.Event()
    original = extraction._ollama_extract

    def stop_after_current(*args, **kwargs):
        result = original(*args, **kwargs)
        stop.set()
        return result

    monkeypatch.setattr(extraction, "_ollama_extract", stop_after_current)
    with worker.worker_lock():
        assert worker.worker_tick(stop=stop, progress=lambda _: None)["stored"] == 1
    monkeypatch.setattr(extraction, "_ollama_extract", original)
    assert tick()["stored"] == 1


def test_crashed_running_item_is_selected_from_source_markers(env, monkeypatch):
    es, db, _ = env
    es.chunks = es.chunks[:1]
    original = extraction._ollama_extract
    monkeypatch.setattr(extraction, "_ollama_extract", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        tick()
    monkeypatch.setattr(extraction, "_ollama_extract", original)
    assert tick()["stored"] == 1
    with graph_connection(db, normalize=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_extraction_items WHERE status = 'running'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM extracted_chunks").fetchone()[0] == 1


def test_disabled_worker_does_not_create_database(tmp_path, monkeypatch):
    path = tmp_path / "not-created" / "graph.sqlite"
    monkeypatch.setattr(worker.settings, "graph_db_path", str(path))
    monkeypatch.setattr(worker.settings, "graph_auto_extract", False)
    assert worker.run_worker(once=True)["status"] == "disabled"
    assert not path.parent.exists()
    assert worker.worker_status()["heartbeat"] is None


def test_loop_recovers_after_service_errors_with_capped_backoff(env, monkeypatch):
    calls = []
    waits = []

    def failing_tick(*, stop, progress, discovery):
        calls.append(True)
        if len(calls) <= 10:
            raise RuntimeError("offline")
        stop.set()
        return {"status": "idle"}

    monkeypatch.setattr(worker, "worker_tick", failing_tick)
    monkeypatch.setattr(worker, "_wait", lambda stop, delay, heartbeat: waits.append(delay))
    assert worker.run_worker()["status"] == "idle"
    assert waits[:3] == [30, 60, 120]
    assert waits[7:10] == [3600, 3600, 3600]
    assert len(calls) == 11


def test_sigterm_stops_between_chunks_and_restores_handler(env, monkeypatch):
    previous = signal.getsignal(signal.SIGTERM)
    original = extraction._ollama_extract

    def request_stop(*args, **kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return original(*args, **kwargs)

    monkeypatch.setattr(extraction, "_ollama_extract", request_stop)
    assert worker.run_worker()["stored"] == 1
    assert signal.getsignal(signal.SIGTERM) is previous


def test_long_wait_pulses_heartbeat_and_stops_promptly(env, monkeypatch):
    monotonic = [0.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: monotonic[0])

    class Stop:
        def is_set(self):
            return False

        def wait(self, delay):
            assert delay == 20
            monotonic[0] += delay
            return monotonic[0] >= 40

    worker._wait(Stop(), 3600, {"status": "waiting", "error": "offline", "next_poll_at": 4600})
    heartbeat = worker.worker_status()["heartbeat"]
    assert heartbeat["status"] == "waiting"
    assert heartbeat["error"] == "offline"
    assert heartbeat["next_poll_at"] == 4600


def test_prerun_invalid_input_does_not_turn_its_retry_into_global_starvation(env, monkeypatch):
    es, db, clock = env
    es.chunks[0]["content_hash"] = ""
    monkeypatch.setattr(worker, "MAX_CHUNKS", 1)
    waits = []

    def wait(stop, delay, heartbeat):
        waits.append(delay)
        clock[0] += delay
        if len(waits) == 2:
            stop.set()

    monkeypatch.setattr(worker, "_wait", wait)
    assert worker.run_worker()["stored"] == 1
    assert waits == [20, 20]
    with graph_connection(db, normalize=False) as conn:
        assert conn.execute("SELECT doc_id FROM extracted_chunks").fetchone()[0].endswith("b.md")
        assert conn.execute("SELECT attempts FROM graph_extraction_retries").fetchone()[0] == 1


def test_new_input_precedes_an_overdue_lexicographically_earlier_retry(env, monkeypatch):
    es, db, clock = env
    es.chunks[0]["content_hash"] = ""
    monkeypatch.setattr(worker, "MAX_CHUNKS", 1)
    assert tick()["failed"] == 1
    clock[0] += 31  # The earlier failed input is due, not merely cooling down.
    assert tick()["stored"] == 1
    with graph_connection(db, normalize=False) as conn:
        assert conn.execute("SELECT doc_id FROM extracted_chunks").fetchone()[0].endswith("b.md")
        assert conn.execute("SELECT attempts FROM graph_extraction_retries").fetchone()[0] == 1


def test_retries_use_oldest_due_time_before_document_sort_order(env, monkeypatch):
    es, db, clock = env
    monkeypatch.setattr(worker, "MAX_CHUNKS", 1)
    worker._failed(es.chunks[0], "newer failure", 995)
    worker._failed(es.chunks[1], "older failure", 990)
    clock[0] = 1030
    assert tick()["stored"] == 1
    with graph_connection(db, normalize=False) as conn:
        assert conn.execute("SELECT doc_id FROM extracted_chunks").fetchone()[0].endswith("b.md")


def test_discovery_scans_once_while_backlog_and_idle_ticks_use_compact_versions(env, monkeypatch):
    es, _, _ = env
    monkeypatch.setattr(worker, "MAX_CHUNKS", 1)
    discovery = DiscoveryCache()
    assert tick(discovery)["stored"] == 1
    searches = es.search_calls
    assert tick(discovery)["stored"] == 1
    mgets = es.mget_calls
    assert tick(discovery)["pending"] == 0
    assert es.search_calls == searches
    assert es.mget_calls == mgets
    assert es.refresh_calls == 1
    assert all(set(source) == {"doc_id", "chunk_index", "content_hash", "input_hash"}
               for source in discovery.sources)


def test_discovery_retries_without_fetching_sources_during_cooldown(env, monkeypatch):
    es, _, clock = env
    es.chunks = es.chunks[:1]
    discovery = DiscoveryCache()
    original = extraction._ollama_extract
    monkeypatch.setattr(extraction, "_ollama_extract", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    assert tick(discovery)["failed"] == 1
    searches, mgets = es.search_calls, es.mget_calls
    assert tick(discovery)["selected"] == 0
    assert es.search_calls == searches
    assert es.mget_calls == mgets
    clock[0] += 31
    monkeypatch.setattr(extraction, "_ollama_extract", original)
    assert tick(discovery)["stored"] == 1
    assert es.search_calls == searches


def test_discovery_observes_new_metadata_changes_and_deletions(env):
    es, _, _ = env
    es.chunks = es.chunks[:1]
    discovery = DiscoveryCache()
    assert tick(discovery)["stored"] == 1
    es.chunks.append({**es.chunks[0], "doc_id": "data/rag/new.md"})
    assert tick(discovery)["stored"] == 1
    es.chunks[0]["title"] = "changed title with same content hash"
    assert tick(discovery)["stored"] == 1
    worker._failed(es.chunks[1], "obsolete retry", 1000)
    es.chunks.pop()
    result = tick(discovery)
    assert result["total"] == 1
    assert result["pending"] == 0
    assert worker.worker_status()["retries"]["items"] == 0
    assert es.refresh_calls == 4


def test_discovery_marker_reset_reextracts_without_scanning_source(env):
    es, db, _ = env
    discovery = DiscoveryCache()
    assert tick(discovery)["stored"] == 2
    searches = es.search_calls
    # A rebuild can begin and finish entirely between worker ticks.
    with graph_connection(db, normalize=False) as conn:
        conn.execute("DELETE FROM extracted_chunks")
    assert tick(discovery)["stored"] == 2
    assert es.search_calls == searches


def test_discovery_invalidates_for_index_recreation_and_generation_switch(env, monkeypatch, tmp_path):
    es, _, _ = env
    discovery = DiscoveryCache()
    assert tick(discovery)["stored"] == 2
    searches = es.search_calls
    es.uuid = "recreated-same-name-and-sequence"
    assert tick(discovery)["pending"] == 0
    assert es.search_calls > searches
    searches = es.search_calls
    path = str(tmp_path / "next-generation.sqlite")
    init_schema(path)
    monkeypatch.setattr(worker.settings, "graph_db_path", path)
    monkeypatch.setattr(worker.settings, "es_index", "next-generation")
    assert tick(discovery)["stored"] == 2
    assert es.search_calls > searches


def test_discovery_does_not_publish_a_scan_with_concurrent_writes(env, monkeypatch):
    es, _, _ = env
    discovery = DiscoveryCache()
    original = es.search
    changed = False

    def racing_search(**kwargs):
        nonlocal changed
        result = original(**kwargs)
        if not changed:
            es.chunks[0]["title"] = "concurrent title"
            changed = True
        return result

    monkeypatch.setattr(es, "search", racing_search)
    result = tick(discovery)
    assert result["stored"] == 1  # The unchanged b chunk still progresses.
    assert result["stale"] == 1
    assert result["reason"] == "source_changed"
    assert result["discovery_complete"] is False
    assert discovery.snapshot is None
    assert tick(discovery)["stored"] == 1


def test_discovery_statistics_failure_is_surfaced_without_a_fallback_scan(env, monkeypatch):
    es, _, _ = env
    discovery = DiscoveryCache()
    assert tick(discovery)["stored"] == 2
    searches = es.search_calls
    original = es.stats
    monkeypatch.setattr(es, "stats", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("statistics offline")))
    with pytest.raises(RuntimeError, match="statistics offline"):
        tick(discovery)
    assert es.search_calls == searches
    assert discovery.snapshot is None
    monkeypatch.setattr(es, "stats", original)
    assert tick(discovery)["pending"] == 0
    assert es.search_calls > searches


def test_discovery_refreshes_new_writes_before_search(env, monkeypatch):
    es, _, _ = env
    discovery = DiscoveryCache()
    original = es.search
    visible = []

    def refresh(**kwargs):
        nonlocal visible
        es.refresh_calls += 1
        visible = [dict(chunk) for chunk in es.chunks]
        return {"_shards": {"failed": 0}}

    def search(**kwargs):
        current = es.chunks
        try:
            es.chunks = visible
            return original(**kwargs)
        finally:
            es.chunks = current

    monkeypatch.setattr(es, "refresh", refresh)
    monkeypatch.setattr(es, "search", search)
    assert tick(discovery)["stored"] == 2
    es.chunks.append({**es.chunks[0], "doc_id": "data/rag/unrefreshed.md"})
    assert tick(discovery)["stored"] == 1


def test_discovery_mget_failure_schedules_the_descriptor_input_version(env, monkeypatch):
    es, db, clock = env
    es.chunks = es.chunks[:1]
    discovery = DiscoveryCache()
    original = es.mget
    monkeypatch.setattr(es, "mget", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("lookup offline")))
    assert tick(discovery)["failed"] == 1
    with graph_connection(db, normalize=False) as conn:
        retry = conn.execute("SELECT input_hash FROM graph_extraction_retries").fetchone()
        assert retry["input_hash"] == extraction.graph_input_hash(es.chunks[0])
    assert tick(discovery)["selected"] == 0
    clock[0] += 31
    monkeypatch.setattr(es, "mget", original)
    assert tick(discovery)["stored"] == 1


@pytest.mark.parametrize("incomplete", [{"timed_out": True}, {"_shards": {"failed": 1}}])
def test_discovery_never_caches_partial_source_searches(env, monkeypatch, incomplete):
    es, _, _ = env
    discovery = DiscoveryCache()
    original = es.search
    monkeypatch.setattr(es, "search", lambda **kwargs: {**original(**kwargs), **incomplete})
    with pytest.raises(RuntimeError, match="incomplete results"):
        tick(discovery)
    assert discovery.snapshot is None
    assert es.mget_calls == 0
    monkeypatch.setattr(es, "search", original)
    assert tick(discovery)["stored"] == 2


def test_worker_loop_preserves_discovery_between_ticks(env, monkeypatch):
    es, _, _ = env
    waits = []

    def wait(stop, delay, heartbeat):
        waits.append(delay)
        if len(waits) == 3:
            stop.set()

    monkeypatch.setattr(worker, "_wait", wait)
    assert worker.run_worker()["pending"] == 0
    assert es.search_calls == 2  # Data page plus terminating empty page, once.
    assert es.refresh_calls == 1


def test_raced_discovery_preserves_retries_for_sources_missing_from_the_scan(env, monkeypatch):
    es, db, _ = env
    discovery = DiscoveryCache()
    worker._failed(es.chunks[1], "still cooling down", 1000)
    original = es.search

    def incomplete_during_ingestion(**kwargs):
        result = original(**kwargs)
        if not kwargs.get("search_after"):
            result["hits"]["hits"] = result["hits"]["hits"][:1]
            es.chunks[0]["title"] = "concurrent source edit"
        return result

    monkeypatch.setattr(es, "search", incomplete_during_ingestion)
    result = tick(discovery)
    assert result["reason"] == "source_changed"
    with graph_connection(db, normalize=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_extraction_retries").fetchone()[0] == 1
    monkeypatch.setattr(es, "search", original)
    assert tick(discovery)["discovery_complete"] is True
    assert worker.worker_status()["retries"]["cooling_down"] == 1


def test_sustained_ingestion_processes_stable_sources_without_global_backoff(env, monkeypatch):
    es, db, _ = env
    discovery = DiscoveryCache()
    monkeypatch.setattr(worker, "DiscoveryCache", lambda: discovery)
    original = es.search
    edits = 0
    waits = []

    def ingest_while_searching(**kwargs):
        nonlocal edits
        result = original(**kwargs)
        if not kwargs.get("search_after"):
            edits += 1
            es.chunks[0]["title"] = f"concurrent edit {edits}"
        return result

    def wait(stop, delay, heartbeat):
        waits.append(delay)
        assert heartbeat["reason"] == "source_changed"
        assert heartbeat["error"] is None
        if len(waits) == 4:
            stop.set()
        else:
            es.chunks.append({**es.chunks[1], "doc_id": f"data/rag/stable-{len(waits)}.md"})

    monkeypatch.setattr(es, "search", ingest_while_searching)
    monkeypatch.setattr(worker, "_wait", wait)
    result = worker.run_worker()
    assert result["stored"] == 1
    assert waits == [20, 20, 20, 20]
    assert discovery.snapshot is None
    with graph_connection(db, normalize=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM extracted_chunks").fetchone()[0] == 4
    monkeypatch.setattr(es, "search", original)
    assert tick(discovery)["stored"] == 1
    assert discovery.snapshot is not None
    searches = es.search_calls
    assert tick(discovery)["discovery_complete"] is True
    assert es.search_calls == searches
