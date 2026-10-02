"""Search evidence formatting through shared renderer, CLI, and HTTP MCP."""

from starlette.testclient import TestClient
from typer.testing import CliRunner

from pkb import mcp_server
from pkb.cli import app
from pkb.context import _tokens, render_search_results


def _hits():
    return [
        {"doc_id": "data/study/a.md", "source_path": "/vault/a.md", "chunk_index": 2,
         "section_path": "같은 절", "content": "매칭 본문", "score": 0.9,
         "neighbors": [
             {"chunk_index": 1, "section_path": "같은 절", "content": "앞 문맥"},
             {"chunk_index": 4, "section_path": "같은 절", "content": "새 주변 문맥"},
             {"chunk_index": 3, "section_path": "다른 절", "content": "제외 문맥"},
         ]},
        {"doc_id": "data/study/a.md", "source_path": "/vault/a.md", "chunk_index": 1,
         "section_path": "같은 절", "content": "두 번째 매칭", "score": 0.8,
         "neighbors": [{"chunk_index": 2, "section_path": "같은 절", "content": "중복 문맥"}]},
    ]


def test_renderer_keeps_identity_orders_matches_and_deduplicates_neighbors():
    output = render_search_results(_hits())
    assert output.count("doc_id: data/study/a.md") == 3
    assert output.count("경로: /vault/a.md") == 3
    assert output.index("매칭 본문") < output.index("두 번째 매칭")
    assert output.index("두 번째 매칭") < output.index("새 주변 문맥")
    assert output.count("주변 문맥]") == 1
    assert "앞 문맥" not in output  # already a matched chunk
    assert "중복 문맥" not in output
    assert "제외 문맥" not in output


def test_renderer_budget_clips_korean_without_replacement_character():
    hits = [{"doc_id": "data/study/한국어.md", "chunk_index": 0,
             "content": "한국어 문장입니다. " * 1000, "score": 1.0}]
    output = render_search_results(hits, max_tokens=256)
    assert len(_tokens(output)) <= 256
    assert "�" not in output
    assert "doc_id: data/study/한국어.md" in output
    assert "일부 근거가 생략" in output


def test_cli_query_expand_uses_shared_renderer(monkeypatch):
    captured = {}
    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    monkeypatch.setattr("pkb.retrieve.hybrid_search", lambda *a, **kw: captured.update(kw) or [{
        "doc_id": "data/study/a.md", "chunk_index": 2, "section_path": "같은 절",
        "content": "매칭", "score": 1.0,
        "neighbors": [{"chunk_index": 1, "section_path": "같은 절", "content": "주변 문맥"}],
    }])
    result = CliRunner().invoke(app, ["query", "질문", "--expand", "1", "--context-tokens", "256"])
    assert result.exit_code == 0, result.output
    assert captured["expand_context"] == 1
    assert "doc_id: data/study/a.md" in result.output
    assert "주변 문맥" in result.output


def test_http_mcp_search_returns_neighbors_with_token_budget(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    monkeypatch.setattr("pkb.config.settings.graph_db_path", str(tmp_path / "absent.sqlite"))
    monkeypatch.setattr("pkb.retrieve.hybrid_search", lambda *a, **kw: captured.update(kw) or [{
        "doc_id": "data/study/a.md", "chunk_index": 2, "section_path": "같은 절",
        "content": "매칭", "score": 1.0,
        "neighbors": [{"chunk_index": 1, "section_path": "같은 절", "content": "HTTP 주변 문맥"}],
    }])
    app_http = mcp_server.mcp.streamable_http_app(
        host="testserver", **mcp_server._HTTP_TRANSPORT_OPTIONS,
    )
    with TestClient(app_http) as client:
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream",
                     "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
                     "Mcp-Name": "search_knowledge"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "search_knowledge", "arguments": {
                      "query": "질문", "expand_context": 1, "max_context_tokens": 256,
                  }, "_meta": {
                      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                      "io.modelcontextprotocol/clientCapabilities": {},
                      "io.modelcontextprotocol/clientInfo": {"name": "pkb-test", "version": "1"},
                  }}},
        )
    assert response.status_code == 200, response.text
    payload = response.json()["result"]
    assert captured["expand_context"] == 1
    text = "\n".join(item["text"] for item in payload["content"] if item["type"] == "text")
    assert "HTTP 주변 문맥" in text
    assert len(_tokens(text)) <= 256
