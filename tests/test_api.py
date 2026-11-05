import io

import docx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import SAMPLE


@pytest.fixture
def client(make_agent):
    agent, provider, settings = make_agent()
    return TestClient(create_app(settings, agent)), provider


def upload(client, data=SAMPLE, name="hw.txt"):
    return client.post("/api/sessions", files={"file": (name, data)})


def test_end_to_end_api_flow(client):
    c, provider = client
    session = upload(c).json()
    sid = session["id"]
    assert "text" not in session and session["text_preview"].startswith("Homework")
    assert c.put(f"/api/sessions/{sid}/tools", json={"approvals": {"model": True, "calculator": True}}).status_code == 200
    plan = c.post(f"/api/sessions/{sid}/plan").json()["plan"]
    assert plan["steps"]
    assert c.post(f"/api/sessions/{sid}/plan/approve").json()["status"] == "plan_approved"
    run = c.post(f"/api/sessions/{sid}/run", json={"mode": "model"})
    assert run.status_code == 200
    state = c.get(f"/api/sessions/{sid}").json()
    assert state["status"] == "completed" and "Answer for Q2" in state["deliverable"]
    assert any(e["kind"] == "model_call" for e in c.get(f"/api/sessions/{sid}/log").json())
    out = c.get(f"/api/sessions/{sid}/export", params={"format": "docx"})
    assert out.status_code == 200 and "attachment" in out.headers["content-disposition"]
    assert "Answer for Q1" in "\n".join(p.text for p in docx.Document(io.BytesIO(out.content)).paragraphs)
    assert c.get(f"/api/sessions/{sid}/export", params={"format": "pdf"}).status_code == 409
    assert any(s["id"] == sid for s in c.get("/api/sessions").json())


def test_error_paths(client):
    c, _ = client
    assert upload(c, b"x", "bad.exe").status_code == 400
    assert c.get("/api/sessions/nope").status_code == 404
    sid = upload(c).json()["id"]
    assert c.post(f"/api/sessions/{sid}/run", json={"mode": "model"}).status_code == 409
    assert c.put(f"/api/sessions/{sid}/tools", json={"approvals": {"shell": True}}).status_code == 400
    assert c.get(f"/api/sessions/{sid}/export").status_code == 409
    bad_plan = {"steps": [{"id": "a", "kind": "send_email", "objective": "x", "expected_output": "y"}]}
    assert c.put(f"/api/sessions/{sid}/plan", json=bad_plan).status_code == 422


def test_settings_validation_and_no_secrets_exposed(client):
    c, _ = client
    assert c.put("/api/settings", json={"llm_provider": "mistral"}).status_code == 400
    assert c.put("/api/settings", json={"ollama_base_url": "file:///etc"}).status_code == 400
    r = c.put("/api/settings", json={"ollama_base_url": "http://example.com:11434"})
    assert "not on this machine" in r.json()["warning"]
    assert "api_key" not in c.get("/api/health").text.lower()


def test_unavailable_model_returns_explicit_fallback_option(make_agent):
    agent, _, settings = make_agent(healthy=False)
    c = TestClient(create_app(settings, agent))
    sid = upload(c).json()["id"]
    c.put(f"/api/sessions/{sid}/tools", json={"approvals": {"model": True}})
    c.post(f"/api/sessions/{sid}/plan")
    c.post(f"/api/sessions/{sid}/plan/approve")
    r = c.post(f"/api/sessions/{sid}/run", json={"mode": "model"})
    assert r.status_code == 409 and r.json()["options"] == ["rule_based"]


def test_delete_a_chat_removes_it_and_its_log_but_not_a_running_one(make_agent):
    from app.models import SessionStatus
    agent, _, settings = make_agent()
    c = TestClient(create_app(settings, agent))
    sid = upload(c).json()["id"]
    assert any(s["id"] == sid for s in c.get("/api/sessions").json())
    assert c.delete(f"/api/sessions/{sid}").status_code == 200
    assert not any(s["id"] == sid for s in c.get("/api/sessions").json())
    assert c.get(f"/api/sessions/{sid}").status_code == 404 and c.get(f"/api/sessions/{sid}/log").status_code == 404
    assert agent.db.get_log(sid) == []  # the action log went with it
    assert c.delete(f"/api/sessions/{sid}").status_code == 404
    # a running chat cannot be deleted
    sid2 = upload(c).json()["id"]
    state = agent.db.load_session(sid2)
    state.status = SessionStatus.running
    agent.db.save_session(state)
    assert c.delete(f"/api/sessions/{sid2}").status_code == 409 and c.get(f"/api/sessions/{sid2}").status_code == 200
