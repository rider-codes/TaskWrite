"""The local server only answers requests addressed to this computer, and only accepts changes from its own pages.

Host check: stops DNS rebinding (an attacker's domain pointed at 127.0.0.1 could otherwise read your sessions).
Origin check: stops a page on another site from making your browser approve, run or delete things here.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from conftest import SAMPLE


@pytest.fixture
def make_client(make_agent):
    def _make(host="127.0.0.1:8000"):
        agent, _, settings = make_agent()
        return TestClient(create_app(settings, agent), base_url=f"http://{host}"), agent
    return _make


def sessions(agent):
    return len(agent.db.list_sessions())


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost", "LOCALHOST:8000"])
def test_requests_to_this_computer_are_answered(make_client, host):
    c, _ = make_client(host)
    assert c.get("/api/health").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "127.0.0.1.evil.example:8000", "192.168.1.20:8000"])
@pytest.mark.parametrize("path", ["/api/health", "/api/sessions", "/app", "/"])
def test_requests_with_another_host_are_refused_everywhere(make_client, host, path):
    c, _ = make_client("127.0.0.1:8000")
    r = c.get(path, headers={"host": host})
    assert r.status_code == 403 and "evil" not in r.text  # the attacker's host name is not echoed back


def test_a_change_from_another_site_is_refused_and_nothing_happens(make_client):
    c, agent = make_client("127.0.0.1:8000")
    r = c.post("/api/sessions", files={"file": ("hw.txt", SAMPLE)}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and sessions(agent) == 0
    sid = c.post("/api/sessions", files={"file": ("hw.txt", SAMPLE)}).json()["id"]
    for method, path, body in [("put", f"/api/sessions/{sid}/tools", {"approvals": {"model": True}}),
                               ("post", f"/api/sessions/{sid}/plan", None), ("delete", f"/api/sessions/{sid}", None),
                               ("put", "/api/settings", {"llm_provider": "groq"})]:
        r = c.request(method.upper(), path, json=body, headers={"origin": "https://evil.example"})
        assert r.status_code == 403, (method, path)
    state = agent.db.load_session(sid)
    assert not any(t.approved for t in state.tool_requests) and state.plan is None


def test_a_change_from_the_apps_own_page_is_accepted(make_client):
    c, agent = make_client("localhost:8000")
    r = c.post("/api/sessions", files={"file": ("hw.txt", SAMPLE)}, headers={"origin": "http://localhost:8000"})
    assert r.status_code == 200 and sessions(agent) == 1


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://127.0.0.1:9999", "null", "file://", "ftp://localhost:8000"])
def test_another_local_page_or_an_opaque_origin_cannot_make_changes(make_client, origin):
    c, agent = make_client("localhost:8000")
    r = c.post("/api/sessions", files={"file": ("hw.txt", SAMPLE)}, headers={"origin": origin})
    assert r.status_code == 403 and sessions(agent) == 0


def test_a_browser_cross_site_request_without_origin_is_refused(make_client):
    c, agent = make_client("127.0.0.1:8000")
    r = c.post("/api/sessions", files={"file": ("hw.txt", SAMPLE)}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403 and sessions(agent) == 0


def test_scripts_without_an_origin_still_work(make_client):
    c, agent = make_client("127.0.0.1:8000")
    assert c.post("/api/sessions", files={"file": ("hw.txt", SAMPLE)}).status_code == 200 and sessions(agent) == 1


def test_reading_from_another_origin_is_not_blocked_by_this_check_but_has_no_cors(make_client):
    c, _ = make_client("127.0.0.1:8000")
    r = c.get("/api/health", headers={"origin": "https://evil.example"})
    assert r.status_code == 200 and "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_more_hosts_can_be_allowed_in_the_environment(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "taskwrite.local, my-pc")
    hosts = Settings.from_env().allowed_hosts
    assert {"127.0.0.1", "localhost", "::1", "taskwrite.local", "my-pc"} <= set(hosts)
