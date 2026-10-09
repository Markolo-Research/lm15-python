"""Offline public-operation regressions for coherent managed dispatch."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from lm15 import BatchRequest, Message, Request, UnsupportedFeatureError
from lm15.errors import AuthOperationError
from lm15.login import Auth, MemoryStore
from lm15.login.bound import BoundClient
from lm15.login.flows.base import LoginResult, oauth_material
from lm15.login.flows.codex import CodexFlow
from lm15.login.flows.copilot import CopilotFlow
from lm15.login.types import ModelSelection
from lm15.providers.base import BaseProviderLM
from lm15.router import AsyncLMRouter, LMRouter, RouterConfig
from lm15.testing import FakeResponse, FakeTransport
from tests.test_async_adapters import FakeAsyncTransport
from tests.test_login import Clock, ScriptUI, _chat_reply, _models_reply
from tests.test_subscription_auth import _CODEX_STREAM_BODY


def request(model="llama3"):
    return Request(model=model, messages=(Message.user("hi"),))


def local(auth, label, *, replace=None):
    # The local recipe accepts a literal key as well as a server URL.
    return auth.configure("ollama", method="local", answers={
        "key": f"token-{label}", "base_url": f"https://{label}.example/v1",
    }, replace=replace)


def wire_pair(sent, token, url):
    assert dict(sent.headers)["Authorization"] == f"Bearer {token}"
    assert sent.url == url


def invoke(lm, operation, asynchronous, model="llama3"):
    if not asynchronous:
        if operation == "stream":
            return list(lm.stream(request(model)))
        if operation == "list_models":
            return lm.list_models()
        return lm.complete(request(model))

    async def run():
        if operation == "stream":
            return [event async for event in lm.stream(request(model))]
        if operation == "list_models":
            return await lm.list_models()
        return await lm.complete(request(model))

    return asyncio.run(run())


def transport_for(operation, asynchronous):
    reply = _models_reply() if operation == "list_models" else _chat_reply()
    if operation == "stream":
        reply = FakeResponse(200, b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\ndata: [DONE]\n\n')
    return FakeAsyncTransport(reply.body) if asynchronous else FakeTransport([reply, reply])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation,path", [("complete", "/chat/completions"),
                                           ("stream", "/chat/completions"), ("list_models", "/models")])
def test_retained_adapter_follows_whole_local_connection(asynchronous, operation, path):
    auth = Auth.memory()
    first = local(auth, "a")
    transport = transport_for(operation, asynchronous)
    router = (AsyncLMRouter if asynchronous else LMRouter)(RouterConfig(auth=auth, transport=transport))
    lm = router.lm("ollama:llama3")
    invoke(lm, operation, asynchronous)
    local(auth, "b", replace=first.id)
    invoke(lm, operation, asynchronous)
    assert len(transport.requests) == 2
    for sent, label in zip(transport.requests, ("a", "b")):
        wire_pair(sent, f"token-{label}", f"https://{label}.example/v1{path}")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_retained_adapter_uses_replacement_transport(asynchronous):
    auth = Auth.memory()
    local(auth, "a")
    original = transport_for("complete", asynchronous)
    replacement = transport_for("complete", asynchronous)
    lm = (AsyncLMRouter if asynchronous else LMRouter)(RouterConfig(auth=auth, transport=original)).lm("ollama:llama3")
    lm.transport = replacement
    invoke(lm, "complete", asynchronous)
    assert original.requests == []
    assert len(replacement.requests) == 1
    wire_pair(replacement.requests[0], "token-a", "https://a.example/v1/chat/completions")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_delayed_stream_never_sends_a_stale_snapshot(asynchronous):
    auth = Auth.memory()
    first = local(auth, "a")
    transport = transport_for("stream", asynchronous)
    lm = (AsyncLMRouter if asynchronous else LMRouter)(RouterConfig(auth=auth, transport=transport)).lm("ollama:llama3")
    stream = lm.stream(request())
    assert transport.requests == []
    local(auth, "b", replace=first.id)
    if asynchronous:
        async def consume():
            return [event async for event in stream]

        asyncio.run(consume())
        assert len(transport.requests) == 1
        wire_pair(transport.requests[0], "token-b", "https://b.example/v1/chat/completions")
    else:
        # Sync stream encodes eagerly; async stream encodes at iteration.
        with pytest.raises(AuthOperationError) as error:
            list(stream)
        assert error.value.reason == "connection_changed"
        assert transport.requests == []


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["complete", "stream", "list_models"])
@pytest.mark.parametrize("change", ["replace", "logout"])
def test_change_after_encoding_before_handoff_sends_nothing(monkeypatch, asynchronous, operation, change):
    auth = Auth.memory()
    first = local(auth, "a")
    transport = transport_for(operation, asynchronous)
    lm = (AsyncLMRouter if asynchronous else LMRouter)(RouterConfig(auth=auth, transport=transport)).lm("ollama:llama3")
    emit = BaseProviderLM._emit
    encoded = []

    def after_encode(self, **kwargs):
        sent = emit(self, **kwargs)
        wire_pair(sent, "token-a", "https://a.example/v1/" + ("models" if operation == "list_models" else "chat/completions"))
        encoded.append(sent)
        if change == "replace":
            local(auth, "b", replace=first.id)
        else:
            auth.logout("ollama")
        return sent

    monkeypatch.setattr(BaseProviderLM, "_emit", after_encode)
    with pytest.raises(AuthOperationError) as error:
        invoke(lm, operation, asynchronous)
    assert error.value.reason == ("connection_changed" if change == "replace" else "login_required")
    assert len(encoded) == 1
    assert transport.requests == []


def test_concurrent_logout_of_encoded_request(monkeypatch):
    auth = Auth.memory()
    local(auth, "a")
    transport = FakeTransport([])
    lm = LMRouter(RouterConfig(auth=auth, transport=transport)).lm("ollama:llama3")
    encoded, resume = threading.Event(), threading.Event()
    emit = BaseProviderLM._emit
    errors = []

    def pause(self, **kwargs):
        sent = emit(self, **kwargs)
        encoded.set()
        assert resume.wait(5), "test failed to release encoded request"
        return sent

    def worker():
        try:
            lm.complete(request())
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(BaseProviderLM, "_emit", pause)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert encoded.wait(5)
        auth.logout("ollama")
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], AuthOperationError)
    assert errors[0].reason == "login_required"
    assert transport.requests == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_codex_token_and_account_follow_replacement(monkeypatch, asynchronous):
    labels = iter(("a", "b"))

    def login(self, ctx, method, settings, answers):
        label = next(labels)
        return LoginResult(material={"type": "oauth", "access": f"token-{label}",
                                     "accountId": f"account-{label}"}, label=label)

    monkeypatch.setattr(CodexFlow, "login", login)
    auth = Auth.memory()
    first = auth.login("openai-codex", "browser", ui=ScriptUI(), allow_unverified=True)
    reply = FakeResponse(200, _CODEX_STREAM_BODY)
    transport = FakeAsyncTransport(reply.body) if asynchronous else FakeTransport([reply, reply])
    lm = (AsyncLMRouter if asynchronous else LMRouter)(RouterConfig(auth=auth, transport=transport)).lm("openai-codex:gpt-5.5")
    invoke(lm, "complete", asynchronous, "gpt-5.5")
    auth.login("openai-codex", "browser", ui=ScriptUI(), replace=first.id, allow_unverified=True)
    invoke(lm, "stream", asynchronous, "gpt-5.5")
    assert len(transport.requests) == 2
    for sent, label in zip(transport.requests, ("a", "b")):
        wire_pair(sent, f"token-{label}", "https://chatgpt.com/backend-api/codex/responses")
        assert dict(sent.headers)["chatgpt-account-id"] == f"account-{label}"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_renewal_updates_endpoint_unless_explicit(monkeypatch, explicit, asynchronous):
    clock = Clock()

    def result(ctx, label):
        return LoginResult(material=oauth_material(
            access=f"token-{label};proxy-ep=proxy.{label}.githubcopilot.com", refresh="refresh",
            expires_in_s=3600, now_ms=int(ctx.wall_clock() * 1000)), label=label, renewal="remint")

    monkeypatch.setattr(CopilotFlow, "login", lambda self, ctx, *args: result(ctx, "a"))
    monkeypatch.setattr(CopilotFlow, "renew", lambda self, ctx, *args: result(ctx, "b"))
    auth = Auth(MemoryStore(), clock=clock.wall)
    auth.login("github-copilot", "device", ui=ScriptUI([""]), allow_unverified=True)
    transport = transport_for("complete", asynchronous)
    config = RouterConfig(auth=auth, transport=transport,
                          base_urls={"github-copilot": "https://explicit.example/v1"} if explicit else None)
    lm = (AsyncLMRouter if asynchronous else LMRouter)(config).lm("github-copilot:gpt-4o")
    invoke(lm, "complete", asynchronous, "gpt-4o")
    clock.advance(3400)
    invoke(lm, "complete", asynchronous, "gpt-4o")
    for sent, label in zip(transport.requests, ("a", "b")):
        url = "https://explicit.example/v1/chat/completions" if explicit else f"https://api.{label}.githubcopilot.com/chat/completions"
        wire_pair(sent, f"token-{label};proxy-ep=proxy.{label}.githubcopilot.com", url)
    assert auth.status("github-copilot").connection.credential_revision == "2"


def test_renewal_cannot_silently_change_codex_account(monkeypatch):
    clock = Clock()

    def result(ctx, account):
        return LoginResult(material=oauth_material(
            access="token", refresh="refresh", expires_in_s=3600,
            now_ms=int(ctx.wall_clock() * 1000), extra={"accountId": account}), label=account)

    monkeypatch.setattr(CodexFlow, "login", lambda self, ctx, *args: result(ctx, "account-a"))
    monkeypatch.setattr(CodexFlow, "renew", lambda self, ctx, *args: result(ctx, "account-b"))
    auth = Auth(MemoryStore(), clock=clock.wall)
    auth.login("openai-codex", "browser", ui=ScriptUI(), allow_unverified=True)
    transport = FakeTransport([])
    lm = LMRouter(RouterConfig(auth=auth, transport=transport)).lm("openai-codex:gpt-5.5")
    clock.advance(3400)
    with pytest.raises(AuthOperationError) as error:
        lm.complete(request("gpt-5.5"))
    assert error.value.reason == "connection_changed"
    assert transport.requests == []
    with pytest.raises(AuthOperationError) as error:
        lm.complete(request("gpt-5.5"))
    assert error.value.reason == "login_required"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_batch_does_not_switch_identity_between_upload_and_submit(asynchronous):
    auth = Auth.memory()
    first = auth.set_api_key("openai", "token-a")
    body = json.dumps({"id": "file-a", "object": "file", "purpose": "batch"}).encode()

    class ReplacingTransport(FakeAsyncTransport if asynchronous else FakeTransport):
        def stream(self, sent):
            reply = super().stream(sent)
            auth.set_api_key("openai", "token-b", replace=first.id)
            return reply

    transport = ReplacingTransport(body) if asynchronous else ReplacingTransport([FakeResponse(200, body)])
    lm = (AsyncLMRouter if asynchronous else LMRouter)(RouterConfig(auth=auth, transport=transport)).lm("openai:gpt-5-nano")
    batch = BatchRequest(requests=(request("gpt-5-nano"),))
    with pytest.raises(AuthOperationError) as error:
        if asynchronous:
            asyncio.run(lm.batch_submit(batch))
        else:
            lm.batch_submit(batch)
    assert error.value.reason == "connection_changed"
    assert len(transport.requests) == 1
    wire_pair(transport.requests[0], "token-a", "https://api.openai.com/v1/files")


def test_bound_stays_pinned_and_plan_is_offline(monkeypatch):
    auth = Auth.memory()
    first = local(auth, "a")
    transport = FakeTransport([_chat_reply()])
    selection = ModelSelection(provider="ollama", model="llama3", connection_id=first.id,
                               identity_generation=first.identity_generation)
    client = BoundClient(auth, selection, router_config=RouterConfig(transport=transport))
    client.complete(messages="hi")
    local(auth, "b", replace=first.id)
    with pytest.raises(AuthOperationError) as error:
        client.complete(messages="hi")
    assert error.value.reason == "connection_changed"
    assert len(transport.requests) == 1

    def forbidden(*args, **kwargs):
        raise AssertionError("offline plan resolved authentication")

    monkeypatch.setattr(auth, "request_auth", forbidden)
    LMRouter(RouterConfig(auth=auth, transport=transport)).plan(request("ollama:llama3"))
    assert len(transport.requests) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_managed_websocket_fails_closed(monkeypatch, asynchronous):
    auth = Auth.memory()
    auth.set_api_key("openai", "token-a")
    transport = transport_for("stream", asynchronous)
    lm = (AsyncLMRouter if asynchronous else LMRouter)(RouterConfig(auth=auth, transport=transport)).lm("openai:gpt-4o-realtime-preview")

    def forbidden(*args, **kwargs):
        raise AssertionError("managed websocket reached connection boundary")

    if asynchronous:
        monkeypatch.setattr("lm15.live.require_websocket_async_connect", forbidden)
    else:
        monkeypatch.setattr(type(lm), "_live_connect", forbidden)
    with pytest.raises(UnsupportedFeatureError, match="managed authentication"):
        if asynchronous:
            from lm15 import LiveConfig

            asyncio.run(lm.live(LiveConfig(model="gpt-4o-realtime-preview")))
        else:
            invoke(lm, "stream", False, "gpt-4o-realtime-preview")
    assert transport.requests == []
