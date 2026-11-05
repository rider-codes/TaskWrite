from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError
from app.main import create_app
from app.models import EmailOpenRequest, SessionStatus
from app.tools import LaunchError, mailto_url, safe_filename, write_workspace_file
from conftest import code_json, FakeLauncher, FakeProvider, default_responder, prepare

CODE_DOC = b"1. Write a Python function that doubles a number.\n"
CODE_ANSWER = code_json("def double(n):\n    return n * 2\n", "assert double(2) == 4", "It doubles the number.")
ESSAY_DOC = (b"Write a short passage on renewable energy and email it to the teacher at "
             b"teacher@school.edu.\n1. Write a short passage about renewable energy.\n")


def test_tool_requests_follow_the_assignment(make_agent):
    agent, _, _ = make_agent()
    names = {t.name for t in agent.create_session("a.txt", ESSAY_DOC).tool_requests}
    assert {"word", "email"} <= names and "vscode" not in names
    names = {t.name for t in agent.create_session("b.txt", CODE_DOC).tool_requests}
    assert "vscode" in names and "email" not in names


def test_vscode_saves_files_and_opens_only_when_approved(make_agent):
    launcher = FakeLauncher()
    agent, _, _ = make_agent(provider=FakeProvider(responder=lambda r: CODE_ANSWER), launcher=launcher)
    sid = prepare(agent, CODE_DOC, tools=("model", "vscode"))
    state = agent.start_run(sid)
    folder = Path(state.workspace_dir) / "hw"
    assert (folder / "q1.py").read_text().startswith("def double")
    assert "from q1 import *" in (folder / "check_q1.py").read_text()
    assert "It doubles the number." in (folder / "EXPLANATIONS.md").read_text()
    assert [c[0] for c in launcher.calls] == ["vscode"]
    assert "cannot confirm" in " ".join(state.tool_results)

    launcher2 = FakeLauncher()
    agent2, _, _ = make_agent(provider=FakeProvider(responder=lambda r: CODE_ANSWER), launcher=launcher2)
    state2 = agent2.start_run(prepare(agent2, CODE_DOC, tools=("model",)))
    assert launcher2.calls == [] and state2.workspace_files == []


def test_launch_failure_is_reported_not_hidden(make_agent):
    launcher = FakeLauncher(fail=True)
    agent, _, _ = make_agent(provider=FakeProvider(responder=lambda r: CODE_ANSWER), launcher=launcher)
    state = agent.start_run(prepare(agent, CODE_DOC, tools=("model", "vscode")))
    assert state.status == SessionStatus.completed
    assert any("could not open the app" in r for r in state.tool_results)


def test_word_saves_docx_and_opens_it(make_agent):
    launcher = FakeLauncher()
    agent, _, _ = make_agent(launcher=launcher)
    state = agent.start_run(prepare(agent, ESSAY_DOC, tools=("model", "word")))
    docx_path = Path(state.workspace_files[0])
    assert docx_path.suffix == ".docx" and docx_path.stat().st_size > 0
    assert launcher.calls == [("document", docx_path)]


def test_email_is_only_drafted_by_the_run_and_never_sent(make_agent):
    launcher = FakeLauncher()
    agent, _, _ = make_agent(launcher=launcher)
    sid = prepare(agent, ESSAY_DOC, tools=("model", "word", "email"))
    state = agent.start_run(sid)
    assert state.email_draft.status == "prepared" and state.email_draft.to == "teacher@school.edu"
    assert all(c[0] != "mail" for c in launcher.calls)

    with pytest.raises(AgentError) as exc:  # no confirmation
        agent.open_email(sid, EmailOpenRequest(to="teacher@school.edu", subject="s", body="b"))
    assert exc.value.code == 400
    with pytest.raises(AgentError):  # invalid recipient
        agent.open_email(sid, EmailOpenRequest(to="not-an-address", subject="s", body="b", confirm=True))
    state = agent.open_email(sid, EmailOpenRequest(to="teacher@school.edu", subject="Hi", body="Body", confirm=True))
    kind, url = launcher.calls[-1]
    assert kind == "mail" and url.startswith("mailto:teacher%40school.edu") and state.email_draft.status == "opened"
    assert "cannot send it" in state.tool_results[-1]


def test_email_requires_tool_approval(make_agent):
    agent, _, _ = make_agent()
    sid = prepare(agent, ESSAY_DOC, tools=("model", "word"))
    state = agent.start_run(sid)
    assert state.email_draft is None
    with pytest.raises(AgentError) as exc:
        agent.open_email(sid, EmailOpenRequest(to="a@b.co", subject="s", body="b", confirm=True))
    assert exc.value.code == 403


def test_model_can_ask_the_user_and_resume_with_the_answer(make_agent):
    prompts = []

    def responder(req):
        prompts.append(req.prompt)
        if "Information the student has given you" not in req.prompt:
            return "NEED_INPUT: Which renewable source should the passage focus on?"
        return default_responder(req)

    agent, provider, _ = make_agent(provider=FakeProvider(responder=responder))
    sid = prepare(agent, b"1. Write a passage about energy.\n", tools=("model",))
    state = agent.start_run(sid)
    assert state.status == SessionStatus.awaiting_input
    assert "renewable source" in state.pending_question and "NEED_INPUT" not in state.deliverable
    with pytest.raises(AgentError):  # cannot run before answering
        agent.start_run(sid)
    agent.answer_question(sid, "Solar power")
    state = agent.start_run(sid)
    assert state.status == SessionStatus.completed and "Solar power" in prompts[-1]
    assert state.answers[0].answer == "Solar power"
    with pytest.raises(AgentError):
        agent.answer_question(sid, "again")


def test_questions_are_capped_so_the_model_cannot_loop(make_agent):
    asked = []

    def responder(req):
        asked.append(1)
        return "NEED_INPUT: more?" if "NEED_INPUT" in (req.system or "") else default_responder(req)

    agent, _, _ = make_agent(provider=FakeProvider(responder=responder), cap=30)
    sid = prepare(agent, b"1. Write a passage about energy.\n", tools=("model",))
    state = agent.start_run(sid)
    for _ in range(10):
        if state.status != SessionStatus.awaiting_input:
            break
        agent.answer_question(sid, "ok")
        state = agent.start_run(sid)
    assert state.status == SessionStatus.completed and len(state.answers) == 5


def test_workspace_paths_cannot_escape(tmp_path):
    assert safe_filename("../../evil.py", "f") == "evil.py"
    path = write_workspace_file(tmp_path / "ws", "../../evil.py", "x")
    assert path.parent == (tmp_path / "ws").resolve()
    url, truncated = mailto_url("a@b.co", "Subject here", "x" * 5000)
    assert truncated and len(url) < 6000 and " " not in url


def test_api_answer_and_email_endpoints(make_agent):
    agent, _, settings = make_agent()
    c = TestClient(create_app(settings, agent))
    sid = c.post("/api/sessions", files={"file": ("e.txt", ESSAY_DOC)}).json()["id"]
    c.put(f"/api/sessions/{sid}/tools", json={"approvals": {"model": True, "word": True, "email": True}})
    c.post(f"/api/sessions/{sid}/plan")
    c.post(f"/api/sessions/{sid}/plan/approve")
    state = c.post(f"/api/sessions/{sid}/run", json={"mode": "model"}).json()
    assert state["email_draft"]["to"] == "teacher@school.edu" and state["tool_results"]
    assert c.post(f"/api/sessions/{sid}/answer", json={"answer": "x"}).status_code == 409
    body = {"to": "teacher@school.edu", "subject": "S", "body": "B", "confirm": True}
    assert c.post(f"/api/sessions/{sid}/email/open", json=body).json()["email_draft"]["status"] == "opened"
