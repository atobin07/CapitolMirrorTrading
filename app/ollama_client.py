"""Thin async wrapper around the Ollama chat API."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(
        self,
        host: str,
        model: str,
        *,
        temperature: float = 0.6,
        num_ctx: int = 4096,
        timeout: int = 120,
    ) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, messages: list[dict]) -> str:
        """Send a list of {role, content} messages and return the reply text."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
            },
        }
        try:
            resp = await self._client.post(f"{self.host}/api/chat", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:400]
            raise OllamaError(
                f"Ollama returned {exc.response.status_code}: {body}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not reach Ollama at {self.host}: {exc}") from exc

        data = resp.json()
        content = (data.get("message") or {}).get("content", "")
        if not content:
            raise OllamaError("Ollama returned an empty response.")
        return content.strip()

    async def health(self) -> bool:
        """Return True if the configured model is available on the server."""
        try:
            resp = await self._client.get(f"{self.host}/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError:
            return False
        names = {m.get("name", "") for m in resp.json().get("models", [])}
        # Ollama tags include the ":latest" suffix; match loosely.
        base = self.model.split(":")[0]
        return any(n == self.model or n.split(":")[0] == base for n in names)
