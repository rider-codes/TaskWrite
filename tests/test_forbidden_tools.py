"""Tools that must never run, even if approved: send, share, delete, shell and friends."""

import json

import pytest

from app.agent import AgentError
from app.executor import ToolGate
from app.mcp_client import McpError, McpManager
from app.safety import ToolNotApproved
from conftest import FakeProvider, prepare
from fake_vscode_mcp import FakeVscodeServer

CODE_DOC = b"1. Write a Python function double(n) that returns twice n.\n2. Write a Python function triple(n) that returns three times n.\n"
BAD = ["execute_shell_command_code", "send_gmail_message", "share_drive_file", "delete_file_code", "drive_delete",
       "trash_file", "remove_user", "set_drive_file_permissions", "manage_drive_access", "sendEmail", "run_shell",
       "deleteFile", "ShareDocument", "send", "delete"]
FINE = ["create_file_code", "list_files_code", "read_file_code", "draft_gmail_message", "create_doc", "read_note",
        "write_note", "get_doc_content", "search_docs"]


@pytest.mark.parametrize("name", BAD)
def test_dangerous_tool_names_are_forbidden(name):
    from app.safety import forbidden_reason
    assert forbidden_reason(name), name


@pytest.mark.parametrize("name", FINE)
def test_ordinary_tool_names_are_not_forbidden(name):
    from app.safety import forbidden_reason
    assert forbidden_reason(name) is None, name


def server_setup(tmp_path, make_agent, **fake_kwargs):
    fake = FakeVscodeServer(tmp_path / "ws", **fake_kwargs).start()
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"vscode": {"url": fake.url}}}), encoding="utf-8")
    manager = McpManager(cfg)
    manager.refresh()
    agent, provider, settings = make_agent(mcp=manager)
    return fake, manager, agent


def test_forbidden_tools_are_hidden_from_the_tool_list_and_cannot_be_ticked(tmp_path, make_agent):
    fake, manager, agent = server_setup(tmp_path, make_agent)
    try:
        state = agent.create_session("hw.txt", CODE_DOC)
        offered = {t.name for t in state.tool_requests}
        for bad in ("execute_shell_command_code", "send_gmail_message", "share_drive_file", "delete_file_code"):
            assert f"mcp:vscode:{bad}" not in offered
        with pytest.raises(AgentError) as err:
            agent.set_tool_approvals(state.id, {"mcp:vscode:execute_shell_command_code": True})
        assert "never" in err.value.message.lower() or "blocked" in err.value.message.lower()
    finally:
        fake.stop()


def test_the_gate_refuses_a_forbidden_tool_even_if_the_database_says_approved(tmp_path, make_agent):
    from app.models import ToolRequest
    fake, manager, agent = server_setup(tmp_path, make_agent)
    try:
        state = agent.create_session("hw.txt", CODE_DOC)
        state.tool_requests.append(ToolRequest(name="mcp:vscode:execute_shell_command_code", reason="forced", approved=True,
                                               group="vscode"))
        agent.db.save_session(state)
        with pytest.raises(ToolNotApproved) as err:
            ToolGate(agent.db, state.id).use("mcp:vscode:execute_shell_command_code", "run a shell command")
        assert "blocked" in str(err.value).lower()
        assert fake.calls == []
    finally:
        fake.stop()


def test_the_shell_tool_call_fails_and_never_reaches_the_server(tmp_path, make_agent):
    """The test requested for the VS Code extension: calling the shell tool must fail, and nothing may arrive."""
    fake, manager, agent = server_setup(tmp_path, make_agent)
    try:
        for bad in ("execute_shell_command_code", "send_gmail_message", "share_drive_file", "delete_file_code"):
            with pytest.raises(McpError) as err:
                manager.call(f"mcp:vscode:{bad}", {"command": "whoami", "to": "x@y.z", "body": "b", "file_id": "1",
                                                   "email": "x@y.z", "path": "a"})
            assert "never" in str(err.value).lower() or "blocked" in str(err.value).lower()
        assert fake.calls == []
    finally:
        fake.stop()


def test_a_plan_naming_a_forbidden_tool_is_rejected():
    from app.safety import PlanValidationError, validate_plan_payload
    raw = {"steps": [{"id": "s1", "kind": "retrieve_context", "objective": "x", "inputs": [],
                      "tools": ["mcp:vscode:execute_shell_command_code"], "expected_output": "y", "requirement_ids": []}]}
    with pytest.raises(PlanValidationError):
        validate_plan_payload(raw, {"mcp:vscode:execute_shell_command_code"})


def test_the_model_driven_research_step_cannot_use_publish_or_forbidden_tools(tmp_path, make_agent):
    fake, manager, agent = server_setup(tmp_path, make_agent)
    try:
        state = agent.create_session("hw.txt", CODE_DOC)
        agent.set_workspace(state.id, str(tmp_path / "ws"))
        agent.set_tool_approvals(state.id, {"model": True, "code_runner": True, "mcp:vscode:create_file_code": True})
        state = agent.generate_plan(state.id)
        research = [s for s in state.plan.steps if s.kind.value == "retrieve_context"]
        assert all("mcp:vscode:create_file_code" not in s.tools and "mcp:vscode:list_files_code" not in s.tools
                   for s in research)
        export = next(s for s in state.plan.steps if s.kind.value == "export_deliverable")
        assert "mcp:vscode:create_file_code" in export.tools
    finally:
        fake.stop()
