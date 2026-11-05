import json
import re

import pytest

from app.agent import Agent
from app.config import Settings
from app.db import Database
from app.tools import LaunchError
from app.providers.base import GenerateRequest, GenerateResponse, HealthStatus, ModelProvider, ProviderError

SAMPLE = b"""Homework 3
Answer in at most 300 words total.
1. Compute 12 * (3 + 4).
2. Explain why the sky is blue.
3. Write a Python function that reverses a string.
"""


def code_json(code="def reverse(s):\n    return s[::-1]\n", tests="assert reverse('abc') == 'cba'",
              explanation="It reverses the string with slicing."):
    return json.dumps({"code": code, "tests": tests, "explanation": explanation})


def default_responder(req: GenerateRequest) -> str:
    if "ONE question of an assessment" in req.system:
        return code_json()
    ids = re.findall(r"### (Q\d+)", req.prompt.split("Complete these parts")[-1])
    return "\n\n".join(f"### {i}\nAnswer for {i}." for i in dict.fromkeys(ids))


class FakeProvider(ModelProvider):
    name = "fake"
    model = "fake-1"

    def __init__(self, responder=default_responder, cloud=False, error=None):
        self.responder, self.is_cloud, self.error = responder, cloud, error
        self.prompts: list[str] = []

    def generate(self, request):
        self.prompts.append(request.prompt)
        if self.error:
            raise self.error
        return GenerateResponse(text=self.responder(request), model=self.model, provider=self.name)

    def health_check(self):
        return HealthStatus(ok=True, message="ok", models=[self.model])


class FakeLauncher:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def _do(self, name, arg):
        self.calls.append((name, arg))
        if self.fail:
            raise LaunchError("app not installed")
        return "ok"

    def open_vscode(self, folder, files):
        return self._do("vscode", (folder, list(files)))

    def open_document(self, path):
        return self._do("document", path)

    def open_mail(self, url):
        return self._do("mail", url)


@pytest.fixture
def make_agent(tmp_path):
    def _make(provider=None, cap=8, healthy=True, launcher=None, mcp=None, sandbox=None, searcher=None, page_reader=None):
        settings = Settings(database_url=f"sqlite:///{tmp_path / 't.db'}", max_agent_steps=cap,
                            workspace_dir=str(tmp_path / 'ws'), mcp_config=str(tmp_path / 'no_mcp.json'),
                            sandbox_docker=False)  # tests never depend on Docker being installed
        provider = provider or FakeProvider()
        status = HealthStatus(ok=healthy, message="ok" if healthy else "Ollama is not reachable.")
        agent = Agent(Database(settings.db_path, settings.secrets), settings,
                      provider_factory=lambda cfg: (provider if healthy else None, status), run_inline=True,
                      launcher=launcher or FakeLauncher(), mcp=mcp, sandbox=sandbox, searcher=searcher, page_reader=page_reader)
        return agent, provider, settings
    return _make


def prepare(agent, text=SAMPLE, tools=("model", "calculator", "code_runner"), filename="hw.txt"):
    state = agent.create_session(filename, text)
    names = {t.name for t in state.tool_requests}
    agent.set_tool_approvals(state.id, {t: True for t in tools if t in names})
    agent.generate_plan(state.id)
    agent.approve_plan(state.id)
    return state.id
