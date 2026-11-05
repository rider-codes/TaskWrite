"""Step 3a: creating the code files through the VS Code MCP server, with one approval card and a read-back."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError
from app.main import create_app
from app.mcp_client import McpManager
from app.models import SessionStatus
from conftest import FakeLauncher, FakeProvider

DOC = (b"Python quiz\n1. Write a Python function double(n) that returns twice n.\n"
       b"2. Write a Python function triple(n) that returns three times n.\n")
CREATE, LIST = "mcp:vscode:create_file_code", "mcp:vscode:list_files_code"
PATHS = ["homework/q1.py", "homework/check_q1.py", "homework/q2.py", "homework/check_q2.py", "homework/EXPLANATIONS.md"]


class Env:
    def __init__(self, tmp_path, make_agent, cap=30, server_root=None, launcher=None, **fake_kwargs):
        from fake_vscode_mcp import FakeVscodeServer
        self.ws = tmp_path / "ws"
        self.ws.mkdir(exist_ok=True)
        self.fake = FakeVscodeServer(server_root or self.ws, **fake_kwargs).start()
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"vscode": {"url": self.fake.url}}}), encoding="utf-8")
        self.manager = McpManager(cfg)
        self.manager.refresh()
        self.launcher = launcher or FakeLauncher()
        self.agent, _, self.settings = make_agent(provider=FakeProvider(), cap=cap, mcp=self.manager, launcher=self.launcher)

    def session(self, doc=DOC, tools=("model", "code_runner", CREATE), name="homework.txt"):
        state = self.agent.create_session(name, doc)
        self.agent.set_workspace(state.id, str(self.ws))
        offered = {t.name for t in state.tool_requests}
        self.agent.set_tool_approvals(state.id, {t: True for t in tools if t in offered})
        self.agent.generate_plan(state.id)
        self.agent.approve_plan(state.id)
        return state.id

    def run_to_card(self, **kw):
        sid = self.session(**kw)
        return sid, self.agent.start_run(sid)

    def approve_and_run(self, sid, item_ids=None, approve=True):
        state = self.agent.db.load_session(sid)
        ids = item_ids if item_ids is not None else [i.id for i in state.publish_items if i.checked]
        self.agent.confirm_publish(sid, approve, ids)
        return self.agent.start_run(sid)

    def stop(self):
        self.fake.stop()


@pytest.fixture
def env(tmp_path, make_agent):
    e = Env(tmp_path, make_agent)
    yield e
    e.stop()


# --- what is offered ------------------------------------------------------------------------------------

def test_only_the_create_tool_and_its_folder_check_are_offered_and_only_for_code(env):
    state = env.agent.create_session("hw.txt", DOC)
    offered = {t.name for t in state.tool_requests if t.group == "vscode"}
    assert offered == {CREATE, LIST}  # read/move/rename/copy/replace/delete/shell/send/share are never offered
    essay = env.agent.create_session("e.txt", b"1. Write an essay on bees.\n")
    assert not {t.name for t in essay.tool_requests if t.group == "vscode"}


def test_approving_the_create_tool_also_approves_its_read_only_folder_check(env):
    state = env.agent.create_session("hw.txt", DOC)
    state = env.agent.set_tool_approvals(state.id, {CREATE: True})
    assert {t.name: t.approved for t in state.tool_requests}[LIST] is True
    state = env.agent.set_tool_approvals(state.id, {CREATE: False})
    assert {t.name: t.approved for t in state.tool_requests}[LIST] is False


# --- one approval card, nothing sent before it --------------------------------------------------------------

def test_the_run_stops_at_one_approval_card_before_any_call(env):
    sid, state = env.run_to_card()
    assert state.status == SessionStatus.awaiting_confirmation and "approval" in state.message.lower()
    assert env.fake.calls == []  # nothing reached the server yet
    items = state.publish_items
    assert [i.path for i in items if i.role == "file"] == PATHS
    assert all(i.checked for i in items if i.role == "file")  # file ticks start ticked
    assert [i.role for i in items][0] == "check" and items[0].required
    first = next(i for i in items if i.path.endswith("q1.py"))
    assert first.size > 0 and len(first.sha256) == 64 and "def " in first.preview and first.server == "vscode"
    with pytest.raises(AgentError):
        env.agent.start_run(sid)  # cannot run past an undecided card


def test_default_ticks_for_each_kind_of_publish():
    from app.publish import DEFAULT_CHECKED
    assert DEFAULT_CHECKED == {"file": True, "doc": False, "mail": False}


def test_approving_creates_the_files_and_each_is_read_back(env):
    sid, state = env.run_to_card()
    before = state.steps_used
    sent = []  # what TaskWrite itself sends, recorded on the client side (the server fills in defaults)
    original = env.manager.call
    env.manager.call = lambda tool_id, args: (sent.append((tool_id, dict(args))), original(tool_id, args))[1]
    state = env.approve_and_run(sid)
    assert state.status == SessionStatus.completed
    assert env.fake.names_called() == ["list_files_code"] + ["create_file_code"] * 5
    created = [args for name, args in env.fake.calls if name == "create_file_code"]
    assert [a["path"] for a in created] == PATHS
    assert [t for t, _ in sent] == [LIST] + [CREATE] * 5
    assert all(set(args) == {"path", "content"} for t, args in sent if t == CREATE)  # never overwrite, never ignoreIfExists
    assert all(a["overwrite"] is False and a["ignoreIfExists"] is False for a in created)
    for path in PATHS:
        assert (env.ws / path).is_file()
    assert (env.ws / "homework/q1.py").read_text() == next(c for c in state.code_results if c.requirement_id == "q1").code.rstrip() + "\n"
    results = {r.path: r for r in state.publish_results}
    assert all(results[p].status == "created" and results[p].read_back for p in PATHS)
    assert state.steps_used - before == 6  # one check plus five files, each charged to the budget
    lines = " ".join(state.tool_results).lower()
    assert "created 5 of 5" in lines and "read each file back" in lines
    for banned in ("opened", "sent", "shared", "delivered"):
        assert banned not in lines
    assert set(map(str, (Path(p).relative_to(env.ws).as_posix() for p in state.workspace_files))) >= set(PATHS)


def test_declining_creates_nothing_and_says_so(env):
    sid, _ = env.run_to_card()
    state = env.approve_and_run(sid, approve=False)
    assert env.fake.calls == [] and not (env.ws / "homework").exists()
    assert {r.status for r in state.publish_results} == {"declined"}
    assert "nothing was created" in " ".join(state.tool_results).lower()


def test_unticked_files_are_not_created(env):
    sid, state = env.run_to_card()
    keep = [i.id for i in state.publish_items if i.path in ("homework/q1.py", "homework/check_q1.py")]
    state = env.approve_and_run(sid, item_ids=keep)
    created = [a["path"] for n, a in env.fake.calls if n == "create_file_code"]
    assert created == ["homework/q1.py", "homework/check_q1.py"]
    assert not (env.ws / "homework/q2.py").exists()
    statuses = {r.path: r.status for r in state.publish_results}
    assert statuses["homework/q2.py"] == "declined" and statuses["homework/q1.py"] == "created"


# --- never outside the workspace, never overwrite ---------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../x.py", "a/../../x.py", "..\\x.py", "/etc/x.py", "C:\\x.py", "C:/x.py", "\\\\server\\share\\x.py",
                                 "", " ", "a\x00b.py", ".", "a/./../../b", "x" * 300 + ".py", "dir/", "CON", "a/b/../../../c"])
def test_unsafe_relative_paths_are_rejected(bad):
    from app.publish import PublishError, safe_relative_path
    with pytest.raises(PublishError):
        safe_relative_path(bad)


@pytest.mark.parametrize("good,expected", [("homework/q1.py", "homework/q1.py"), ("a\\b.py", "a/b.py"), ("a/./b.py", "a/b.py"),
                                           ("sub/../ok.py", "ok.py")])
def test_safe_relative_paths_are_normalised(good, expected):
    from app.publish import safe_relative_path
    assert safe_relative_path(good) == expected


def test_a_tampered_path_is_refused_and_nothing_escapes(env, tmp_path):
    sid, state = env.run_to_card()
    state.publish_items[3].path = "../escaped.py"
    env.agent.db.save_session(state)
    state = env.approve_and_run(sid)
    assert not (tmp_path / "escaped.py").exists() and not list(tmp_path.glob("**/escaped.py"))
    assert "../escaped.py" not in [a["path"] for n, a in env.fake.calls if n == "create_file_code"]
    bad = next(r for r in state.publish_results if r.item_id == state.publish_items[3].id)
    assert bad.status == "refused"


def test_changed_content_after_approval_is_refused(env):
    sid, state = env.run_to_card()
    state.publish_items[1].args_hash = "0" * 64
    env.agent.db.save_session(state)
    state = env.approve_and_run(sid)
    refused = [r for r in state.publish_results if r.status == "refused"]
    assert len(refused) == 1 and "changed" in refused[0].detail.lower()
    assert len([1 for n, _ in env.fake.calls if n == "create_file_code"]) == 4


def test_existing_files_are_never_overwritten(env):
    (env.ws / "homework").mkdir()
    (env.ws / "homework/q1.py").write_text("KEEP ME", encoding="utf-8")
    sid, _ = env.run_to_card()
    state = env.approve_and_run(sid)
    assert (env.ws / "homework/q1.py").read_text() == "KEEP ME"
    assert "homework/q1.py" not in [a["path"] for n, a in env.fake.calls if n == "create_file_code"]  # not even attempted
    assert all(a.get("overwrite") is not True for n, a in env.fake.calls)
    r = next(r for r in state.publish_results if r.path == "homework/q1.py")
    assert r.status == "exists" and "left unchanged" in r.detail
    assert (env.ws / "homework/q2.py").is_file()  # the others are still created


def test_the_wrong_open_folder_is_detected_before_anything_is_written(tmp_path, make_agent):
    other = tmp_path / "some_other_folder_open_in_vscode"
    e = Env(tmp_path, make_agent, server_root=other)
    try:
        sid, _ = e.run_to_card()
        state = e.approve_and_run(sid)
        assert "create_file_code" not in e.fake.names_called()
        assert not list(other.rglob("*.py")) and not (e.ws / "homework").exists()
        lines = " ".join(state.tool_results).lower()
        assert "open" in lines and "folder" in lines and "nothing was created" in lines
        assert not list(e.ws.glob(".taskwrite-probe-*"))  # the sentinel file was removed
    finally:
        e.stop()


# --- never trust the server's word ---------------------------------------------------------------------------------

def test_a_server_that_says_ok_but_writes_nothing_is_not_believed(tmp_path, make_agent):
    e = Env(tmp_path, make_agent, lie_about_writes=True)
    try:
        sid, _ = e.run_to_card()
        state = e.approve_and_run(sid)
        assert {r.status for r in state.publish_results if r.path} == {"not_found"}
        lines = " ".join(state.tool_results).lower()
        assert "created 0 of 5" in lines and "not found" in lines
    finally:
        e.stop()


def test_content_that_differs_from_what_was_sent_is_reported(tmp_path, make_agent):
    e = Env(tmp_path, make_agent, corrupt_writes=True)
    try:
        sid, _ = e.run_to_card()
        state = e.approve_and_run(sid)
        assert {r.status for r in state.publish_results if r.path} == {"mismatch"}
        assert "created 0 of 5" in " ".join(state.tool_results).lower()
    finally:
        e.stop()


# --- budget, resume, fallbacks --------------------------------------------------------------------------------------

def test_the_step_cap_is_never_exceeded_and_resuming_does_not_repeat_files(tmp_path, make_agent):
    e = Env(tmp_path, make_agent, cap=8)
    try:
        sid, state = e.run_to_card()
        state = e.approve_and_run(sid)
        assert state.steps_used <= state.step_cap and state.status == SessionStatus.paused_budget
        done_first = [a["path"] for n, a in e.fake.calls if n == "create_file_code"]
        assert 0 < len(done_first) < 5
        state = e.agent.continue_run(sid, extra_steps=10)
        assert state.status == SessionStatus.completed
        created = [a["path"] for n, a in e.fake.calls if n == "create_file_code"]
        assert sorted(created) == sorted(PATHS) and len(created) == 5  # each exactly once
        assert e.fake.names_called().count("list_files_code") == 1  # the folder check is not repeated
    finally:
        e.stop()


def test_an_unreachable_server_is_reported_and_nothing_falls_back_silently(tmp_path, make_agent):
    e = Env(tmp_path, make_agent, launcher=FakeLauncher())
    try:
        sid, _ = e.run_to_card(tools=("model", "code_runner", CREATE, "vscode"))
        e.stop()  # the extension goes away between approval and creation
        state = e.approve_and_run(sid)
        assert not (e.ws / "homework").exists()
        assert e.launcher.calls == []  # the old launcher was not used behind the user's back
        lines = " ".join(state.tool_results).lower()
        assert "could not" in lines and "nothing was created" in lines
    finally:
        e.stop()


def test_without_an_mcp_server_the_old_vscode_launcher_still_works(tmp_path, make_agent):
    launcher = FakeLauncher()
    agent, _, _ = make_agent(provider=FakeProvider(), cap=30, launcher=launcher)
    state = agent.create_session("homework.txt", DOC)
    agent.set_tool_approvals(state.id, {"model": True, "code_runner": True, "vscode": True})
    agent.generate_plan(state.id)
    agent.approve_plan(state.id)
    state = agent.start_run(state.id)
    assert state.status == SessionStatus.completed and [c[0] for c in launcher.calls] == ["vscode"]
    assert state.publish_items == []


# --- the API ------------------------------------------------------------------------------------------------------------------

def test_publish_endpoint(env):
    c = TestClient(create_app(env.settings, env.agent))
    sid, state = env.run_to_card()
    assert c.post(f"/api/sessions/{sid}/publish", json={"approve": True, "item_ids": ["nope"]}).status_code == 400
    ids = [i.id for i in state.publish_items]
    ok = c.post(f"/api/sessions/{sid}/publish", json={"approve": True, "item_ids": ids})
    assert ok.status_code == 200 and all(i["decision"] == "approved" for i in ok.json()["publish_items"] if i["id"] in ids)
    assert c.post(f"/api/sessions/{sid}/publish", json={"approve": True, "item_ids": ids}).status_code == 409  # already decided
    assert c.get(f"/api/sessions/{sid}").json()["publish_items"]
