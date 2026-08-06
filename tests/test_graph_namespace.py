"""Namespace-aware concept identity and alias collision safeguards."""

from __future__ import annotations

import json
import sqlite3

from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema


def test_schema_migration_adds_identity_columns_without_rewriting_legacy_rows(tmp_path):
    """An old v1 database remains readable and keeps its original slug/category."""
    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE concepts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            category TEXT,
            description TEXT,
            embedding BLOB,
            mention_count INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE concept_aliases (
            concept_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            alias_slug TEXT NOT NULL,
            PRIMARY KEY (concept_id, alias_slug)
        );
        CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO concepts(name, slug, category, created_at, updated_at)
        VALUES ('React', 'react', 'frontend', 'now', 'now');
        """
    )
    conn.commit()
    conn.close()

    init_schema(str(db_path))
    conn = get_connection(str(db_path))
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(concepts)")}
    row = conn.execute("SELECT * FROM concepts WHERE id = 1").fetchone()

    assert {"namespace", "base_slug"} <= columns
    assert row["slug"] == "react"
    assert row["category"] == "frontend"
    assert row["namespace"] == ""
    assert row["base_slug"] == "react"
    conn.close()


def test_same_normalized_name_is_scoped_when_categories_conflict(tmp_path):
    db_path = tmp_path / "graph.sqlite"
    init_schema(str(db_path))
    conn = get_connection(str(db_path))

    agent = gstore.upsert_concept(
        conn, "ReAct", category="ai", match_by_embedding=False
    )
    frontend = gstore.upsert_concept(
        conn, "React", category="frontend", match_by_embedding=False
    )
    same_agent = gstore.upsert_concept(
        conn, "ReAct", category="ai", match_by_embedding=False
    )

    assert same_agent == agent
    assert frontend != agent
    assert {
        row["namespace"] for row in conn.execute("SELECT * FROM concepts")
    } == {"ai", "frontend"}
    assert {
        row["base_slug"] for row in conn.execute("SELECT * FROM concepts")
    } == {"react"}

    # No category means ambiguous and must not return whichever row SQLite
    # happens to enumerate first.  Category context resolves both identities.
    assert gstore.get_concept(conn, "react") is None
    assert gstore.get_concept(conn, "react", category="ai")["id"] == agent
    assert gstore.get_concept(conn, "react", category="frontend")["id"] == frontend
    conn.close()


def test_alias_collision_is_rejected_and_reported(tmp_path):
    db_path = tmp_path / "graph.sqlite"
    init_schema(str(db_path))
    conn = get_connection(str(db_path))
    agent = gstore.upsert_concept(
        conn, "ReAct", category="ai", match_by_embedding=False
    )
    frontend = gstore.upsert_concept(
        conn, "React", category="frontend", match_by_embedding=False
    )

    assert gstore.add_alias(conn, frontend, "ReAct") is False
    assert gstore.list_aliases(conn, frontend) == []
    conflicts = gstore.alias_conflicts(conn, "react")
    assert conflicts
    assert conflicts[0]["concept_id"] == frontend
    assert conflicts[0]["existing_concept_id"] == agent
    assert conflicts[0]["reason"] == "canonical_or_alias_collision"
    conn.close()


def test_legacy_duplicate_aliases_are_not_silently_resolved(tmp_path):
    db_path = tmp_path / "graph.sqlite"
    init_schema(str(db_path))
    conn = get_connection(str(db_path))
    first = gstore.upsert_concept(conn, "First", match_by_embedding=False)
    second = gstore.upsert_concept(conn, "Second", match_by_embedding=False)
    # Simulate an old database that already contains a collision.  Migration is
    # intentionally non-destructive, so the read guard must handle it.
    conn.execute(
        "INSERT INTO concept_aliases(concept_id, alias, alias_slug) VALUES (?, ?, ?)",
        (first, "Shared", "shared"),
    )
    conn.execute(
        "INSERT INTO concept_aliases(concept_id, alias, alias_slug) VALUES (?, ?, ?)",
        (second, "Shared", "shared"),
    )
    assert gstore.find_concept_by_alias(conn, "shared") is None
    assert len(gstore.alias_conflicts(conn, "shared")) == 1
    conn.close()


def test_store_concepts_reports_scoped_alias_conflict(monkeypatch, tmp_path):
    from pkb.graph import services

    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    monkeypatch.setattr("pkb.embeddings.embed", lambda texts: [[0.0] for _ in texts])

    class _NoES:
        def mget(self, **kwargs):
            return {"docs": []}

    monkeypatch.setattr("pkb.store.get_client", lambda: _NoES())
    payload = {
        "items": [
            {
                "doc_id": "data/ai/agent.md",
                "chunk_index": 0,
                "category": "ai",
                "concepts": [{"name": "ReAct", "aliases": ["React"]}],
                "relations": [],
            },
            {
                "doc_id": "data/frontend/react.md",
                "chunk_index": 0,
                "category": "frontend",
                "concepts": [{"name": "React", "aliases": ["ReAct"]}],
                "relations": [],
            },
        ]
    }
    result = services.store_concepts(json.dumps(payload, ensure_ascii=False))

    assert "별칭 충돌 1건 제외" in result
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT name, namespace, base_slug FROM concepts ORDER BY id"
    ).fetchall()
    assert [(row["name"], row["namespace"], row["base_slug"]) for row in rows] == [
        ("ReAct", "ai", "react"),
        ("React", "frontend", "react"),
    ]
    conn.close()
