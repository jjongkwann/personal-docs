"""MCP 도구 경계의 접근 제어: 도구 등급, 파일 도구, 관리자 도구, HTTP Bearer 인증."""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest
import yaml
from starlette.testclient import TestClient

from pkb import access, mcp_server

ALICE_TOKEN = "alice-secret-token"

ADMIN_TOOLS = {
    "convert_and_ingest", "sync_obsidian", "sync_corpus", "doctor",
    "graph_list_chunks", "graph_store_concepts", "graph_curate", "graph_merge",
    "graph_validate", "graph_classify",
}


def _fn(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "study").mkdir(parents=True)
    (root / "career").mkdir()
    (root / "study" / "rag.md").write_text("# RAG\nhybrid search", encoding="utf-8")
    (root / "career" / "cv.md").write_text("# CV\nSECRET-SALARY", encoding="utf-8")
    policy = {
        "version": 1,
        "default": {"read": ["group:staff"]},
        "principals": {
            "owner": {"admin": True},
            "alice": {"groups": ["staff"], "token_sha256": hashlib.sha256(ALICE_TOKEN.encode()).hexdigest()},
            "carol": {"groups": ["staff"]},
            "bob": {},
        },
        "rules": [
            {"path": "data/career", "read": ["user:alice"], "write": ["user:alice"]},
            {"path": "data/study", "read": ["*"]},
        ],
    }
    policy_path = tmp_path / "access.yml"
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    monkeypatch.setattr("pkb.config.settings.data_root", str(root))
    monkeypatch.setattr("pkb.config.settings.access_policy", str(policy_path))
    return root


def _as(principal_id):
    return access.acting_as(access.load_policy().principal(principal_id))


def test_every_tool_declares_an_access_capability():
    tools = mcp_server.mcp._tool_manager._tools
    capabilities = {name: getattr(tool.fn, "pkb_access", None) for name, tool in tools.items()}
    assert set(capabilities.values()) <= access.CAPABILITIES, capabilities
    assert {name for name, cap in capabilities.items() if cap == "admin"} == ADMIN_TOOLS


def test_denied_and_missing_files_look_the_same(corpus):
    with _as("bob"):
        denied = _fn(mcp_server.read_file)("data/career/cv.md")
        missing = _fn(mcp_server.read_file)("data/career/none.md")
        allowed = _fn(mcp_server.read_file)("data/study/rag.md")
    assert "SECRET" not in denied
    assert denied.replace("cv.md", "X") == missing.replace("none.md", "X")
    assert "AccessDeniedError" in denied
    assert "hybrid search" in allowed


def test_case_variant_path_cannot_skip_a_deny_rule(corpus):
    # carol은 default로 읽지만 data/career 규칙에서 거부된다. macOS는 data/CAREER로도 같은 파일을 연다.
    with _as("carol"):
        result = _fn(mcp_server.read_file)("data/CAREER/cv.md")
    assert "SECRET" not in result and "AccessDeniedError" in result


def test_list_files_hides_unreadable_entries(corpus):
    with _as("bob"):
        listing = _fn(mcp_server.list_files)("data")
        with pytest.raises(access.AccessDeniedError):
            _fn(mcp_server.list_files)("data/career")
    assert [entry["name"] for entry in listing["entries"]] == ["study"]
    with _as("alice"):
        names = {entry["name"] for entry in _fn(mcp_server.list_files)("data")["entries"]}
    assert names == {"career", "study"}


def test_get_file_and_write_file_are_checked(corpus):
    with _as("bob"):
        result = _fn(mcp_server.get_file)("data/career/cv.md")
        written = _fn(mcp_server.write_file)("data/career/new.md", "# x", dry_run=True)
    assert result.is_error and "SECRET" not in json.dumps(result.model_dump(mode="json"))
    assert "AccessDeniedError" in written
    with _as("alice"):
        preview = _fn(mcp_server.write_file)("data/career/new.md", "# x", dry_run=True, strict_policy=False)
    assert preview.startswith("쓰기 미리보기")


def test_admin_tools_reject_non_admins(corpus):
    with _as("alice"):
        assert "관리자 전용" in _fn(mcp_server.doctor)()
        assert "관리자 전용" in _fn(mcp_server.graph_merge)("a", '["b"]')


def test_whoami_reports_the_rules_that_grant_access(corpus):
    with _as("bob"):
        result = json.loads(_fn(mcp_server.whoami)())
    assert result == {
        "principal": "bob", "groups": [], "admin": False, "access_control": True,
        "policy_sha256": access.load_policy().sha256, "readable": ["data/study"], "writable": [],
    }


def test_http_requires_a_bearer_token_when_a_policy_is_set(corpus, monkeypatch):
    reloaded = importlib.reload(mcp_server)
    try:
        app = reloaded.mcp.streamable_http_app(host="testserver", **reloaded._HTTP_TRANSPORT_OPTIONS)
        call = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}, "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "pkb-test", "version": "1"},
            }},
        }
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": "whoami",
        }
        with TestClient(app) as client:
            anonymous = client.post("/mcp", headers=headers, json=call)
            wrong = client.post("/mcp", headers={**headers, "Authorization": "Bearer nope"}, json=call)
            ok = client.post("/mcp", headers={**headers, "Authorization": f"Bearer {ALICE_TOKEN}"}, json=call)
        assert anonymous.status_code == 401 and "resource_metadata" in anonymous.headers["www-authenticate"]
        assert wrong.status_code == 401
        assert ok.status_code == 200, ok.text
        text = ok.json()["result"]["content"][0]["text"]
        assert json.loads(text)["principal"] == "alice"
    finally:
        monkeypatch.setattr("pkb.config.settings.access_policy", "")
        importlib.reload(mcp_server)
