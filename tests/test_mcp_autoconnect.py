"""At start-up the app connects the MCP servers the user listed, in the background, so nobody has to press Connect."""

import json
import sys
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.mcp_client import McpManager

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


def notes_config(tmp_path, command=sys.executable):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"notes": {"command": command, "args": [SERVER], "env": {"NOTES_DIR": str(tmp_path)}}}}),
                   encoding="utf-8")
    return cfg


def status(client):
    return client.get("/api/mcp").json()


def test_servers_are_connected_at_start_up_without_pressing_connect(tmp_path, make_agent):
    agent, _, settings = make_agent(mcp=McpManager(notes_config(tmp_path)))
    app = create_app(settings, agent, autoconnect=True)
    app.state.mcp_thread.join(60)
    s = status(TestClient(app))
    assert s["connecting"] is False
    assert [(x["name"], x["status"]) for x in s["servers"]] == [("notes", "connected")]
    assert "read_note" in {t["name"] for t in s["servers"][0]["tools"]}


def test_nothing_is_started_unless_autoconnect_is_asked_for(tmp_path, make_agent):
    agent, _, settings = make_agent(mcp=McpManager(notes_config(tmp_path)))
    app = create_app(settings, agent)
    assert getattr(app.state, "mcp_thread", None) is None
    s = status(TestClient(app))
    assert s["connecting"] is False and s["servers"][0]["status"] == "not_connected"


def test_a_broken_server_does_not_stop_the_app_and_is_reported(tmp_path, make_agent):
    agent, _, settings = make_agent(mcp=McpManager(notes_config(tmp_path, command="no-such-program-xyz")))
    app = create_app(settings, agent, autoconnect=True)
    app.state.mcp_thread.join(60)
    c = TestClient(app)
    assert c.get("/api/health").status_code == 200
    s = status(c)
    assert s["servers"][0]["status"] == "error" and s["servers"][0]["error"] and s["connecting"] is False


def test_the_app_answers_while_still_connecting_and_says_so(tmp_path, make_agent):
    manager = McpManager(notes_config(tmp_path))
    release, real = threading.Event(), manager._discover

    async def slow(cfg):
        import asyncio
        while not release.is_set():
            await asyncio.sleep(0.02)
        return await real(cfg)

    manager._discover = slow
    agent, _, settings = make_agent(mcp=manager)
    app = create_app(settings, agent, autoconnect=True)
    c = TestClient(app)
    try:
        assert c.get("/api/health").status_code == 200  # start-up was not blocked
        assert status(c)["connecting"] is True
    finally:
        release.set()
        app.state.mcp_thread.join(60)
    assert status(c)["connecting"] is False and status(c)["servers"][0]["status"] == "connected"


def test_no_config_file_means_nothing_to_connect(make_agent):
    agent, _, settings = make_agent()  # points at a config file that does not exist
    app = create_app(settings, agent, autoconnect=True)
    app.state.mcp_thread.join(30)
    assert status(TestClient(app)) ["servers"] == []


def test_two_refreshes_never_run_at_the_same_time(tmp_path):
    manager = McpManager(notes_config(tmp_path))
    running, overlap, real = [0], [False], manager._discover

    async def tracked(cfg):
        import asyncio
        running[0] += 1
        overlap[0] = overlap[0] or running[0] > 1
        await asyncio.sleep(0.2)
        try:
            return await real(cfg)
        finally:
            running[0] -= 1

    manager._discover = tracked
    threads = [threading.Thread(target=manager.refresh) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(90) for t in threads]
    assert overlap[0] is False and manager.status()[0][0].status == "connected"
