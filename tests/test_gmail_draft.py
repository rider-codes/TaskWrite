"""Step 3c: saving the finished answer as a Gmail draft through an MCP server. It is only ever a draft: nothing is sent."""

import json
import sys
from pathlib import Path

import pytest

from app.agent import AgentError
from app.mcp_client import McpError, McpManager, load_config
from app.models import SessionStatus
from conftest import FakeLauncher, FakeProvider

FAKE = str(Path(__file__).parent / "fake_gmail_mcp.py")
DRAFT, SEND = "mcp:gmail:draft_gmail_message", "mcp:gmail:send_gmail_message"
ESSAY = b"Bees essay\n1. Write a short essay on why bees matter to farming.\n"
CODE = (b"Python quiz\n1. Write a Python function double(n) that returns twice n.\n"
        b"2. Write a Python function triple(n) that returns three times n.\n")
SECRET = "s3cr3t-value-123"
ACCOUNT = "me@example.com"


class Env:
    def __init__(self, tmp_path, make_agent, monkeypatch, fail=False, extra_servers=None, account=True):
        self.log = tmp_path / "gmail.log"
        self.ws = tmp_path / "ws"
        self.ws.mkdir(exist_ok=True)
        monkeypatch.setenv("TW_TEST_SECRET", SECRET)
        monkeypatch.setenv("TW_TEST_LOG", str(self.log))
        monkeypatch.setenv("TW_TEST_EMAIL", ACCOUNT)
        env = {"FAKE_GMAIL_LOG": "${TW_TEST_LOG}", "GOOGLE_OAUTH_CLIENT_SECRET": "${TW_TEST_SECRET}",
               "FAKE_GMAIL_FAIL": "1" if fail else "0"}
        if account:
            env["USER_GOOGLE_EMAIL"] = "${TW_TEST_EMAIL}"
        servers = {"gmail": {"command": sys.executable, "args": [FAKE], "env": env}, **(extra_servers or {})}
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        self.manager = McpManager(cfg)
        self.status, self.problems = self.manager.refresh()
        self.agent, _, _ = make_agent(provider=FakeProvider(), cap=30, mcp=self.manager, launcher=FakeLauncher())

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def names_called(self):
        return [c["name"] for c in self.calls()]

    def session(self, doc=ESSAY, tools=("model", DRAFT), name="bees.txt"):
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


@pytest.fixture
def env(tmp_path, make_agent, monkeypatch):
    return Env(tmp_path, make_agent, monkeypatch)


# --- the config can read the Google client from .env without the secret living in the config file -----------------

def test_config_fills_in_environment_references(tmp_path, monkeypatch):
    monkeypatch.setenv("TW_ID", "id-123")
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"gmail": {"command": "x", "env": {"A": "${TW_ID}", "B": "plain", "C": "pre-${TW_ID}"}}}}))
    servers, problems = load_config(cfg)
    assert problems == [] and servers["gmail"].env == {"A": "id-123", "B": "plain", "C": "pre-id-123"}


def test_a_missing_reference_is_a_problem_and_the_server_is_not_started(tmp_path, monkeypatch):
    monkeypatch.delenv("TW_NOT_SET", raising=False)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"gmail": {"command": "x", "env": {"A": "${TW_NOT_SET}"}}}}))
    servers, problems = load_config(cfg)
    assert servers == {} and len(problems) == 1 and "TW_NOT_SET" in problems[0] and "gmail" in problems[0]


def test_the_google_secret_reaches_the_server_process_but_never_the_status_or_logs(env):
    server = next(s for s in env.status if s.name == "gmail")
    assert server.status == "connected"
    state = env.agent.create_session("b.txt", ESSAY)
    env.agent.set_tool_approvals(state.id, {"model": True, DRAFT: True})
    shown = json.dumps([s.model_dump() for s in env.manager.status()[0]]) + json.dumps(state.model_dump(mode="json"))
    assert SECRET not in shown
    sid, card = env.run_to_card()
    env.approve_and_run(sid, item_ids=["m1"])
    assert env.calls()[0]["secret_len"] == len(SECRET)  # the child process did get it


# --- what is offered: only the draft tool --------------------------------------------------------------------------

def test_only_the_draft_tool_is_offered_and_send_is_hidden(env):
    server = next(s for s in env.status if s.name == "gmail")
    assert "send_gmail_message" in server.hidden and "send_gmail_message" not in {t.name for t in server.tools}
    state = env.agent.create_session("b.txt", ESSAY)
    offered = {t.name for t in state.tool_requests if t.group == "gmail"}
    assert offered == {DRAFT}  # no mailbox reading, no labels, no sign-in tool
    code = env.agent.create_session("c.txt", CODE)
    assert {t.name for t in code.tool_requests if t.group == "gmail"} == {DRAFT}


def test_the_offer_explains_that_it_only_saves_a_draft(env):
    state = env.agent.create_session("b.txt", ESSAY)
    why = next(t.reason for t in state.tool_requests if t.name == DRAFT).lower()
    assert "draft" in why and "never sends" in why


def test_a_send_tool_can_never_be_called_even_if_it_is_approved(env):
    with pytest.raises(McpError, match="blocked"):
        env.manager.call(SEND, {"to": "a@b.com", "subject": "s", "body": "b"})
    state = env.agent.create_session("b.txt", ESSAY)
    with pytest.raises(AgentError):
        env.agent.set_tool_approvals(state.id, {SEND: True})
    assert env.names_called() == []  # nothing reached the server


def test_mailbox_reading_tools_are_not_callable_through_the_publish_path(env):
    from app.publish import is_mail_tool_id
    assert is_mail_tool_id(DRAFT)
    for name in ("search_gmail_messages", "get_gmail_message_content", "list_gmail_labels", "manage_gmail_label", "send_gmail_message"):
        assert not is_mail_tool_id(f"mcp:gmail:{name}")


# --- one approval card, unticked, nothing sent before it ------------------------------------------------------------

def test_the_run_stops_at_a_card_with_the_draft_unticked_and_nothing_is_called(env):
    sid, state = env.run_to_card()
    assert state.status == SessionStatus.awaiting_confirmation
    assert env.names_called() == []
    mail = [i for i in state.publish_items if i.role == "mail"]
    assert len(mail) == 1 and mail[0].checked is False and mail[0].required is False
    assert mail[0].server == "gmail" and mail[0].tool == DRAFT and len(mail[0].args_hash) == 64
    assert "Answer for Q1" in mail[0].preview
    with pytest.raises(AgentError):
        env.agent.start_run(sid)


def test_approving_the_card_without_ticking_the_draft_creates_nothing(env):
    sid, _ = env.run_to_card()
    state = env.approve_and_run(sid)  # default ticks: the draft starts unticked
    assert state.status == SessionStatus.completed
    assert env.names_called() == []
    assert next(r for r in state.publish_results if r.role == "mail").status == "declined"


def test_declining_the_card_creates_nothing(env):
    sid, _ = env.run_to_card()
    state = env.approve_and_run(sid, item_ids=["m1"], approve=False)
    assert env.names_called() == []
    assert next(r for r in state.publish_results if r.role == "mail").status == "declined"


def test_ticking_the_draft_saves_one_draft_with_the_essay_in_the_body_and_no_recipient(env):
    sid, card = env.run_to_card()
    state = env.approve_and_run(sid, item_ids=["m1"])
    assert env.names_called() == ["draft_gmail_message"]
    sent = env.calls()[0]["args"]
    assert sent["body"] == card.deliverable.rstrip() or sent["body"] == state.deliverable
    assert "Answer for Q1" in sent["body"]
    assert sent["subject"].startswith("Draft: bees") and sent["body_format"] == "plain"
    assert sent["to"] is None and sent["cc"] is None and sent["bcc"] is None  # TaskWrite never picks a recipient
    assert sent["user_google_email"] == ACCOUNT  # the account the server must save the draft in
    assert "send_gmail_message" not in env.names_called()


def test_the_result_is_honest_about_what_could_and_could_not_be_checked(env):
    sid, _ = env.run_to_card()
    state = env.approve_and_run(sid, item_ids=["m1"])
    result = next(r for r in state.publish_results if r.role == "mail")
    assert result.status == "created" and result.read_back is False  # no tool can read a draft back
    assert "r-1234567890" in result.detail and "could not" in result.detail.lower()
    said = " ".join(state.tool_results).lower()
    assert "draft" in said and "nothing was sent" in said
    assert "verified" not in said and "confirmed" not in said


def test_a_server_error_is_reported_as_failed_not_created(tmp_path, make_agent, monkeypatch):
    e = Env(tmp_path, make_agent, monkeypatch, fail=True)
    sid, _ = e.run_to_card()
    state = e.approve_and_run(sid, item_ids=["m1"])
    result = next(r for r in state.publish_results if r.role == "mail")
    assert result.status == "failed" and SECRET not in result.detail
    assert "Nothing was saved" in " ".join(state.tool_results) or "not saved" in " ".join(state.tool_results).lower()


def test_content_that_changes_after_approval_is_refused(env):
    sid, _ = env.run_to_card()
    env.agent.confirm_publish(sid, True, ["m1"])
    state = env.agent.db.load_session(sid)
    next(s for s in state.plan.steps if s.output.strip()).output += "\nAn extra line nobody approved."  # the deliverable is rebuilt from this
    env.agent.db.save_session(state)
    state = env.agent.start_run(sid)
    result = next(r for r in state.publish_results if r.role == "mail")
    assert result.status == "refused" and "changed" in result.detail
    assert env.names_called() == []


def test_the_draft_tool_is_called_only_when_it_was_approved_for_the_session(tmp_path, make_agent, monkeypatch):
    e = Env(tmp_path, make_agent, monkeypatch)
    sid, state = e.run_to_card(tools=("model",))  # offered but not approved
    assert state.status == SessionStatus.completed and not state.publish_items
    assert e.names_called() == []


def test_the_card_names_the_gmail_account_the_draft_will_be_saved_in(env):
    sid, state = env.run_to_card()
    mail = next(i for i in state.publish_items if i.role == "mail")
    assert ACCOUNT in mail.label


def test_without_a_configured_account_no_draft_is_offered_and_nothing_is_called(tmp_path, make_agent, monkeypatch):
    e = Env(tmp_path, make_agent, monkeypatch, account=False)  # the real server needs user_google_email
    sid, state = e.run_to_card()
    assert state.status == SessionStatus.completed and not state.publish_items
    assert "USER_GOOGLE_EMAIL" in " ".join(state.tool_results) and "Nothing was saved" in " ".join(state.tool_results)
    assert e.names_called() == []


def test_the_budget_counts_the_draft(env):
    sid, _ = env.run_to_card()
    before = env.agent.db.load_session(sid).steps_used
    state = env.approve_and_run(sid, item_ids=["m1"])
    assert state.steps_used == before + 1


# --- files and a draft on the same card ---------------------------------------------------------------------------

def test_code_files_and_the_draft_share_one_card_and_only_files_start_ticked(tmp_path, make_agent, monkeypatch):
    from fake_vscode_mcp import FakeVscodeServer
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeVscodeServer(ws).start()
    try:
        e = Env(tmp_path, make_agent, monkeypatch, extra_servers={"vscode": {"url": fake.url}})
        sid, state = e.run_to_card(doc=CODE, tools=("model", "code_runner", "mcp:vscode:create_file_code", DRAFT), name="homework.txt")
        assert state.status == SessionStatus.awaiting_confirmation
        roles = [(i.role, i.checked) for i in state.publish_items]
        assert ("check", True) in roles and ("mail", False) in roles and roles.count(("file", True)) == 5
        state = e.approve_and_run(sid)  # the default ticks: files yes, draft no
        assert state.status == SessionStatus.completed
        assert "create_file_code" in fake.names_called() and e.names_called() == []
    finally:
        fake.stop()
