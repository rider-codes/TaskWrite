"""OpenAI and Anthropic adapters against mocked HTTP."""

import json

import httpx
import pytest

from app.providers.anthropic import AnthropicProvider
from app.providers.base import GenerateRequest, ProviderError
from app.providers.openai import OpenAIProvider, is_text_model

OKEY = "sk-proj-TESTKEY1234567890abcdefghij"
AKEY = "sk-ant-TESTKEY1234567890abcdefghij"


def openai(handler, key=OKEY, model="gpt-test"):
    return OpenAIProvider(key, model, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def anthropic(handler, key=AKEY, model="claude-test"):
    return AnthropicProvider(key, model, transport=httpx.MockTransport(handler), sleep=lambda s: None)


OPENAI_OK = {"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
ANTHROPIC_OK = {"content": [{"type": "text", "text": "hel"}, {"type": "text", "text": "lo"}],
                "usage": {"input_tokens": 6, "output_tokens": 3}}


def test_openai_request_shape_and_response():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), headers=req.headers, body=json.loads(req.content))
        return httpx.Response(200, json=OPENAI_OK)

    out = openai(handler).generate(GenerateRequest(prompt="p", system="s", json_mode=True, max_tokens=99))
    assert out.text == "hello" and out.usage.prompt_tokens == 5 and out.provider == "openai"
    assert OKEY not in seen["url"] and seen["headers"]["authorization"] == f"Bearer {OKEY}"
    body = seen["body"]
    assert body["response_format"] == {"type": "json_object"} and body["max_completion_tokens"] == 99
    assert body["messages"][0] == {"role": "system", "content": "s"} and body["temperature"] == 0.2
    # reasoning families do not accept a custom temperature
    openai(lambda r: (seen.update(body=json.loads(r.content)) or httpx.Response(200, json=OPENAI_OK)), model="o4-mini") \
        .generate(GenerateRequest(prompt="p"))
    assert "temperature" not in seen["body"]


def test_anthropic_request_shape_and_response():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), headers=req.headers, body=json.loads(req.content))
        return httpx.Response(200, json=ANTHROPIC_OK)

    out = anthropic(handler).generate(GenerateRequest(prompt="p", system="s", max_tokens=77, temperature=1.7))
    assert out.text == "hello" and out.usage.completion_tokens == 3 and out.provider == "anthropic"
    assert AKEY not in seen["url"] and seen["headers"]["x-api-key"] == AKEY and seen["headers"]["anthropic-version"]
    body = seen["body"]
    assert body["system"] == "s" and body["max_tokens"] == 77 and body["temperature"] == 1.0  # clamped to the API range
    assert body["messages"] == [{"role": "user", "content": "p"}]


def test_model_lists_filter_and_health():
    names = ["gpt-4o", "gpt-4o-mini", "o3", "gpt-image-1", "text-embedding-3-small", "whisper-1", "tts-1", "gpt-4o-audio-preview",
             "dall-e-3", "omni-moderation-latest", "gpt-4o-realtime-preview"]
    p = openai(lambda r: httpx.Response(200, json={"data": [{"id": n} for n in names]}), model="gpt-4o")
    assert p.list_models() == ["gpt-4o", "gpt-4o-mini", "o3"] and p.health_check().ok
    assert not is_text_model("gpt-image-1") and is_text_model("gpt-5-mini")
    a = anthropic(lambda r: httpx.Response(200, json={"data": [{"id": "claude-b"}, {"id": "claude-a"}]}), model="claude-a")
    assert a.list_models() == ["claude-a", "claude-b"] and a.health_check().ok
    assert not anthropic(lambda r: httpx.Response(200, json={"data": [{"id": "x"}]}), model="claude-a").health_check().ok


@pytest.mark.parametrize("make,key", [(openai, OKEY), (anthropic, AKEY)])
def test_errors_are_normalised_and_never_leak_the_key(make, key):
    for status, kind in {401: "auth", 403: "auth", 404: "model_missing", 429: "rate_limited", 500: "bad_response"}.items():
        body = {"error": {"message": f"nope {key}"}}
        with pytest.raises(ProviderError) as err:
            make(lambda r, s=status, b=body: httpx.Response(s, json=b)).generate(GenerateRequest(prompt="p"))
        assert err.value.kind == kind and key not in err.value.message


def test_out_of_credit_accounts_are_reported_as_billing_not_a_rate_limit():
    quota = {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}
    with pytest.raises(ProviderError) as err:
        openai(lambda r: httpx.Response(429, json=quota)).generate(GenerateRequest(prompt="p"))
    assert err.value.kind == "billing"
    low = {"error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the API"}}
    with pytest.raises(ProviderError) as err:
        anthropic(lambda r: httpx.Response(400, json=low)).generate(GenerateRequest(prompt="p"))
    assert err.value.kind == "billing"
    with pytest.raises(ProviderError) as err:
        anthropic(lambda r: httpx.Response(529, json={})).generate(GenerateRequest(prompt="p"))
    assert err.value.kind == "rate_limited"


@pytest.mark.parametrize("make", [openai, anthropic])
def test_missing_key_or_model_never_calls_the_network(make):
    def boom(req):
        raise AssertionError("network must not be touched")

    with pytest.raises(ProviderError) as err:
        make(boom, key="").generate(GenerateRequest(prompt="p"))
    assert err.value.kind == "auth"
    with pytest.raises(ProviderError) as err:
        make(boom, model="").generate(GenerateRequest(prompt="p"))
    assert err.value.kind == "model_missing"


def test_five_providers_are_selectable_and_keys_are_tracked(make_agent):
    from fastapi.testclient import TestClient
    from app.main import create_app

    agent, _, settings = make_agent()
    c = TestClient(create_app(settings, agent))
    health = c.get("/api/health").json()
    assert set(health["supported_providers"]) == {"ollama", "gemini", "groq", "openai", "anthropic"}
    assert set(health["keys"]) == {"gemini", "groq", "openai", "anthropic"}
    secret = "sk-ant-NEVERECHOTHISKEY1234567890xx"
    r = c.put("/api/keys", json={"provider": "anthropic", "key": secret})
    assert r.status_code == 200 and r.json()["keys"]["anthropic"] is True and secret not in r.text
    for provider in ("openai", "anthropic"):
        r = c.put("/api/settings", json={"llm_provider": provider, f"{provider}_model": "m1"})
        assert r.status_code == 200 and r.json()["config"]["llm_provider"] == provider and r.json()["cloud"] is True
        assert r.json()["active_model"] == "m1"
