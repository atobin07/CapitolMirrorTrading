"""Pluggable LLM backend: local Ollama, or any OpenAI-compatible API.

OpenAI-compatible covers OpenRouter, Together, DeepInfra, Fireworks, Novita,
vLLM/TGI, and Ollama's own /v1 endpoint — so each client can point at the
cheapest provider that fits its content needs, with no code change, just env:

    LLM_BACKEND=openai
    LLM_BASE_URL=https://openrouter.ai/api/v1
    LLM_API_KEY=sk-...
    LLM_MODEL=<provider's model slug>

Both clients expose the same interface (chat / health / close) and raise
OllamaError on failure, so the rest of the bot is backend-agnostic.
"""
from __future__ import annotations

import logging

import httpx

from app.ollama_client import OllamaClient, OllamaError

log = logging.getLogger(__name__)

_OPENAI_BACKENDS = {"openai", "openai-compatible", "api", "openrouter"}


class OpenAICompatClient:
    """Talks to any OpenAI-compatible /chat/completions endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        temperature: float = 0.6,
        max_tokens: int = 512,
        timeout: int = 120,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, messages: list[dict]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        try:
            resp = await self._client.post(
                f"{self.base}/chat/completions", headers=self._headers, json=payload
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"LLM API returned {exc.response.status_code}: "
                f"{exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not reach LLM API at {self.base}: {exc}") from exc

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise OllamaError(f"Unexpected LLM API response: {str(data)[:300]}")
        content = (content or "").strip()
        if not content:
            raise OllamaError("LLM API returned an empty response.")
        return content

    async def health(self) -> bool:
        """A 200 from /models means the endpoint is reachable and the key works."""
        try:
            resp = await self._client.get(f"{self.base}/models", headers=self._headers)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200


def uses_openai(cfg) -> bool:
    return (cfg.llm_backend or "ollama").lower() in _OPENAI_BACKENDS


def backend_label(cfg) -> str:
    if uses_openai(cfg):
        model = cfg.llm_model or cfg.ollama_model
        return f"API · {model} @ {cfg.llm_base_url or '(no base url)'}"
    return f"Ollama · {cfg.ollama_model} @ {cfg.ollama_host}"


def build_llm(cfg):
    """Return the configured LLM client (Ollama or OpenAI-compatible)."""
    if uses_openai(cfg):
        return OpenAICompatClient(
            cfg.llm_base_url,
            cfg.llm_api_key,
            cfg.llm_model or cfg.ollama_model,
            temperature=cfg.ollama_temperature,
            max_tokens=cfg.llm_max_tokens,
            timeout=cfg.ollama_timeout,
        )
    return OllamaClient(
        cfg.ollama_host,
        cfg.ollama_model,
        temperature=cfg.ollama_temperature,
        num_ctx=cfg.ollama_num_ctx,
        timeout=cfg.ollama_timeout,
    )
