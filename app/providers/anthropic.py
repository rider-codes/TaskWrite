"""Anthropic adapter (Messages API). Cloud: needs the user's own API key with API credits.

An Anthropic or Claude app subscription does not include API access. Models come from the account's own list.
There is no JSON mode in this API, so structured output uses the shared parse-and-validate fallback.
"""

from __future__ import annotations

from .base import GenerateRequest, GenerateResponse, HealthStatus, ProviderError, Usage
from .http_base import HttpProvider

API_VERSION = "2023-06-01"


class AnthropicProvider(HttpProvider):
    name = "anthropic"
    label = "Anthropic"
    base_url = "https://api.anthropic.com/v1"

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}

    def _parse(self, resp):
        if resp.status_code == 400:  # an exhausted balance is reported as a 400 invalid_request_error
            try:
                message = str((resp.json().get("error") or {}).get("message", ""))
            except (ValueError, AttributeError):
                message = ""
            if "credit balance" in message.lower():
                raise ProviderError("billing", "Anthropic: the credit balance is too low to use the API. Add credits.")
        if resp.status_code == 529:
            raise ProviderError("rate_limited", "Anthropic is overloaded right now. Wait and retry, or switch provider.")
        return super()._parse(resp)

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        if not self.model:
            raise ProviderError("model_missing", "No Anthropic model is selected.")
        payload: dict[str, object] = {"model": self.model, "max_tokens": request.max_tokens,
                                      "temperature": min(max(request.temperature, 0.0), 1.0),
                                      "messages": [{"role": "user", "content": request.prompt}]}
        if request.system:
            payload["system"] = request.system
        data = self._request("POST", "/messages", payload)
        try:
            text = "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text")
            usage = data.get("usage") or {}
            tokens_in = int(usage.get("input_tokens", 0) or 0)
            tokens_out = int(usage.get("output_tokens", 0) or 0)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ProviderError("bad_response", "Anthropic returned an unexpected response.") from exc
        return GenerateResponse(text=text, model=self.model, provider=self.name,
                                usage=Usage(prompt_tokens=tokens_in, completion_tokens=tokens_out))

    def list_models(self) -> list[str]:
        data = self._request("GET", "/models?limit=100")
        try:
            return sorted(m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m)
        except (KeyError, TypeError) as exc:
            raise ProviderError("bad_response", "Anthropic returned an unexpected model list.") from exc

    def health_check(self) -> HealthStatus:
        try:
            return self._health_from(self.list_models())
        except ProviderError as exc:
            return HealthStatus(ok=False, message=exc.message)
