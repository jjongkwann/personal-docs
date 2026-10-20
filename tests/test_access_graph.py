"""그래프 읽기는 읽을 수 있는 문서 근거만으로 다시 계산되고, 비공개 문서의 흔적이 새지 않는다."""

from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from pkb import access, mcp_server
from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema
from pkb.graph.visibility import graph_read_connection

PUBLIC = "data/public/a.md"
PRIVATE = "data/private/b.md"


def _fn(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture
def graph(tmp_path, monkeypatch):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    monkeypatch.setattr("pkb.embeddings.embed_queries", lambda texts: [[1.0, 0.0]])
    init_schema(db_path)
    with get_connection(db_path) as conn:
        shared = gstore.upsert_concept(conn, "Shared", "SECRET-DESCRIPTION", "rag", [1.0, 0.0])
        public = gstore.upsert_concept(conn, "Public", "public text", "rag", [0.9, 0.1], match_by_embedding=False)
        hidden = gstore.upsert_concept(conn, "HiddenOnly", "SECRET-HIDDEN", "rag", [0.8, 0.2],
                                       match_by_embedding=False)
        gstore.add_alias(conn, shared, "SECRET-ALIAS")
        gstore.upsert_document(conn, PUBLIC, "Public doc", "public")
        gstore.upsert_document(conn, PRIVATE, "SECRET-TITLE", "private")
        for concept, doc_id in ((shared, PUBLIC), (shared, PRIVATE), (public, PUBLIC), (hidden, PRIVATE)):
            gstore.add_mention(conn, concept, doc_id, 0, "")
        gstore.add_edge(conn, shared, public, "part_of", 0.9, doc_id=PUBLIC, chunk_index=0)
        gstore.add_edge(conn, shared, public, "part_of", 0.7, doc_id=PRIVATE, chunk_index=0)
        gstore.add_edge(conn, shared, hidden, "related_to", 0.9, doc_id=PRIVATE, chunk_index=0)
        # 비공개 문서만 근거인 전이 관계 — 추론 경로의 전제가 숨겨지면 추론도 나오면 안 된다
        gstore.add_edge(conn, public, hidden, "part_of", 0.9, doc_id=PRIVATE, chunk_index=0)
        gstore.recompute_mention_counts(conn, {shared, public, hidden})
        gstore.set_curation(conn, "shared", "real", prose="SECRET-PROSE")
    policy = tmp_path / "access.yml"
    policy.write_text(yaml.safe_dump({
        "version": 1,
        "principals": {"bob": {}, "owner": {"admin": True}},
        "rules": [{"path": "data/public", "read": ["*"]}],
    }), encoding="utf-8")
    monkeypatch.setattr("pkb.config.settings.access_policy", str(policy))
    return db_path


def _as(principal_id):
    return access.acting_as(access.load_policy().principal(principal_id))


def test_restricted_graph_recounts_from_readable_evidence(graph):
    with _as("bob"):
        explained = json.loads(_fn(mcp_server.graph_explain)("Shared"))
        hidden = _fn(mcp_server.graph_explain)("HiddenOnly")
    concept = explained["concept"]
    assert concept["description"] == "" and concept["aliases"] == [] and concept["prose"] == ""
    assert concept["mention_count"] == 1
    assert [edge["target"]["slug"] for edge in explained["outbound"]] == ["public"]
    edge = explained["outbound"][0]
    assert edge["evidence_count"] == 1 and edge["confidence"] == 0.9
    assert [ev["doc_id"] for ev in edge["evidence"]] == [PUBLIC]
    assert [m["doc_id"] for m in explained["mentions"]] == [PUBLIC]
    assert explained["inferred"] == []
    assert hidden == "오류: ValueError: Concept not found: HiddenOnly"


def test_nothing_from_the_private_document_reaches_any_graph_tool(graph):
    with _as("bob"):
        outputs = [
            _fn(mcp_server.graph_explain)("Shared"),
            _fn(mcp_server.graph_list_concepts)(),
            _fn(mcp_server.graph_query)("anything", depth=2),
            _fn(mcp_server.graph_affected)("Shared"),
            _fn(mcp_server.graph_map)(concept="Shared", depth=2),
            _fn(mcp_server.graph_inferred)("Shared", relation="part_of"),
            _fn(mcp_server.graph_inferred)("Public", relation="part_of", direction="up"),
        ]
        hidden_path = _fn(mcp_server.graph_path)("Shared", "HiddenOnly")
        missing_path = _fn(mcp_server.graph_path)("Shared", "Nowhere")
    joined = "\n".join(outputs)
    assert "SECRET" not in joined and "hiddenonly" not in joined.lower()
    # 숨긴 개념은 없는 개념과 같은 오류를 낸다 (입력한 이름만 되돌린다)
    assert hidden_path.lower().replace("hiddenonly", "X") == missing_path.lower().replace("nowhere", "X")
    with open(json.loads(outputs[-3])["path"], encoding="utf-8") as stream:
        rendered = stream.read()
    assert "SECRET" not in rendered and "HiddenOnly" not in rendered


def test_admin_and_unrestricted_reads_see_the_whole_graph(graph):
    with _as("owner"):
        explained = json.loads(_fn(mcp_server.graph_explain)("Shared"))
    assert explained["concept"]["description"] == "SECRET-DESCRIPTION"
    assert explained["concept"]["mention_count"] == 2
    assert {edge["target"]["slug"] for edge in explained["outbound"]} == {"public", "hiddenonly"}
    assert [(r["target"]["slug"], r["via"]) for r in explained["inferred"]] == [
        ("hiddenonly", ["shared", "public", "hiddenonly"])
    ]


def test_restricted_connection_cannot_write(graph):
    with _as("bob"), graph_read_connection(graph) as conn, pytest.raises(sqlite3.OperationalError, match="view"):
        conn.execute("DELETE FROM concepts")
