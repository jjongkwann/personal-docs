"""그래프 스키마의 레거시 데이터 마이그레이션 테스트."""

from pkb.graph.schema import get_connection, init_schema


def test_init_schema_removes_blank_slug_concepts_and_edges(tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    init_schema(db_path)
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM graph_meta WHERE key = 'invalid_slug_cleanup_v1'")
        valid = conn.execute(
            "INSERT INTO concepts "
            "(name, slug, mention_count, created_at, updated_at) "
            "VALUES ('RRF', 'rrf', 1, 'now', 'now')"
        ).lastrowid
        invalid = conn.execute(
            "INSERT INTO concepts "
            "(name, slug, mention_count, created_at, updated_at) "
            "VALUES ('${{...}}', '', 1, 'now', 'now')"
        ).lastrowid
        conn.execute(
            "INSERT INTO concept_edges (src_id, dst_id, relation) "
            "VALUES (?, ?, 'related_to')",
            (invalid, valid),
        )

    init_schema(db_path)

    with get_connection(db_path) as conn:
        assert conn.execute("SELECT 1 FROM concepts WHERE slug = ''").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM concept_edges").fetchone()[0] == 0
        assert conn.execute(
            "SELECT value FROM graph_meta WHERE key = 'invalid_slug_cleanup_v1'"
        ).fetchone()[0] == "complete"
