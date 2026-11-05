"""The model picker: switching provider or model is locked in the backend while a run is going, not just greyed out."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import SessionStatus
from conftest import SAMPLE


@pytest.fixture
def client(make_agent):
    agent, _, settings = make_agent()
    return TestClient(create_app(settings, agent)), agent


def start_run_state(c, agent, status):
    sid = c.post("/api/sessions", files={"file": ("hw.txt", SAMPLE)}).json()["id"]
    state = agent.db.load_session(sid)
    state.status = status
    agent.db.save_session(state)
    return sid


def current(c):
    return c.get("/api/health").json()["config"]


@pytest.mark.parametrize("body", [{"llm_provider": "groq"}, {"ollama_model": "other-model"}, {"groq_model": "x"},
                                  {"gemini_model": "x"}, {"openai_model": "x"}, {"anthropic_model": "x"},
                                  {"ollama_base_url": "http://localhost:11434"}])
def test_changing_the_model_is_refused_while_a_run_is_going(client, body):
    c, agent = client
    before = current(c)
    start_run_state(c, agent, SessionStatus.running)
    r = c.put("/api/settings", json=body)
    assert r.status_code == 409 and "in progress" in r.json()["detail"].lower()
    assert current(c) == before  # nothing was changed


def test_changing_the_model_works_when_nothing_is_running(client):
    c, agent = client
    start_run_state(c, agent, SessionStatus.awaiting_review)
    start_run_state(c, agent, SessionStatus.completed)
    assert c.put("/api/settings", json={"llm_provider": "groq", "groq_model": "m-1"}).status_code == 200
    assert current(c)["llm_provider"] == "groq"


def test_the_lock_ends_when_the_run_ends(client):
    c, agent = client
    sid = start_run_state(c, agent, SessionStatus.running)
    assert c.put("/api/settings", json={"llm_provider": "groq"}).status_code == 409
    state = agent.db.load_session(sid)
    state.status = SessionStatus.completed
    agent.db.save_session(state)
    assert c.put("/api/settings", json={"llm_provider": "groq"}).status_code == 200
