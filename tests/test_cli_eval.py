"""CLI eval migration and machine report output."""

import json

from typer.testing import CliRunner

from pkb.cli import app
from pkb.eval import format_report


def test_cli_eval_migration_preserves_source(tmp_path):
    source = tmp_path / "legacy.jsonl"
    source.write_text('{"query":"질문","doc_id":"data/a.md"}\n', encoding="utf-8")
    original = source.read_bytes()
    destination = tmp_path / "v2.jsonl"

    result = CliRunner().invoke(app, ["eval", "--gold", str(source), "--migrate-to", str(destination)])

    assert result.exit_code == 0, result.output
    assert source.read_bytes() == original
    assert json.loads(destination.read_text())["relevant"] == [{"doc_id": "data/a.md"}]
    assert "원본 보존" in result.output


def test_cli_eval_rejects_invalid_config_before_search(tmp_path, monkeypatch):
    gold = tmp_path / "gold.jsonl"
    gold.write_text(json.dumps({"version": 2, "query": "질문", "query_type": "fact",
                                "answerable": True, "relevant": [{"doc_id": "data/a.md"}]}) + "\n")
    config = tmp_path / "bad.json"
    config.write_text('{"bad": {"unknown_option": true}}')
    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    monkeypatch.setattr(
        "pkb.eval.hybrid_search",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("search should not run")),
    )
    output = tmp_path / "result.json"

    result = CliRunner().invoke(app, ["eval", "--gold", str(gold), "--configurations",
                                      str(config), "--output", str(output)])

    assert result.exit_code == 1
    assert "unknown search options" in result.output
    assert not output.exists()


def test_cli_eval_json_matches_report(tmp_path, monkeypatch):
    gold = tmp_path / "gold.jsonl"
    gold.write_text(json.dumps({"version": 2, "query": "질문", "query_type": "fact",
                                "answerable": True, "relevant": [{"doc_id": "data/a.md"}]}) + "\n")
    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    monkeypatch.setattr("pkb.eval.hybrid_search", lambda *a, **kw: [{"doc_id": "data/a.md", "chunk_index": 0}])
    output = tmp_path / "result.json"

    result = CliRunner().invoke(app, ["eval", "--gold", str(gold), "--output", str(output), "--top-k", "1"])

    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["schema_version"] == report["metric_cutoff"] == 1
    assert report["modes"]["operational"]["summary"]["mrr_at_k"] == 1
    assert format_report(report) in result.output
