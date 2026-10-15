"""Offline async construction must not block unrelated loop work."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import threading

import pytest

from lm15 import Message, Request
from lm15.router import AsyncLMRouter


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "stream", "cache", "openai_complete", "openai_stream"])
async def test_initial_adapter_construction_runs_off_loop(monkeypatch, operation):
    router = AsyncLMRouter()
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    worker_threads = []
    closed = []

    @dataclass
    class Cached:
        provider: str = "groq"

    class Adapter:
        async def complete(self, request):
            return "complete"

        async def stream(self, request):
            try:
                yield "event"
            finally:
                closed.append(True)

        async def cache(self, request, **kwargs):
            return Cached()

    def construct(model):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "construction blocked the event loop"
        return Adapter()

    monkeypatch.setattr(router, "lm", construct)
    request = Request(model="groq:test", messages=(Message.user("hi"),))

    async def invoke():
        if operation == "complete":
            return await router.complete(request)
        if operation == "cache":
            return await router.cache(request)
        if operation == "openai_complete":
            return await router.complete_from_openai_chat("groq:test", [{"role": "user", "content": "hi"}])
        stream = (router.stream(request) if operation == "stream" else
                  router.stream_from_openai_chat("groq:test", [{"role": "user", "content": "hi"}]))
        try:
            assert await anext(stream) == "event"
        finally:
            await stream.aclose()

    task = asyncio.create_task(invoke())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert worker_threads == [worker_threads[0]]
        assert worker_threads[0] != loop_thread
        release.set()
        await task
        if operation in ("stream", "openai_stream"):
            assert closed == [True]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
