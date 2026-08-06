"""MCP 2026-07-28 무세션 Streamable HTTP 전송 회귀 테스트."""

from __future__ import annotations

from starlette.testclient import TestClient

from pkb import mcp_server


def test_streamable_http_accepts_modern_and_legacy_clients_without_sessions():
    """2026 요청은 initialize 없이 동작하고, 구 클라이언트도 세션 ID 없이 호환된다."""
    app = mcp_server.mcp.streamable_http_app(
        host="testserver",
        **mcp_server._HTTP_TRANSPORT_OPTIONS,
    )

    with TestClient(app) as client:
        modern = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/list",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "pkb-test",
                            "version": "1",
                        },
                    }
                },
            },
        )

        assert modern.status_code == 200
        assert "mcp-session-id" not in modern.headers
        modern_result = modern.json()["result"]
        assert modern_result["resultType"] == "complete"
        assert "search_knowledge" in {tool["name"] for tool in modern_result["tools"]}

        legacy_initialize = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pkb-legacy-test", "version": "1"},
                },
            },
        )

        assert legacy_initialize.status_code == 200
        assert "mcp-session-id" not in legacy_initialize.headers
        assert legacy_initialize.json()["result"]["protocolVersion"] == "2025-11-25"

        legacy_list = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )

        assert legacy_list.status_code == 200
        assert "mcp-session-id" not in legacy_list.headers
        assert "search_knowledge" in {
            tool["name"] for tool in legacy_list.json()["result"]["tools"]
        }
