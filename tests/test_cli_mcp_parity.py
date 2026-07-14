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
    "concept_notes": {"cli": "graph sync-notes", "mcp": "sync_concept_notes"},
}

# 비가역/무거운 admin·API 벌크 — CLI 전용이 의도된 것.
# "graph stats"는 doctor의 그래프 통계 섹션이 MCP 쪽을 이미 커버하므로 CLI 전용으로 남긴다.
# "eval"은 골드셋 전체를 4개 모드로 도는 벤치마크 하니스라 CLI 전용.
# "stale"은 SessionStart 훅용 신선도 점검(fail-open)이라 CLI 전용.
# "watch"는 폴링 데몬(포그라운드 상주 프로세스)이라 CLI 전용.
ALLOWLIST_CLI_ONLY = {
    "init",
    "reindex",
    "delete",
    "purge-archived",
    "eval",
    "graph stats",
    "stale",
    "watch",
}

# Claude 루프 셀프추출·큐레이션·병합(graph_*) + obsidian 전용 재조정.
# "graph_list_concepts"는 추출 전 기존 어휘 재사용 유도용 — graph_list_chunks와 같은 Claude 루프 전용.
ALLOWLIST_MCP_ONLY = {
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
