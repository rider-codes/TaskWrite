"""Google Gemini adapter (REST generateContent). Cloud: needs the user's own API key.

The key travels in the x-goog-api-key header, never in the URL.
"""

from __future__ import annotations

from .base import GenerateRequest, GenerateResponse, HealthStatus, ProviderError, Usage
from .http_base import HttpProvider


# Models that can accept generateContent but are not for writing answers (images, speech, agents, robots...).
NON_TEXT_WORDS = ("image", "tts", "transcribe", "embed", "lyria", "robotics", "computer-use", "nano-banana",
                  "deep-research", "antigravity", "omni", "audio", "live", "veo", "imagen", "aqa", "-customtools",
                  "argon")


def is_text_model(name: str) -> bool:
    lowered = name.lower()
    return not any(word in lowered for word in NON_TEXT_WORDS)


class GeminiProvider(HttpProvider):
    name = "gemini"
    label = "Gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        if not self.model:
            raise ProviderError("model_missing", "No Gemini model is selected.")
        config: dict[str, object] = {"temperature": request.temperature, "maxOutputTokens": request.max_tokens}
        if request.json_mode:
            config["responseMimeType"] = "application/json"
        payload: dict[str, object] = {"contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
                                      "generationConfig": config}
        if request.system:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        data = self._request("POST", f"/models/{self.model.removeprefix('models/')}:generateContent", payload)
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            usage = data.get("usageMetadata") or {}
            tokens_in = int(usage.get("promptTokenCount", 0) or 0)
            tokens_out = int(usage.get("candidatesTokenCount", 0) or 0)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            feedback = data.get("promptFeedback")
            reason = str(feedback.get("blockReason", "")) if isinstance(feedback, dict) else ""
            raise ProviderError("bad_response", "Gemini returned no answer"
                                + (f" (blocked: {reason})." if reason else ".")) from exc
        return GenerateResponse(text=text, model=self.model, provider=self.name,
                                usage=Usage(prompt_tokens=tokens_in, completion_tokens=tokens_out))

    def list_models(self) -> list[str]:
        data = self._request("GET", "/models?pageSize=200")
        try:
            names = (m["name"].removeprefix("models/") for m in data.get("models", [])
                     if isinstance(m, dict) and "generateContent" in m.get("supportedGenerationMethods", []))
            return sorted(n for n in names if is_text_model(n))
        except (KeyError, TypeError) as exc:
            raise ProviderError("bad_response", "Gemini returned an unexpected model list.") from exc

    def health_check(self) -> HealthStatus:
        try:
            return self._health_from(self.list_models())
        except ProviderError as exc:
            return HealthStatus(ok=False, message=exc.message)
