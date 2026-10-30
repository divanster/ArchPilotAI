import asyncio
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel

from app.llm.agent_prompt import build_system_prompt
from app.mcp.server import create_server
from app.mcp.tools import ToolContext
from app.tools import Tool, ToolInputError, ToolRegistry, UnknownToolError, get_registry
from app.tools.registry import EXPOSED_TIERS, render_args
from tests.test_agent import (  # noqa: F401  (fixtures)
    action,
    clean_agent_state,
    finish,
    play,
    write,
)
from tests.test_agent import world as _world_fixture

world = _world_fixture  # re-exported so pytest finds the fixture here

READ_NAMES = {
    "list_projects",
    "get_requirements",
    "get_architecture",
    "get_plan",
    "get_patch",
    "get_workspace_tree",
    "get_impact",
    "get_orphans",
    "search_documents",
    "get_repo_summary",
    "find_symbol",
    "find_references",
    "get_file_dependants",
    "search_repository",
}
AGENT_NAMES = {
    "read_file",
    "list_files",
    "search_code",
    "get_requirements",
    "get_task",
    "get_impact",
    "search_documents",
    "get_repo_summary",
    "find_symbol",
    "find_references",
    "get_file_dependants",
    "search_repository",
    "edit_file",
    "write_file",
    "delete_file",
    "run_static_checks",
    "run_tests",
    "finish",
}


class Nothing(BaseModel):
    pass


def _tool(name: str, tier: Any, audience: Any) -> Tool:
    return Tool(name, f"{name} tool", tier, audience, Nothing, lambda _c, _a: {"ok": name})


def _with_approval() -> ToolRegistry:
    registry = ToolRegistry(get_registry().all())
    registry.register(_tool("approve_patch", "approval", "agent"))
    registry.register(_tool("approve_plan", "approval", "mcp"))
    return registry


def test_names_are_unique_per_audience() -> None:
    keys = [(t.audience, t.name) for t in get_registry().all()]
    assert len(keys) == len(set(keys))
    with pytest.raises(ValueError, match="Duplicate"):
        get_registry().register(get_registry().all()[0])


def test_each_audience_sees_the_expected_tools() -> None:
    registry = get_registry()
    assert {t.name for t in registry.exposed("mcp")} == READ_NAMES
    assert {t.name for t in registry.exposed("agent")} == AGENT_NAMES
    assert {t.tier for t in registry.exposed("mcp")} == {"read"}
    assert {t.tier for t in registry.exposed("agent")} <= {"read", "draft"}


def test_agent_read_and_draft_tiers() -> None:
    tiers = {t.name: t.tier for t in get_registry().exposed("agent")}
    assert tiers["read_file"] == "read" and tiers["get_impact"] == "read"
    assert tiers["write_file"] == "draft" and tiers["finish"] == "draft"


def test_mcp_server_uses_the_registry_and_stays_read_only() -> None:
    server = create_server(ToolContext(lambda: None))  # type: ignore[arg-type,return-value]
    listed = asyncio.run(server.list_tools())
    assert {t.name for t in listed} == READ_NAMES
    registry = get_registry()
    for tool in listed:
        assert registry.get("mcp", tool.name).tier == "read"
    by_name = {t.name: t for t in listed}
    assert by_name["get_requirements"].inputSchema["required"] == ["project_id"]
    assert set(by_name["search_documents"].inputSchema["properties"]) == {
        "project_id",
        "query",
        "top_k",
    }


def test_approval_tools_are_never_exposed() -> None:
    registry = _with_approval()
    assert "approval" not in set().union(*EXPOSED_TIERS.values())
    assert "approve_plan" not in {t.name for t in registry.exposed("mcp")}
    assert "approve_patch" not in {t.name for t in registry.exposed("agent")}
    server = create_server(ToolContext(lambda: None), registry)  # type: ignore[arg-type,return-value]
    assert "approve_plan" not in {t.name for t in asyncio.run(server.list_tools())}
    prompt = build_system_prompt(registry.exposed("agent"))
    assert "approve_patch" not in prompt
    for audience, name in (("mcp", "approve_plan"), ("agent", "approve_patch")):
        with pytest.raises(UnknownToolError):
            registry.get(audience, name)
        with pytest.raises(UnknownToolError):
            registry.call(audience, name, {}, None)


def test_tier_enforcement_by_audience() -> None:
    registry = get_registry()
    with pytest.raises(UnknownToolError):
        registry.get("mcp", "write_file")  # a draft tool is not an MCP tool
    with pytest.raises(UnknownToolError):
        registry.call("mcp", "finish", {}, None)
    registry.get("agent", "read_file")


def test_unknown_tool_error_is_short_and_clear() -> None:
    with pytest.raises(ToolInputError) as found:
        get_registry().call("mcp", "drop_database", {}, None)
    assert str(found.value) == "Unknown tool: drop_database"
    server = create_server(ToolContext(lambda: None))  # type: ignore[arg-type,return-value]
    with pytest.raises(ToolError, match="Unknown tool: drop_database"):
        asyncio.run(server.call_tool("drop_database", {}))


def test_invalid_arguments_do_not_leak_pydantic_text() -> None:
    with pytest.raises(ToolInputError) as found:
        get_registry().call("mcp", "get_requirements", {"project_id": 5, "bogus": 1}, None)
    message = str(found.value)
    assert message == "Invalid arguments for get_requirements: bogus, project_id"
    assert "pydantic" not in message and "validation error" not in message


def test_agent_prompt_lists_exactly_the_offered_tools() -> None:
    registry = get_registry()
    offered = [t for t in registry.exposed("agent") if not t.needs_execution]
    prompt = build_system_prompt(offered)
    listed = {line.split()[1] for line in prompt.splitlines() if line.startswith("- ")}
    listed &= AGENT_NAMES | READ_NAMES
    assert listed == AGENT_NAMES - {"run_tests"}
    assert '- write_file {"path": string, "content": string}' in prompt
    assert '- read_file {"path": string, "start_line"?: integer, "end_line"?: integer}' in prompt
    with_tests = build_system_prompt(registry.exposed("agent"))
    assert "- run_tests " in with_tests and "- run_tests " not in prompt


def test_a_new_registry_tool_reaches_the_prompt_without_editing_it() -> None:
    registry = ToolRegistry(get_registry().all())
    registry.register(
        Tool(
            "count_files",
            "Count draft files.",
            "read",
            "agent",
            Nothing,
            lambda _c, _a: {},
        )
    )
    prompt = build_system_prompt(registry.exposed("agent"))
    assert "- count_files {}: Count draft files." in prompt
    assert render_args(registry.get("agent", "count_files")) == "{}"


def test_run_panel_reports_tool_and_tier(client: Any, world: Any) -> None:
    run, _ = play(client, world, [action("list_files"), write("src/a.py"), finish()])
    assert [(s["tool"], s["tier"]) for s in run["steps"]] == [
        ("list_files", "read"),
        ("write_file", "draft"),
        ("finish", "draft"),
    ]
