"""Local Ollama adapter."""

from __future__ import annotations

import httpx

from .base import (GenerateRequest, GenerateResponse, HealthStatus, ModelProvider,
                   ProviderError, Usage)


class OllamaProvider(ModelProvider):
    name = "ollama"
    is_cloud = False

    def __init__(self, base_url: str, model: str, timeout_s: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        if not self.model:
            raise ProviderError("model_missing", "No Ollama model is selected.")
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        if request.json_mode:
            payload["format"] = "json"
        try:
            resp = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"Ollama did not respond within {self.timeout_s:g}s.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("unreachable", f"Could not reach Ollama at {self.base_url}.") from exc
        if resp.status_code == 404:
            raise ProviderError("model_missing", f"Model '{self.model}' is not installed in Ollama.")
        if resp.status_code != 200:
            raise ProviderError("bad_response", f"Ollama returned HTTP {resp.status_code}.")
        try:
            data = resp.json()
            text = data["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("bad_response", "Ollama returned an unexpected response.") from exc
        return GenerateResponse(
            text=text, model=self.model, provider=self.name,
            usage=Usage(prompt_tokens=data.get("prompt_eval_count", 0),
                        completion_tokens=data.get("eval_count", 0)),
        )

    def health_check(self) -> HealthStatus:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = sorted(m["name"] for m in resp.json().get("models", []))
        except (httpx.HTTPError, ValueError, KeyError):
            return HealthStatus(ok=False, message=f"Ollama is not reachable at {self.base_url}.")
        if not self.model:
            return HealthStatus(ok=False, models=models, message="Ollama is running; choose a model in settings.")
        if self.model not in models:
            return HealthStatus(ok=False, models=models,
                                message=f"Model '{self.model}' is not installed. Installed: {', '.join(models) or 'none'}.")
        return HealthStatus(ok=True, models=models, message=f"Ollama ready with '{self.model}'.")
