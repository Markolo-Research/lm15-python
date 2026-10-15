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


@pytest.mark.asyncio
async def test_close_waits_for_construction_without_blocking_loop():
    router = AsyncLMRouter()
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    closed = []

    class Transport:
        async def aclose(self):
            closed.append(threading.get_ident())

    def construct():
        with router._lock:
            entered.set()
            assert release.wait(3), "aclose blocked the event loop"
            router._transport = Transport()
            router._lms["test"] = object()

    construction = asyncio.create_task(asyncio.to_thread(construct))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        closing = asyncio.create_task(router.aclose())
        await asyncio.sleep(0.02)
        assert not closing.done()
        release.set()
        await construction
        await closing
        assert closed == [loop_thread]
        assert router._transport is None
        assert router._lms == {}
    finally:
        release.set()
        await construction
        if closing is not None:
            await closing


@pytest.mark.asyncio
@pytest.mark.parametrize("during_close", [False, True])
async def test_cancelled_close_finishes_cleanup(during_close):
    router = AsyncLMRouter()
    entered, release = threading.Event(), threading.Event()
    close_entered, close_release = asyncio.Event(), asyncio.Event()
    closed = []

    class Transport:
        async def aclose(self):
            close_entered.set()
            await close_release.wait()
            closed.append(True)

    def construct():
        with router._lock:
            entered.set()
            assert release.wait(3)
            router._transport = Transport()

    construction = asyncio.create_task(asyncio.to_thread(construct))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        closing = asyncio.create_task(router.aclose())
        await asyncio.sleep(0.02)
        if during_close:
            release.set()
            await close_entered.wait()
        closing.cancel()
        await asyncio.sleep(0.02)
        closing.cancel()
        await asyncio.sleep(0.02)
        assert not closing.done()
        release.set()
        close_release.set()
        await construction
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert closed == [True]
        assert router._transport is None
        await router.aclose()
        assert closed == [True]
    finally:
        release.set()
        close_release.set()
        await construction
        if closing is not None:
            try:
                await closing
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_cancelled_construction_is_drained(failure, monkeypatch):
    router = AsyncLMRouter()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def construct(model):
        entered.set()
        assert release.wait(3)
        finished.set()
        if failure:
            raise ValueError("construction failed")
        return object()

    monkeypatch.setattr(router, "lm", construct)
    operation = asyncio.create_task(router._alm("groq:test"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        operation.cancel()
        await asyncio.sleep(0.02)
        operation.cancel()
        await asyncio.sleep(0.02)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert finished.is_set()
    finally:
        release.set()
        try:
            await operation
        except asyncio.CancelledError:
            pass
