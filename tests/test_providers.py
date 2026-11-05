"""Gemini and Groq adapters, key handling and cloud consent, all against mocked HTTP."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.keys import KeyStore
from app.main import create_app
from app.providers.base import GenerateRequest, ProviderError
from app.providers.gemini import GeminiProvider
from app.providers.groq import GroqProvider
from app.safety import redact

KEY = "AIzaSyTESTKEY1234567890abcdefghij"
GROQ_KEY = "gsk_TESTKEY1234567890abcdefghij"


def transport(handler):
    return httpx.MockTransport(handler)


def gemini(handler, key=KEY, model="gemini-test"):
    return GeminiProvider(key, model, transport=transport(handler), sleep=lambda s: None)


def groq(handler, key=GROQ_KEY, model="llama-test"):
    return GroqProvider(key, model, transport=transport(handler), sleep=lambda s: None)


GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "hello "}, {"text": "world"}]}}],
             "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3}}
GROQ_OK = {"choices": [{"message": {"content": "hi there"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}


# --- success paths --------------------------------------------------------------

def test_gemini_generate_success_and_request_shape():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, json.loads(req.content)
        return httpx.Response(200, json=GEMINI_OK)

    out = gemini(handler).generate(GenerateRequest(prompt="p", system="s", json_mode=True))
    assert out.text == "hello world" and out.provider == "gemini"
    assert out.usage.prompt_tokens == 11 and out.usage.completion_tokens == 3
    assert KEY not in seen["url"] and seen["headers"]["x-goog-api-key"] == KEY  # header, never the URL
    assert "gemini-test:generateContent" in seen["url"]
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "s"


def test_groq_generate_success_and_request_shape():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, json.loads(req.content)
        return httpx.Response(200, json=GROQ_OK)

    out = groq(handler).generate(GenerateRequest(prompt="p", system="s", json_mode=True))
    assert out.text == "hi there" and out.usage.completion_tokens == 2
    assert GROQ_KEY not in seen["url"] and seen["headers"]["authorization"] == f"Bearer {GROQ_KEY}"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"][0] == {"role": "system", "content": "s"}


def test_model_lists_and_health():
    gem = gemini(lambda r: httpx.Response(200, json={"models": [
        {"name": "models/gemini-test", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embed-x", "supportedGenerationMethods": ["embedContent"]}]}))
    assert gem.list_models() == ["gemini-test"] and gem.health_check().ok
    grq = groq(lambda r: httpx.Response(200, json={"data": [{"id": "llama-test"}, {"id": "whisper-large"}]}))
    assert grq.list_models() == ["llama-test"] and grq.health_check().ok
    missing = groq(lambda r: httpx.Response(200, json={"data": [{"id": "other"}]}))
    assert not missing.health_check().ok


# --- error handling ----------------------------------------------------------------

@pytest.mark.parametrize("make,key", [(gemini, KEY), (groq, GROQ_KEY)])
def test_http_errors_are_normalised_and_never_leak_the_key(make, key):
    cases = {401: "auth", 403: "auth", 402: "billing", 404: "model_missing", 429: "rate_limited", 500: "bad_response"}
    for status, kind in cases.items():
        body = {"error": {"message": f"boom with {key} inside"}}
        provider = make(lambda r, s=status, b=body: httpx.Response(s, json=b))
        with pytest.raises(ProviderError) as err:
            provider.generate(GenerateRequest(prompt="p"))
        assert err.value.kind == kind, (status, err.value.message)
        assert key not in err.value.message


@pytest.mark.parametrize("make", [gemini, groq])
def test_timeout_unreachable_and_malformed(make):
    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    def down(req):
        raise httpx.ConnectError("nope", request=req)

    for handler, kind in ((timeout, "timeout"), (down, "unreachable"),
                          (lambda r: httpx.Response(200, text="not json"), "bad_response"),
                          (lambda r: httpx.Response(200, json={"unexpected": True}), "bad_response")):
        with pytest.raises(ProviderError) as err:
            make(handler).generate(GenerateRequest(prompt="p"))
        assert err.value.kind == kind


def test_rate_limit_retries_once_then_succeeds_or_fails():
    calls, waits = [], []

    def flaky(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "2"}) if len(calls) == 1 else httpx.Response(200, json=GROQ_OK)

    provider = GroqProvider(GROQ_KEY, "m", transport=transport(flaky), sleep=waits.append)
    assert provider.generate(GenerateRequest(prompt="p")).text == "hi there"
    assert len(calls) == 2 and waits == [2.0]

    always = []
    provider = GroqProvider(GROQ_KEY, "m", transport=transport(lambda r: always.append(1) or httpx.Response(429)),
                            sleep=lambda s: None)
    with pytest.raises(ProviderError) as err:
        provider.generate(GenerateRequest(prompt="p"))
    assert err.value.kind == "rate_limited" and len(always) == 2  # one retry only


def test_missing_key_or_model_never_calls_the_network():
    def boom(req):
        raise AssertionError("network must not be touched")

    with pytest.raises(ProviderError) as err:
        gemini(boom, key="").generate(GenerateRequest(prompt="p"))
    assert err.value.kind == "auth"
    with pytest.raises(ProviderError) as err:
        groq(boom, model="").generate(GenerateRequest(prompt="p"))
    assert err.value.kind == "model_missing"


def test_gemini_blocked_answer_is_reported_not_crashed():
    provider = gemini(lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(ProviderError) as err:
        provider.generate(GenerateRequest(prompt="p"))
    assert "SAFETY" in err.value.message


# --- keys ------------------------------------------------------------------------------

def test_keystore_redaction_validation_and_env_keys():
    store = KeyStore(Settings(groq_api_key=GROQ_KEY))
    assert store.has("groq") and not store.has("gemini")
    store.set("gemini", KEY)
    assert store.get("gemini") == KEY
    assert KEY not in redact(f"failed calling with {KEY}") and GROQ_KEY not in redact(f"x {GROQ_KEY}")
    with pytest.raises(ValueError):
        store.set("gemini", "short")
    with pytest.raises(ValueError):
        store.set("ollama", "whatever-key-1234")
    store.set("gemini", "")
    assert not store.has("gemini")


@pytest.fixture
def api(make_agent):
    agent, _, settings = make_agent()
    return TestClient(create_app(settings, agent)), agent, agent.db


def test_key_endpoint_never_echoes_logs_or_stores_the_key(api, tmp_path):
    c, agent, db = api
    secret = "AIzaSyNEVERSHOWTHISKEY1234567890xx"
    r = c.put("/api/keys", json={"provider": "gemini", "key": secret})
    assert r.status_code == 200 and r.json()["keys"]["gemini"] is True
    assert secret not in r.text and secret not in c.get("/api/health").text
    assert c.put("/api/keys", json={"provider": "gemini", "key": "bad key"}).status_code == 400
    assert c.put("/api/keys", json={"provider": "ollama", "key": secret}).status_code == 400
    db.log_action("s1", "test", f"tried {secret}", f"result {secret}")
    raw = open(db.path, "rb").read()
    assert secret.encode() not in raw  # not in sqlite file (settings or log)


def test_switching_providers_and_cloud_provider_needs_consent(api):
    c, agent, db = api
    r = c.put("/api/settings", json={"llm_provider": "groq", "groq_model": "llama-test"})
    assert r.status_code == 200 and r.json()["config"]["llm_provider"] == "groq"
    assert r.json()["cloud"] is True
    assert c.put("/api/settings", json={"llm_provider": "ollama"}).json()["cloud"] is False
    assert c.put("/api/settings", json={"llm_provider": "mistral"}).status_code == 400


def test_provider_account_problems_stop_the_run_instead_of_burning_steps(make_agent):
    from app.models import SessionStatus
    from conftest import FakeProvider, prepare
    from app.providers.base import ProviderError
    for kind in ("auth", "billing", "rate_limited"):
        agent, _, _ = make_agent(provider=FakeProvider(error=ProviderError(kind, f"{kind} problem")))
        sid = prepare(agent, b"1. Write a Python function f() that returns 1.\n2. Write a Python function g() that returns 2.\n",
                      tools=("model", "code_runner"))
        state = agent.start_run(sid)
        assert state.status == SessionStatus.failed and f"{kind} problem" in state.message
        assert state.steps_used <= 1  # stopped at the first failure, did not retry each question


def test_gemini_model_list_keeps_text_models_only():
    names = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.1-pro-preview", "gemma-4-31b-it",
             "gemini-2.5-flash-image", "gemini-2.5-flash-preview-tts", "gemini-3.5-transcribe", "lyria-3-clip-preview",
             "gemini-robotics-er-2-preview", "gemini-2.5-computer-use-preview-10-2025", "nano-banana-pro-preview",
             "deep-research-pro-preview-12-2025", "antigravity-preview-latest", "gemini-omni-flash-preview",
             "gemini-3.1-flash-lite-image", "text-embedding-004"]
    body = {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in names]}
    kept = gemini(lambda r: httpx.Response(200, json=body)).list_models()
    assert kept == ["gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-flash-latest", "gemma-4-31b-it"]
