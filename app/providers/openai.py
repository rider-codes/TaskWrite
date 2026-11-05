"""OpenAI adapter (Chat Completions). Cloud: needs the user's own API key with API credits.

A ChatGPT subscription does not include API access. Models come from the account's own list.
"""

from __future__ import annotations

import re

from .base import GenerateRequest, GenerateResponse, HealthStatus, ProviderError, Usage
from .http_base import HttpProvider

# Keep chat/text models; drop image, audio, embedding, moderation, realtime and similar families.
_TEXT = re.compile(r"^(gpt-|o\d|chatgpt-)", re.I)
_NOT_TEXT = ("image", "audio", "tts", "transcribe", "whisper", "embed", "moderation", "realtime", "search", "dall",
             "instruct", "codex", "computer-use", "davinci", "babbage")


def is_text_model(name: str) -> bool:
    lowered = name.lower()
    return bool(_TEXT.match(lowered)) and not any(word in lowered for word in _NOT_TEXT)


class OpenAIProvider(HttpProvider):
    name = "openai"
    label = "OpenAI"
    base_url = "https://api.openai.com/v1"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _parse(self, resp):  # OpenAI reports "insufficient_quota" as HTTP 429: that is billing, not a burst limit
        if resp.status_code == 429:
            try:
                code = str((resp.json().get("error") or {}).get("code", ""))
            except (ValueError, AttributeError):
                code = ""
            if code == "insufficient_quota":
                raise ProviderError("billing", "OpenAI: this account has no API credits left (insufficient_quota).")
        return super()._parse(resp)

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        if not self.model:
            raise ProviderError("model_missing", "No OpenAI model is selected.")
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        payload: dict[str, object] = {"model": self.model, "messages": messages,
                                      "max_completion_tokens": request.max_tokens}
        if not re.match(r"^(o\d|gpt-5)", self.model, re.I):  # reasoning families reject custom temperatures
            payload["temperature"] = request.temperature
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._request("POST", "/chat/completions", payload)
        try:
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("bad_response", "OpenAI returned an unexpected response.") from exc
        return GenerateResponse(text=text, model=self.model, provider=self.name,
                                usage=Usage(prompt_tokens=tokens_in, completion_tokens=tokens_out))

    def list_models(self) -> list[str]:
        data = self._request("GET", "/models")
        try:
            return sorted(m["id"] for m in data.get("data", []) if isinstance(m, dict) and is_text_model(m.get("id", "")))
        except (KeyError, TypeError) as exc:
            raise ProviderError("bad_response", "OpenAI returned an unexpected model list.") from exc

    def health_check(self) -> HealthStatus:
        try:
            return self._health_from(self.list_models())
        except ProviderError as exc:
            return HealthStatus(ok=False, message=exc.message)
