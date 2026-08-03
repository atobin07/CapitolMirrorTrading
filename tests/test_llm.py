"""Tests for the pluggable LLM backend. Run: python tests/test_llm.py"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm import OpenAICompatClient, build_llm, uses_openai  # noqa: E402
from app.ollama_client import OllamaClient, OllamaError  # noqa: E402

_checks = 0


def check(cond, msg):
    global _checks
    assert cond, "FAILED: " + msg
    _checks += 1


def _cfg(**kw):
    base = dict(
        llm_backend="ollama", llm_base_url="", llm_api_key="", llm_model="",
        llm_max_tokens=512, ollama_host="http://x:11434", ollama_model="llama3.1:8b",
        ollama_temperature=0.6, ollama_num_ctx=4096, ollama_timeout=120,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_factory():
    check(isinstance(build_llm(_cfg()), OllamaClient), "defaults to Ollama")
    c = build_llm(_cfg(llm_backend="openai", llm_base_url="https://api.x/v1",
                       llm_api_key="k", llm_model="uncensored/model"))
    check(isinstance(c, OpenAICompatClient), "openai backend → OpenAI client")
    check(c.model == "uncensored/model", "uses LLM_MODEL")
    check(uses_openai(_cfg(llm_backend="openrouter")), "openrouter counts as openai-compatible")


async def test_openai_chat():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            check(request.headers.get("Authorization") == "Bearer sk-test", "sends bearer key")
            body = request.content.decode()
            check('"model": "unc/model"' in body or '"model":"unc/model"' in body, "sends model")
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "hey, what's up?"}}]
            })
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "unc/model"}]})
        return httpx.Response(404, json={})

    c = OpenAICompatClient("https://api.x/v1", "sk-test", "unc/model")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    reply = await c.chat([{"role": "user", "content": "hi"}])
    check(reply == "hey, what's up?", "parses choices[0].message.content")
    check(await c.health() is True, "health OK on 200 /models")
    await c.close()


async def test_openai_errors():
    def handler(request):
        return httpx.Response(401, text="invalid api key")
    c = OpenAICompatClient("https://api.x/v1", "bad", "m")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await c.chat([{"role": "user", "content": "hi"}])
        raised = False
    except OllamaError as exc:
        raised = "401" in str(exc)
    check(raised, "HTTP error surfaces as OllamaError with status")
    check(await c.health() is False, "health False when key rejected")
    await c.close()


async def main():
    test_factory()
    await test_openai_chat()
    await test_openai_errors()
    print(f"\nALL {_checks} LLM-BACKEND CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
