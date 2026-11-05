"""Groq adapter (OpenAI-compatible chat completions). Cloud: needs the user's own API key."""

from __future__ import annotations

from .base import GenerateRequest, GenerateResponse, HealthStatus, ProviderError, Usage
from .http_base import HttpProvider

SKIP_MODEL_WORDS = ("whisper", "tts", "guard", "embed")


class GroqProvider(HttpProvider):
    name = "groq"
    label = "Groq"
    base_url = "https://api.groq.com/openai/v1"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        if not self.model:
            raise ProviderError("model_missing", "No Groq model is selected.")
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        payload: dict[str, object] = {"model": self.model, "messages": messages,
                                      "temperature": request.temperature, "max_tokens": request.max_tokens}
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._request("POST", "/chat/completions", payload)
        try:
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("bad_response", "Groq returned an unexpected response.") from exc
        return GenerateResponse(text=text, model=self.model, provider=self.name,
                                usage=Usage(prompt_tokens=tokens_in, completion_tokens=tokens_out))

    def list_models(self) -> list[str]:
        data = self._request("GET", "/models")
        try:
            ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        except TypeError as exc:
            raise ProviderError("bad_response", "Groq returned an unexpected model list.") from exc
        return sorted(i for i in ids if not any(w in i.lower() for w in SKIP_MODEL_WORDS))

    def health_check(self) -> HealthStatus:
        try:
            return self._health_from(self.list_models())
        except ProviderError as exc:
            return HealthStatus(ok=False, message=exc.message)
