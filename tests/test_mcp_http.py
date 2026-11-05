"""Loopback-only HTTP transport for MCP servers (such as the VS Code extension)."""

import json

import pytest

from app.mcp_client import McpError, McpManager, load_config
from fake_vscode_mcp import FakeVscodeServer


def write_config(tmp_path, servers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


@pytest.mark.parametrize("url", ["http://127.0.0.1:3000/mcp", "http://localhost:3000/mcp", "http://[::1]:3000/mcp"])
def test_loopback_urls_are_accepted(tmp_path, url):
    servers, problems = load_config(write_config(tmp_path, {"vscode": {"url": url}}))
    assert problems == [] and servers["vscode"].url == url


@pytest.mark.parametrize("url", [
    "http://192.168.1.5:3000/mcp", "http://10.0.0.2/mcp", "http://example.com/mcp", "https://evil.example/mcp",
    "http://127.0.0.1.evil.example/mcp", "http://localhost.evil.example/mcp", "ftp://127.0.0.1/mcp",
    "file:///C:/x", "http://user:pw@127.0.0.1:3000/mcp", "127.0.0.1:3000", "http:///mcp"])
def test_non_loopback_or_odd_urls_are_refused_with_a_reason(tmp_path, url):
    servers, problems = load_config(write_config(tmp_path, {"vscode": {"url": url}}))
    assert "vscode" not in servers and problems and "loopback" in problems[0].lower()


def test_a_server_needs_exactly_one_of_command_or_url(tmp_path):
    servers, problems = load_config(write_config(tmp_path, {"both": {"command": "x", "url": "http://127.0.0.1:1/mcp"},
                                                            "neither": {}}))
    assert servers == {} and len(problems) == 2


def test_discovery_and_calls_work_over_http(tmp_path):
    fake = FakeVscodeServer(tmp_path / "ws").start()
    try:
        manager = McpManager(write_config(tmp_path, {"vscode": {"url": fake.url}}))
        statuses, problems = manager.refresh()
        assert problems == [] and statuses[0].status == "connected", statuses[0].error
        names = {t.name for t in statuses[0].tools}
        assert {"create_file_code", "list_files_code"} <= names
        result = manager.call("mcp:vscode:create_file_code", {"path": "a.txt", "content": "hi"})
        assert result.ok and (tmp_path / "ws" / "a.txt").read_text() == "hi"
        assert manager.status()[0][0].command.startswith("http://127.0.0.1")
    finally:
        fake.stop()


def test_arguments_are_still_checked_against_the_schema_over_http(tmp_path):
    fake = FakeVscodeServer(tmp_path / "ws").start()
    try:
        manager = McpManager(write_config(tmp_path, {"vscode": {"url": fake.url}}))
        manager.refresh()
        with pytest.raises(McpError, match="schema"):
            manager.call("mcp:vscode:create_file_code", {"path": "a.txt"})  # content is required
        assert fake.calls == []
    finally:
        fake.stop()


def test_an_unreachable_http_server_is_reported_not_raised(tmp_path):
    manager = McpManager(write_config(tmp_path, {"vscode": {"url": "http://127.0.0.1:9/mcp"}}))
    statuses, _ = manager.refresh()
    assert statuses[0].status == "error" and statuses[0].error
