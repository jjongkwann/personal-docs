"""Incremental extraction stays bounded, auditable and independent of rebuild/reset."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pkb.graph import extraction, ollama
from pkb.graph.rebuild import ExtractedBatch, ExtractedConcept, ExtractedItem, ExtractedRelation, _storage_payload
from pkb.graph.schema import graph_connection, init_schema
from pkb.graph.services import graph_input_hash


class FakeES:
    def __init__(self):
        self.chunks = [
            {
                "doc_id": "data/rag/x.md",
                "chunk_index": i,
                "content_hash": f"h{i}",
                "content": "BM25는 검색 알고리즘이다.",
                "title": "BM25",
                "section_path": "검색",
                "category": "rag",
            }
            for i in range(3)
        ]

    def search(self, *, query, size, search_after=None, **kwargs):
        term = query["bool"]["must"][0].get("term", {}).get("doc_id")
        rows = [c for c in self.chunks if not term or c["doc_id"] == term]
        if search_after:
            rows = [c for c in rows if [c["doc_id"], c["chunk_index"]] > search_after]
        return {"hits": {"hits": [{"_source": dict(c), "sort": [c["doc_id"], c["chunk_index"]]} for c in rows[:size]]}}

    def mget(self, *, ids, **kwargs):
        by_id = {f"{c['doc_id']}_{c['chunk_index']}": c for c in self.chunks}
        return {"docs": [{"found": key in by_id, "_source": dict(by_id.get(key, {}))} for key in ids]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    db = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db)
    init_schema(db)
    es = FakeES()
    monkeypatch.setattr(extraction, "get_client", lambda: es)
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.embeddings.embed_documents", lambda texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(extraction, "generation_model", lambda *a, **k: {"digest": "sha256:one"})

    def extract(chunks, hints, **kwargs):
        return ExtractedBatch(
            items=[
                ExtractedItem(
                    doc_id=c["doc_id"],
                    chunk_index=c["chunk_index"],
                    concepts=[ExtractedConcept(name="BM25", kind="algorithm", description="검색 알고리즘")],
                )
                for c in chunks
            ]
        ), {"prompt_eval_count": 123}

    monkeypatch.setattr(extraction, "_ollama_extract", extract)
    return es, db


def test_bounded_extract_stores_without_reset_and_keeps_old_data(env):
    es, db = env
    result = extraction.extract_pending(doc_id="data/rag/x.md", max_chunks=1, progress=lambda _: None)
    assert result["selection_complete"] is True
    assert result["items"] == {"stored": 1}
    record = extraction.runs(run_id=result["run_id"])[0]
    assert record["model_digest"] == "sha256:one"
    assert record["pending_before"] == 3
    assert record["items"][0]["input_hash"] == graph_input_hash(es.chunks[0])
    assert record["items"][0]["attempts"] == 1
    with graph_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM extracted_chunks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM concept_mentions").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM graph_meta WHERE key='edge_evidence_rebuild'").fetchone()
    next_result = extraction.extract_pending(max_chunks=1, progress=lambda _: None)
    assert extraction.runs(run_id=next_result["run_id"])[0]["items"][0]["chunk_index"] == 1


def test_resume_retries_failed_items_only_and_preserves_failure_events(env, monkeypatch):
    original = extraction._ollama_extract
    calls = []

    def fail_second(chunks, *args, **kwargs):
        calls.append(chunks[0]["chunk_index"])
        if chunks[0]["chunk_index"] == 1:
            raise RuntimeError("offline")
        return original(chunks, *args, **kwargs)

    monkeypatch.setattr(extraction, "_ollama_extract", fail_second)
    first = extraction.extract_pending(max_chunks=2, retries=1, progress=lambda _: None)
    assert first["items"] == {"failed": 1, "stored": 1}
    assert calls == [0, 1, 1]
    monkeypatch.setattr(extraction, "_ollama_extract", original)
    second = extraction.extract_pending(resume=first["run_id"], progress=lambda _: None)
    record = extraction.runs(run_id=first["run_id"])[0]
    assert second["items"] == {"stored": 2}
    assert [item["attempts"] for item in record["items"]] == [1, 3]
    assert any(event["error"] and "offline" in event["error"] for event in record["events"])


def test_resume_refuses_changed_model_and_source(env, monkeypatch):
    es, _ = env
    original = extraction._ollama_extract
    monkeypatch.setattr(extraction, "_ollama_extract", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    first = extraction.extract_pending(max_chunks=1, retries=0, progress=lambda _: None)
    monkeypatch.setattr(extraction, "generation_model", lambda *a, **k: {"digest": "sha256:changed"})
    with pytest.raises(ValueError, match="digest"):
        extraction.extract_pending(resume=first["run_id"])
    monkeypatch.setattr(extraction, "generation_model", lambda *a, **k: {"digest": "sha256:one"})
    monkeypatch.setattr(extraction, "_ollama_extract", original)
    es.chunks[0]["title"] = "changed title"
    result = extraction.extract_pending(resume=first["run_id"], progress=lambda _: None)
    assert result["items"] == {"stale": 1}
    assert extraction.runs(run_id=first["run_id"])[0]["items"][0]["input_hash"] != graph_input_hash(es.chunks[0])


def test_interrupted_run_is_recoverable(env, monkeypatch):
    original = extraction._ollama_extract
    monkeypatch.setattr(extraction, "_ollama_extract", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        extraction.extract_pending(max_chunks=1, progress=lambda _: None)
    record = extraction.runs()[0]
    assert record["status"] == "interrupted"
    monkeypatch.setattr(extraction, "_ollama_extract", original)
    assert extraction.extract_pending(resume=record["run_id"], progress=lambda _: None)["selection_complete"]


@pytest.mark.parametrize("field,value", [
    ("source_type", "image"), ("source_type", "audio"),
    ("import_extraction", "unsupported"), ("import_extraction", "empty"),
])
def test_selected_input_excluded_before_inference_is_stale(env, monkeypatch, field, value):
    es, db = env
    selected = dict(es.chunks[0])

    def change_applicability(*args, **kwargs):
        es.chunks[0][field] = value
        return {"digest": "sha256:one"}

    monkeypatch.setattr(extraction, "generation_model", change_applicability)
    monkeypatch.setattr(extraction, "_ollama_extract", lambda *a, **k: pytest.fail("excluded source inference"))
    result = extraction.extract_pending(max_chunks=1, _selected_chunks=[selected], progress=lambda _: None)
    assert result["items"] == {"stale": 1}
    with graph_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM extracted_chunks").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [
    ("source_type", "image"), ("source_type", "audio"),
    ("import_extraction", "unsupported"), ("import_extraction", "empty"),
])
def test_input_excluded_during_generation_cannot_store_evidence(env, monkeypatch, field, value):
    es, db = env
    original = extraction._ollama_extract

    def change_applicability(*args, **kwargs):
        result = original(*args, **kwargs)
        es.chunks[0][field] = value
        return result

    monkeypatch.setattr(extraction, "_ollama_extract", change_applicability)
    result = extraction.extract_pending(max_chunks=1, retries=0, progress=lambda _: None)
    assert result["items"] == {"failed": 1}
    with graph_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM extracted_chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM concept_mentions").fetchone()[0] == 0


def test_schema_upgrade_keeps_legacy_graph_rows(env):
    _, db = env
    with graph_connection(db) as conn:
        conn.execute("INSERT INTO concepts (name,slug,created_at,updated_at) VALUES ('Legacy','legacy','t','t')")
        conn.execute("DROP TABLE graph_extraction_events")
        conn.execute("DROP TABLE graph_extraction_items")
        conn.execute("DROP TABLE graph_extraction_runs")
    init_schema(db)
    with graph_connection(db) as conn:
        assert conn.execute("SELECT name FROM concepts").fetchone()[0] == "Legacy"
        assert conn.execute("SELECT COUNT(*) FROM graph_extraction_runs").fetchone()[0] == 0


def test_incremental_resume_refuses_rebuild_history_after_finalize(env):
    _, db = env
    first = extraction.extract_pending(max_chunks=1, progress=lambda _: None)
    with graph_connection(db) as conn:
        row = conn.execute(
            "SELECT options_json FROM graph_extraction_runs WHERE run_id = ?", (first["run_id"],)
        ).fetchone()
        options = {**json.loads(row[0]), "mode": "rebuild"}
        conn.execute(
            "UPDATE graph_extraction_runs SET options_json = ? WHERE run_id = ?",
            (json.dumps(options), first["run_id"]),
        )
    # No active rebuild flag remains; the persisted mode itself must enforce the boundary.
    with pytest.raises(ValueError, match="정상 증분 추출 실행만"):
        extraction.extract_pending(resume=first["run_id"])


def test_generation_discovery_excludes_embedding_model(monkeypatch):
    calls = []

    def request(endpoint, path, *, payload=None, **kwargs):
        calls.append((path, payload))
        if path == "/api/tags":
            return {"models": [{"name": "embed:1", "digest": "e"}, {"name": "chat:1", "digest": "c"}]}
        return {"capabilities": ["embedding"] if payload["model"].startswith("embed") else ["completion", "vision"]}

    monkeypatch.setattr(ollama, "request_json", request)
    assert [m["generation"] for m in ollama.models("http://windows")] == [False, True]
    with pytest.raises(ValueError, match="completion"):
        ollama.generation_model("http://windows", "embed:1")
    assert ollama.generation_model("http://windows", "chat:1")["digest"] == "c"
    assert any(path == "/api/show" for path, _ in calls)


def test_pg_storing_data_is_not_part_of_data():
    chunk = {"doc_id": "x", "chunk_index": 0, "content": "PostgreSQL은 영속 데이터를 저장한다."}
    batch = ExtractedBatch(
        items=[
            ExtractedItem(
                doc_id="x",
                chunk_index=0,
                concepts=[
                    ExtractedConcept(name="PostgreSQL", kind="software"),
                    ExtractedConcept(name="영속 데이터", kind="field"),
                ],
                relations=[ExtractedRelation(src="PostgreSQL", dst="영속 데이터", type="part_of", confidence=0.9)],
            )
        ]
    )
    assert json.loads(_storage_payload(batch, [chunk]))["items"][0]["relations"] == []
    chunk["content"] = "PostgreSQL은 데이터 플랫폼의 구성 요소다."
    batch.items[0].concepts[1] = ExtractedConcept(name="데이터 플랫폼", kind="software")
    batch.items[0].relations[0].dst = "데이터 플랫폼"
    assert len(json.loads(_storage_payload(batch, [chunk]))["items"][0]["relations"]) == 1


def test_chat_uses_context_output_and_rejects_truncation(monkeypatch):
    from pkb.graph import rebuild

    captured = []

    def request(*args, payload, **kwargs):
        captured.append(payload)
        return {"done_reason": "length", "message": {"content": "{}"}}

    monkeypatch.setattr(rebuild, "request_json", request)
    with pytest.raises(ValueError, match="출력 토큰"):
        rebuild._ollama_extract(
            [], {}, model="chat:1", endpoint="http://windows", timeout=5, context_tokens=4096, output_tokens=512
        )
    assert captured[0]["options"]["num_ctx"] == 4096
    assert captured[0]["options"]["num_predict"] == 512
    assert captured[0]["format"] == ExtractedBatch.model_json_schema()


def test_cli_exposes_bounded_runner_and_model_flags(monkeypatch):
    from pkb.cli import app

    called = []
    monkeypatch.setattr(
        extraction, "extract_pending", lambda **kwargs: called.append(kwargs) or {"selection_complete": True}
    )
    result = CliRunner().invoke(
        app,
        [
            "graph",
            "extract",
            "--doc-id",
            "data/x.md",
            "--max-chunks",
            "2",
            "--context",
            "4096",
            "--output",
            "512",
            "--model",
            "chat:1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert called[0]["max_chunks"] == 2
    assert called[0]["context_tokens"] == 4096


def test_input_budget_counts_schema_hints_and_reserves_output(monkeypatch):
    import tiktoken

    from pkb.graph import rebuild

    messages = [{"role": "user", "content": "짧은 문서"}]
    schema = ExtractedBatch.model_json_schema()
    result = rebuild.input_budget(messages, schema, context_tokens=4096, output_tokens=512)
    assert result["input_with_margin"] + 512 <= 4096
    assert result["tokenizer_estimate"] > len(tiktoken.get_encoding("cl100k_base").encode("짧은 문서"))
    calls = []
    monkeypatch.setattr(rebuild, "request_json", lambda *a, **k: calls.append(k) or {})
    source = {"doc_id": "x", "chunk_index": 0, "content": "근거 문장입니다. " * 4000}
    with pytest.raises(ValueError, match="입력 예산 초과"):
        rebuild._ollama_extract(
            [source],
            {("x", 0): ["hint"]},
            model="chat:1",
            endpoint="http://x",
            timeout=1,
            context_tokens=4096,
            output_tokens=512,
        )
    assert calls == []  # no silently truncated HTTP request is sent
    with pytest.raises(ValueError, match="입력 예산 초과"):
        rebuild.input_budget(
            messages, {"description": "schema material " * 4000}, context_tokens=4096, output_tokens=512
        )


def test_actual_prompt_saturation_never_reaches_storage(monkeypatch):
    from pkb.graph import rebuild

    monkeypatch.setattr(
        rebuild,
        "request_json",
        lambda *a, **k: {
            "prompt_eval_count": 4000,
            "message": {"content": '{"items": []}'},
        },
    )
    with pytest.raises(ValueError, match="입력 절단 가능성"):
        rebuild._ollama_extract(
            [], {}, model="chat:1", endpoint="http://x", timeout=1, context_tokens=4096, output_tokens=512
        )


def test_model_context_limit_is_checked_before_selecting_source(env, monkeypatch):
    monkeypatch.setattr(extraction, "generation_model", lambda *a, **k: {"digest": "one", "context_limit": 2048})
    with pytest.raises(ValueError, match="모델 한도"):
        extraction.extract_pending(context_tokens=4096)
    assert extraction.runs() == []


def test_open_reader_does_not_block_runner_store_or_history(env, monkeypatch):
    import sqlite3

    _, db = env
    with graph_connection(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        for name in ("legacy-a", "legacy-b", "legacy-c"):
            conn.execute(
                "INSERT INTO concepts (name, slug, base_slug, created_at, updated_at) VALUES (?, ?, ?, 't', 't')",
                (name, name, name),
            )
    original = extraction._ollama_extract
    readers = []

    def generate_with_concurrent_reader(chunks, *args, **kwargs):
        conn = sqlite3.connect(db)
        cursor = conn.execute("SELECT * FROM concepts")
        cursor.fetchone()  # Leave the SELECT unfinished throughout store + history commits.
        readers.append((conn, cursor))
        return original(chunks, *args, **kwargs)

    monkeypatch.setattr(extraction, "_ollama_extract", generate_with_concurrent_reader)
    try:
        result = extraction.extract_pending(max_chunks=1, retries=0, progress=lambda _: None)
        record = extraction.runs(run_id=result["run_id"])[0]
        assert result["status"] == "completed"
        assert record["items"][0]["status"] == "stored"
        assert record["events"][-1]["status"] == "stored"
        # The old reader still holds its pre-write snapshot.
        assert len(readers[0][1].fetchall()) == 2
    finally:
        for conn, cursor in readers:
            cursor.close()
            conn.close()


def test_delete_journal_migrates_once_without_changing_existing_data(tmp_path):
    import sqlite3

    from pkb.graph.schema import SCHEMA_SQL

    db = str(tmp_path / "legacy.sqlite")
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_SQL)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        conn.execute(
            "INSERT INTO concepts (id, name, slug, base_slug, concept_type, created_at, updated_at) "
            "VALUES (1, 'Legacy', 'legacy', 'legacy', 'software', 'original', 'original')"
        )
        conn.execute("INSERT INTO concept_curation VALUES ('legacy', 'real', 'keep original prose', 'original')")
        conn.execute("INSERT INTO concept_mentions VALUES (1, 'data/legacy.md', 0, 'original section')")
        conn.execute("INSERT INTO extracted_chunks VALUES ('data/legacy.md', 0, 'original hash', 'input', 'original')")
        tables = ("concepts", "concept_curation", "concept_mentions", "extracted_chunks")
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    # Read-only query initialization does not migrate the operational journal.
    init_schema(db, normalize=False)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    init_schema(db)
    init_schema(db)  # Already-WAL startup is idempotent.
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before


def test_memory_database_keeps_memory_journal():
    from pkb.graph.schema import get_connection

    conn = get_connection(":memory:")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    finally:
        conn.close()
    # The write initialization itself must also accept an in-memory journal.
    init_schema(":memory:")
