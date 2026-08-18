"""CLI ↔ MCP 표면 정합 가드 테스트.

introspection으로 실제 등록된 CLI 명령/MCP 도구 이름을 읽어 CAPABILITY 맵과 대조한다.
새 명령/도구가 추가됐는데 이 파일이 갱신되지 않으면 (b) 테스트가 실패해 drift를 막는다.
"""

from __future__ import annotations

from pkb.cli import app, graph_app


def cli_names() -> set[str]:
    names = [
        (c.name or c.callback.__name__.replace("_", "-")) for c in app.registered_commands
    ]
    names += [
        "graph " + (c.name or c.callback.__name__.replace("_", "-"))
        for c in graph_app.registered_commands
    ]
    return set(names)


def mcp_names() -> set[str]:
    import pkb.mcp_server as m

    return set(m.mcp._tool_manager._tools.keys())


# 능력 키 → {"cli": <cli 명령명>, "mcp": <mcp 도구명>}.
# reconcile의 mcp 대표는 sync_corpus (sync_obsidian은 ALLOWLIST_MCP에서 별도 커버).
CAPABILITY: dict[str, dict[str, str]] = {
    "search": {"cli": "query", "mcp": "search_knowledge"},
    "ingest_file": {"cli": "add", "mcp": "add_document"},
    "convert_ingest": {"cli": "convert", "mcp": "convert_and_ingest"},
    "list_docs": {"cli": "list", "mcp": "list_documents"},
    "get_doc": {"cli": "show", "mcp": "get_document"},
    "write_note": {"cli": "write", "mcp": "write_file"},
    "reindex_doc": {"cli": "reindex-doc", "mcp": "reindex_document"},
    "reconcile": {"cli": "sync", "mcp": "sync_corpus"},
    "archive": {"cli": "archive", "mcp": "archive_document"},
    "restore": {"cli": "restore", "mcp": "restore_document"},
    "doctor": {"cli": "doctor", "mcp": "doctor"},
    "graph_explain": {"cli": "graph explain", "mcp": "graph_explain"},
    "graph_path": {"cli": "graph path", "mcp": "graph_path"},
    "graph_query": {"cli": "graph query", "mcp": "graph_query"},
    "graph_affected": {"cli": "graph affected", "mcp": "graph_affected"},
    "graph_map": {"cli": "graph map", "mcp": "graph_map"},
}

# 비가역/무거운 admin·API 벌크 — CLI 전용이 의도된 것.
# "graph stats"는 doctor의 그래프 통계 섹션이 MCP 쪽을 이미 커버하므로 CLI 전용으로 남긴다.
# "eval"은 골드셋 전체를 4개 모드로 도는 벤치마크 하니스라 CLI 전용.
# "stale"은 SessionStart 훅용 신선도 점검(fail-open)이라 CLI 전용.
# "watch"는 폴링 데몬(포그라운드 상주 프로세스)이라 CLI 전용.
# "graph map"은 07-24에 CLI 전용으로 뒀었다("MCP 소비자는 JSON을 직접 쓴다"). 사람이
#   그래프를 눈으로 보고 싶을 때 MCP만 쓰는 사용자가 CLI로 나가야 하는 게 실제 병목이라
#   MCP에도 노출했다. --open(브라우저 실행)만 CLI에 남는다.
ALLOWLIST_CLI_ONLY = {
    "init",
    "reindex",
    "index-switch",
    "delete",
    "purge-archived",
    "eval",
    "graph stats",
    "graph reset-evidence",
    "graph finalize-evidence",
    "graph rebuild-evidence-local",
    "stale",
    "watch",
}

# Claude 루프 셀프추출·큐레이션·병합(graph_*) + obsidian 전용 재조정.
# "graph_list_concepts"는 추출 전 기존 어휘 재사용 유도용 — graph_list_chunks와 같은 Claude 루프 전용.
# "read_file"/"patch_file"은 원격 MCP에서 디스크 원문을 읽고 부분 편집하는 경로 — 로컬 CLI는
#   cat/sed로 충분하므로 CLI 대응 없음.
ALLOWLIST_MCP_ONLY = {
    "read_file",
    "patch_file",
    "graph_list_concepts",
    "graph_list_chunks",
    "graph_store_concepts",
    "graph_curate",
    "graph_merge",
    "sync_obsidian",
}


def test_capability_present_on_both_surfaces():
    """allowlist가 아닌 모든 능력은 cli/mcp 양쪽에 실제로 등록돼 있어야 한다."""
    cli = cli_names()
    mcp = mcp_names()
    missing = []
    for capability, surfaces in CAPABILITY.items():
        if surfaces["cli"] not in cli:
            missing.append(f"{capability}: cli '{surfaces['cli']}' 없음")
        if surfaces["mcp"] not in mcp:
            missing.append(f"{capability}: mcp '{surfaces['mcp']}' 없음")
    assert not missing, "\n".join(missing)


def test_no_unmapped_cli_commands():
    """등록된 모든 CLI 명령은 CAPABILITY 매핑값이거나 명시적 allowlist에 있어야 한다."""
    mapped = {v["cli"] for v in CAPABILITY.values()}
    allowed = mapped | ALLOWLIST_CLI_ONLY
    unmapped = cli_names() - allowed
    assert not unmapped, f"미매핑 CLI 명령: {unmapped} — CAPABILITY 또는 ALLOWLIST_CLI_ONLY에 추가 필요"


def test_no_unmapped_mcp_tools():
    """등록된 모든 MCP 도구는 CAPABILITY 매핑값이거나 명시적 allowlist에 있어야 한다."""
    mapped = {v["mcp"] for v in CAPABILITY.values()}
    allowed = mapped | ALLOWLIST_MCP_ONLY
    unmapped = mcp_names() - allowed
    assert not unmapped, f"미매핑 MCP 도구: {unmapped} — CAPABILITY 또는 ALLOWLIST_MCP_ONLY에 추가 필요"


def test_core_profile_exposes_only_core_tools(monkeypatch):
    """PKB_MCP_PROFILE=core는 CORE_TOOLS만 남기고, 기본값은 전량 유지한다."""
    import importlib

    import pkb.mcp_server as m

    monkeypatch.setenv("PKB_MCP_PROFILE", "core")
    core_mod = importlib.reload(m)
    try:
        exposed = set(core_mod.mcp._tool_manager._tools)
        assert exposed == set(core_mod.CORE_TOOLS)
        assert "graph_merge" not in exposed
    finally:
        monkeypatch.delenv("PKB_MCP_PROFILE", raising=False)
        full = importlib.reload(m)
    assert set(full.CORE_TOOLS) < set(full.mcp._tool_manager._tools)


def test_instructions_never_name_a_pruned_tool(monkeypatch):
    """instructions가 안내한 도구는 그 프로파일에 실제로 등록돼 있어야 한다.

    core로 줄이면서 graph_path/graph_affected 안내가 남으면 모델은 없는 도구를 부른다 —
    instructions는 MCPServer 생성 시점 고정이라 pruning과 별개로 틀어질 수 있다.
    """
    import importlib

    import pkb.mcp_server as m

    monkeypatch.delenv("PKB_MCP_PROFILE", raising=False)
    universe = set(importlib.reload(m).mcp._tool_manager._tools)

    monkeypatch.setenv("PKB_MCP_PROFILE", "core")
    try:
        core = importlib.reload(m)
        named = {tool for tool in universe if tool in core.mcp.instructions}
        registered = set(core.mcp._tool_manager._tools)
        assert named <= registered, f"core에 없는 도구를 안내함: {named - registered}"
        # 지도 안내가 빠지면 MCP만 쓰는 사용자는 그래프를 볼 방법이 없다.
        assert "graph_map" in named
    finally:
        monkeypatch.delenv("PKB_MCP_PROFILE", raising=False)
        importlib.reload(m)


def test_write_workflow_checks_paths_and_duplicates_before_creation():
    import pkb.mcp_server as m

    workflow = m._WRITE_WORKFLOW
    assert workflow.index("list_documents") < workflow.index("search_knowledge")
    assert workflow.index("search_knowledge") < workflow.index("get_document")
    assert "없을 때만" in workflow
    assert "canonical_group=False" in workflow
