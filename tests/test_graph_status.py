"""Document status follows current hashes and exposes only permitted document progress."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from pkb import access, mcp_server
from pkb.graph import schema, status
from pkb.graph.extraction_history import add_items, start_run, update_items
from pkb.graph.schema import graph_connection
from pkb.graph.services import graph_input_hash, scan_pending_chunks

DOC = "data/public/a.md"


class ES:
    def __init__(self):
        self.sources = [dict(doc_id=DOC, chunk_index=i, content_hash=f"hash{i}", content="RAG 검색",
                             title="RAG", category="public", section_path="") for i in range(2)]

    def search(self, *, query, search_after=None, size, **kwargs):
        terms = query.get("term") or query["bool"]["must"][0].get("term", {})
        rows = [s for s in self.sources if not terms.get("doc_id") or s["doc_id"] == terms["doc_id"]]
        if "bool" in query:
            assert {"terms": {"import_extraction": ["unsupported", "empty"]}} in query["bool"]["must_not"]
            rows = [s for s in rows if s.get("import_extraction") not in {"unsupported", "empty"}]
        if search_after:
            rows = [s for s in rows if [s["doc_id"], s["chunk_index"]] > search_after]
        return {"hits": {"hits": [{"_source": s, "sort": [s["doc_id"], s["chunk_index"]]} for s in rows[:size]]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr(status.settings, "graph_db_path", db)
    monkeypatch.setattr(status.settings, "graph_auto_extract", True)
    es = ES()
    monkeypatch.setattr(status, "get_client", lambda: es)
    with graph_connection(db):
        pass
    return es, db


def heartbeat(db, *, state="idle", age=0, doc=None, **extra):
    record = {"status": state, "updated_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
              "current_doc_id": doc, **extra}
    with graph_connection(db) as conn:
        conn.execute("INSERT OR REPLACE INTO graph_meta VALUES ('graph_worker_status',?)", (json.dumps(record),))


def mark(db, source):
    with graph_connection(db) as conn:
        conn.execute("INSERT OR REPLACE INTO extracted_chunks VALUES (?,?,?,?,?)",
                     (DOC, source["chunk_index"], source["content_hash"], graph_input_hash(source), "now"))


def test_current_hash_counts_completion_and_requeues_metadata_change(env):
    es, db = env
    heartbeat(db)
    assert status.document_status(DOC)["status"] == "queued"
    mark(db, es.sources[0])
    partial = status.document_status(DOC)
    assert (partial["completed_chunks"], partial["pending_chunks"]) == (1, 1)
    mark(db, es.sources[1])
    assert status.document_status(DOC)["status"] == "completed"
    es.sources[1]["title"] = "Changed metadata"
    assert status.document_status(DOC)["status"] == "queued"


@pytest.mark.parametrize("query_fails", [False, True])
def test_status_reads_without_schema_writes_and_closes_connection(env, monkeypatch, query_fails):
    statements = []
    connections = []
    connect = sqlite3.connect

    def open_read_only(*args, **kwargs):
        conn = connect(*args, **kwargs)
        connections.append(conn)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO graph_meta VALUES ('status-write', 'forbidden')")
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(status.sqlite3, "connect", open_read_only)
    monkeypatch.setattr(schema, "init_schema", lambda *a, **kw: pytest.fail("status initialized schema"))
    if query_fails:
        def fail_query(*args, **kwargs):
            raise RuntimeError("search unavailable")

        monkeypatch.setattr(status, "scan_pending_chunks", fail_query)
        with pytest.raises(RuntimeError, match="search unavailable"):
            status.document_status(DOC)
    else:
        assert status.document_status(DOC)["status"] == "waiting"
        assert statements and all(sql.lstrip().startswith("SELECT") for sql in statements)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_worker_heartbeat_controls_live_status(env, monkeypatch):
    _, db = env
    assert status.document_status(DOC)["status"] == "waiting"
    heartbeat(db, state="running", doc=DOC)
    assert status.document_status(DOC)["status"] == "running"
    heartbeat(db, state="running", doc="data/private/other.md")
    assert status.document_status(DOC)["status"] == "queued"
    heartbeat(db, age=10000)
    assert status.document_status(DOC)["status"] == "waiting"
    monkeypatch.setattr(status.settings, "graph_auto_extract", False)
    assert status.document_status(DOC)["status"] == "disabled"


def test_retry_status_matches_current_version_and_does_not_leak_errors(env):
    es, db = env
    source = {**es.sources[0], "input_hash": graph_input_hash(es.sources[0])}
    with graph_connection(db) as conn:
        start_run(conn, run_id="one", endpoint="http://windows", model="m", digest="d", options={},
                  prompt_version="v", schema_version="v", prompt="", schema={}, pending=2)
        add_items(conn, "one", [source])
    update_items("one", [(DOC, 0)], "failed", error="Private endpoint detail")
    heartbeat(db, state="waiting", reason="retry_backoff")
    result = status.document_status(DOC)
    assert result["status"] == "retrying" and result["failed_chunks"] == 1
    assert "Private" not in json.dumps(result)
    es.sources[0]["content"] = "New content"
    assert status.document_status(DOC)["failed_chunks"] == 0


@pytest.mark.parametrize("extraction", ["unsupported", "empty"])
def test_metadata_only_imports_are_not_graph_work(env, extraction):
    es, db = env
    for source in es.sources:
        source["import_extraction"] = extraction
    assert status.document_status(DOC)["status"] == "not_applicable"
    with graph_connection(db) as conn:
        assert scan_pending_chunks(es, conn) == ([], 0)


def test_mcp_checks_document_permission_before_any_lookup(env, tmp_path, monkeypatch):
    policy = tmp_path / "access.yaml"
    policy.write_text(yaml.safe_dump({"version": 1, "principals": {"reader": {}},
                                     "rules": [{"path": "data/public", "read": ["*"]}]}))
    monkeypatch.setattr(status.settings, "access_policy", str(policy))
    monkeypatch.setattr(status, "get_client", lambda: pytest.fail("unauthorized ES lookup"))
    tool = getattr(mcp_server.graph_extraction_status, "fn", mcp_server.graph_extraction_status)
    with access.acting_as(access.load_policy().principal("reader")):
        result = tool("data/private/hidden.md")
    assert "오류" in result
    assert "graph_extraction_status" in mcp_server.CORE_TOOLS
