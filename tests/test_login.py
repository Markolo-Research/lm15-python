"""Managed authentication (lm15.login, lm15.interactive): spec/auth.md
AUTH-12–26 core scenarios (lm15-contract/auth/managed/scenarios.md).

Every test is offline: a fake auth server behind ``Auth(opener=...)``, a
sandbox store under a temporary directory, a fixed clock, a scripted UI.
Scenario ids (MA-nnn) name the contract scenario each test carries.
Secrets are sentinels; every public rendering is checked for their absence.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from lm15 import Message
from lm15.errors import AuthOperationError, NotConfiguredError, RateLimitError, ServerError, TransportError
from lm15.login import Auth, FileStore, LoginCancelled, MemoryStore, TerminalUI, providers
from lm15.login.bound import BoundClient, model_choices
from lm15.login.engine import CallbackListener, LoginContext, parse_manual_return, run_device_flow, DeviceStep
from lm15.login.types import (
    AuthUrlNotice,
    DeviceCodeNotice,
    ManualCodePrompt,
    ModelSelection,
    SecretPrompt,
    SelectPrompt,
    TextPrompt,
)
from lm15.router import LMRouter, RouterConfig
from lm15.testing import FakeResponse, FakeTransport

SENTINEL = "SECRET-SENTINEL-DO-NOT-PRINT"
ACCESS = f"{SENTINEL}-access"
REFRESH = f"{SENTINEL}-refresh"


# ─── fakes ────────────────────────────────────────────────────────────


class _Reply:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self, n: int = -1) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(url: str, status: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(json.dumps(body).encode()))


class FakeXai:
    """The xAI device-code endpoints.  ``script`` is the list of poll
    outcomes; ``refresh`` the refresh outcome."""

    def __init__(self, polls=None, refresh=None) -> None:
        self.polls = list(polls or ["pending", "ok"])
        self.refresh = refresh or "ok"
        self.calls: list[tuple[str, dict]] = []
        self.token_n = 0

    def __call__(self, request: urllib.request.Request, timeout: float):
        url = request.full_url
        data = dict(urllib.parse.parse_qsl(request.data.decode())) if request.data else {}
        self.calls.append((url, data))
        if url.endswith("/device/code"):
            return _Reply(200, {"device_code": f"{SENTINEL}-device", "user_code": "ABCD-1234",
                                "verification_uri": "https://auth.x.ai/device", "interval": 1, "expires_in": 600})
        if url.endswith("/token") and data.get("grant_type", "").endswith("device_code"):
            step = self.polls.pop(0) if self.polls else "ok"
            if step == "pending":
                raise _http_error(url, 400, {"error": "authorization_pending"})
            if step == "slow":
                raise _http_error(url, 400, {"error": "slow_down"})
            if step == "denied":
                raise _http_error(url, 400, {"error": "access_denied", "error_description": SENTINEL + "-desc"})
            if step == "expired":
                raise _http_error(url, 400, {"error": "expired_token"})
            if step == "timeout":
                raise urllib.error.URLError(TimeoutError("timed out"))
            self.token_n += 1
            return _Reply(200, {"access_token": f"{ACCESS}{self.token_n}", "refresh_token": f"{REFRESH}{self.token_n}",
                                "expires_in": 3600})
        if url.endswith("/token") and data.get("grant_type") == "refresh_token":
            if self.refresh == "rejected":
                raise _http_error(url, 401, {"error": "invalid_grant", "error_description": SENTINEL})
            if self.refresh == "rate_limit":
                raise _http_error(url, 429, {"error": "temporarily_unavailable"})
            if self.refresh == "server":
                raise _http_error(url, 503, {})
            if self.refresh == "timeout":
                raise urllib.error.URLError(TimeoutError("timed out"))
            if self.refresh == "refused":
                raise urllib.error.URLError(ConnectionRefusedError("refused"))
            self.token_n += 1
            return _Reply(200, {"access_token": f"{ACCESS}{self.token_n}", "refresh_token": f"{REFRESH}{self.token_n}",
                                "expires_in": 3600})
        raise AssertionError(f"unexpected request {url}")


class ScriptUI:
    def __init__(self, answers=()) -> None:
        self.answers = list(answers)
        self.prompts: list = []
        self.notices: list = []

    def prompt(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt {prompt!r}")
        answer = self.answers.pop(0)
        return answer(prompt) if callable(answer) else answer

    def notify(self, notice):
        self.notices.append(notice)


def pick(label):
    def choose(prompt):
        assert isinstance(prompt, SelectPrompt), prompt
        for option in prompt.options:
            if label in (option.id, option.label):
                return option.id
        raise AssertionError((label, [o.id for o in prompt.options]))

    return choose


class Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start
        self.m = 0.0

    def wall(self) -> float:
        return self.t

    def mono(self) -> float:
        return self.m

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.m += seconds


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LM15_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("LM15_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # No Pi store, no CLI files: mode A must see only the sandbox.
    import lm15.auth as legacy

    monkeypatch.setattr(legacy, "PI_AGENT_AUTH_PATH", tmp_path / "no-pi.json")
    monkeypatch.setattr(legacy, "CLAUDE_CODE_CREDENTIALS_PATH", tmp_path / "no-claude.json")
    monkeypatch.setattr(legacy, "CODEX_CLI_AUTH_PATH", tmp_path / "no-codex.json")
    return tmp_path


def make_auth(sandbox: Path, clock: Clock, server=None, *, memory=False, real_sleep=False) -> Auth:
    store = MemoryStore() if memory else FileStore(sandbox / "credentials.json")
    sleep = None if real_sleep else clock.advance
    return Auth(store, clock=clock.wall, monotonic=clock.mono, opener=server or FakeXai(), sleep=sleep)


def no_secret(*texts: object) -> None:
    for text in texts:
        assert SENTINEL not in repr(text)
        assert SENTINEL not in str(text)


# ─── MA-001 construction is inert; MA-002 discovery ───────────────────


def test_construction_reads_nothing(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: calls.append(self) or "")
    auth = Auth.local(sandbox / "credentials.json")
    auth.providers()
    auth.methods("xai")
    assert providers.xai.id == "xai" and providers.openai_codex.id == "openai-codex"
    assert calls == []
    assert not (sandbox / "credentials.json").exists()


def test_public_provider_descriptors_are_the_login_registry() -> None:
    import importlib

    import lm15

    import lm15.providers.base as base
    import lm15.providers.openai as openai

    assert lm15.provider_descriptors is providers
    assert lm15.provider_descriptors.xai.id == "xai"
    assert lm15.provider_descriptors.openai_codex.id == "openai-codex"
    assert lm15.providers is importlib.import_module("lm15.providers")
    assert base is importlib.import_module("lm15.providers.base")
    assert openai is importlib.import_module("lm15.providers.openai")


def test_descriptors_are_data_not_authority(sandbox: Path) -> None:
    auth = Auth.memory()
    with pytest.raises(AttributeError):
        providers.not_a_provider
    with pytest.raises(AuthOperationError) as info:
        auth.login("not-a-provider", ui=ScriptUI())
    assert info.value.reason == "method_unavailable"
    claude = [m for m in auth.methods("claude-code")]
    assert {m.id: m.availability for m in claude} == {
        "browser": "unverified", "loopback": "unverified", "external:claude-code-cli": "supported",
    }
    radius = auth.descriptor("radius")
    assert all(m.availability == "unavailable" for m in radius.methods)


# ─── MA-003 a missing choice asks; unverified is opt-in ───────────────


def test_missing_method_asks_never_guesses(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    ui = ScriptUI([pick("device")])
    connection = auth.login("xai", ui=ui)
    assert isinstance(ui.prompts[0], SelectPrompt)
    assert [o.id for o in ui.prompts[0].options] == ["device", "external:pi-xai", "api_key", "env"]  # all supported; unverified/unavailable not offered
    assert connection.kind == "account" and connection.method_id == "device"
    with pytest.raises(AuthOperationError) as info:
        auth.login("claude-code", "browser", ui=ScriptUI())
    assert info.value.reason == "method_unavailable" and "allow_unverified" in str(info.value)
    with pytest.raises(AuthOperationError) as info:
        auth.login("radius", "browser", ui=ScriptUI())
    assert info.value.reason == "method_unavailable"


# ─── MA-016 device pacing; MA-017 the login itself ────────────────────


def test_device_login_saves_actual_expiry_and_one_owner(sandbox: Path) -> None:
    clock = Clock()
    server = FakeXai(polls=["pending", "slow", "ok"])
    auth = make_auth(sandbox, clock, server)
    ui = ScriptUI()
    connection = auth.login("xai", "device", ui=ui)
    notice = next(n for n in ui.notices if isinstance(n, DeviceCodeNotice))
    assert notice.user_code == "ABCD-1234" and notice.verification_url.startswith("https://")
    no_secret(notice, connection, ui.notices, auth.status("xai"))
    document = json.loads((sandbox / "credentials.json").read_text(encoding="utf-8"))
    entry = document["xai"]
    assert entry["type"] == "oauth" and entry["expires"] == int(clock.t * 1000) + 3600 * 1000
    assert entry["issued_at"] == int(clock.t * 1000) and entry["lifetime_s"] == 3600.0
    assert document["_lm15"]["slots"]["xai"]["generation"] == "1"
    # The legacy loader reads the same entry: one file, one owner.
    import lm15.auth as legacy

    assert legacy.xai_stored_state() == "usable"
    # Pacing: the slow_down step raised the interval to >= 6 s (1 + 5).
    assert auth.status("xai").usability == "ready"


def test_device_pacing_never_decreases_and_deadline_never_extends() -> None:
    waits: list[float] = []
    clock = Clock()

    class _UI(ScriptUI):
        pass

    ctx = LoginContext(ui=_UI(), deadline=clock.mono() + 100, clock=clock.mono, wall_clock=clock.wall)
    original_wait = ctx.wait

    def wait(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    ctx.wait = wait  # type: ignore[method-assign]
    steps = iter([DeviceStep("pending"), DeviceStep("slow_down", interval_s=2.0), DeviceStep("slow_down"),
                  DeviceStep("complete", "done")])
    assert run_device_flow(ctx, lambda: next(steps), interval_s=5.0, expires_in_s=50.0) == "done"
    assert waits == [5.0, 5.0, 10.0, 15.0]  # provider 5; slow_down(2) -> max(5+5, 2); slow_down -> +5
    assert ctx.deadline == pytest.approx(50.0)  # bounded by the provider's expiry, not extended


def test_device_denied_and_expired_are_typed_without_provider_text(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock, FakeXai(polls=["denied"]))
    with pytest.raises(AuthOperationError) as info:
        auth.login("xai", "device", ui=ScriptUI())
    assert info.value.reason == "login_denied" and info.value.commit_state == "not_committed"
    no_secret(info.value, info.value.__cause__)
    auth2 = make_auth(sandbox, clock, FakeXai(polls=["expired"]))
    with pytest.raises(AuthOperationError) as info:
        auth2.login("xai", "device", ui=ScriptUI())
    assert info.value.reason == "login_expired"
    assert auth.status("xai").presence == "absent"  # nothing half-saved


# ─── MA-011 PKCE; MA-012/013/014 listener hardening ───────────────────


def test_pkce_is_s256_and_fresh() -> None:
    from lm15.authkit import generate_pkce, pkce_challenge

    a, b = generate_pkce(), generate_pkce()
    assert a.verifier != b.verifier and a.method == "S256"
    # RFC 7636 appendix B vector
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - loopback test
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_listener_validates_state_on_success_and_error_and_binds_loopback() -> None:
    clock = Clock()
    ctx = LoginContext(ui=ScriptUI(), deadline=time.monotonic() + 10, provider="t")
    with CallbackListener(path="/cb", expected_state="S", port=0, redirect_host="localhost") as listener:
        base = f"http://127.0.0.1:{listener.port}"
        assert listener.redirect_uri == f"http://localhost:{listener.port}/cb"  # registered host != bind host
        assert _get(f"{base}/other?code=x&state=S") == 404
        assert _get(f"{base}/cb?code=x&state=WRONG") == 400  # wrong state: generic, keeps waiting
        assert _get(f"{base}/cb?error=access_denied&state=WRONG") == 400  # error with wrong state: ignored
        assert _get(f"{base}/cb?code=x&error=y&state=S") == 400  # both: invalid
        assert _get(f"{base}/cb?code=x&code=y&state=S") == 400  # duplicate params
        assert not listener._done.is_set()
        assert _get(f"{base}/cb?code={SENTINEL}&state=S") == 200
        result = listener.wait(ctx)
        assert result is not None and result.code == SENTINEL
        no_secret(result)
        assert listener._done.is_set()  # one-shot: the serve loop ends with the first accepted return
    with pytest.raises(AuthOperationError):
        CallbackListener(path="/cb", expected_state=None, bind_host="0.0.0.0")


def test_listener_rejects_oversized_target() -> None:
    with CallbackListener(path="/cb", expected_state=None, port=0) as listener:
        assert _get(f"http://127.0.0.1:{listener.port}/cb?code=" + "a" * 9000) == 414


def test_manual_return_parsing_checks_context() -> None:
    ok = parse_manual_return("http://localhost:1/cb?code=C&state=S", expected_state="S", allow_bare_code=False,
                             registered_path="/cb")
    assert ok.code == "C" and ok.state == "S"
    with pytest.raises(AuthOperationError) as info:
        parse_manual_return("http://localhost:1/cb?code=C&state=X", expected_state="S", allow_bare_code=False,
                            registered_path="/cb")
    assert info.value.reason == "invalid_login_state"
    with pytest.raises(AuthOperationError):
        parse_manual_return("http://localhost:1/wrong?code=C&state=S", expected_state="S", allow_bare_code=False,
                            registered_path="/cb")
    with pytest.raises(AuthOperationError):  # a bare code cannot supply state
        parse_manual_return("C", expected_state="S", allow_bare_code=False, registered_path="/cb")
    assert parse_manual_return("C#S", expected_state="S", allow_bare_code=False, registered_path="/cb").code == "C"
    assert parse_manual_return("C", expected_state=None, allow_bare_code=True, registered_path="/x").code == "C"


# ─── MA-019 literal keys; MA-054 no mystery strings ───────────────────


def test_keys_are_literal_text(sandbox: Path) -> None:
    auth = Auth.memory()
    connection = auth.set_api_key("groq", "!shell echo $HOME")
    assert auth.request_auth("groq").credential.value == "!shell echo $HOME"
    no_secret(connection)
    with pytest.raises(AuthOperationError) as info:
        auth.set_api_key("groq", "   ")
    assert info.value.reason == "interaction_required"


# ─── MA-025/026 replacement; MA-029 logout; MA-028 cancel vs commit ───


def test_replacement_is_conditional_and_old_stays_usable(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    first = auth.login("xai", "device", ui=ScriptUI())
    with pytest.raises(AuthOperationError) as info:
        auth.login("xai", "device", ui=ScriptUI())
    assert info.value.reason == "connection_exists" and info.value.connection_id == first.id
    assert auth.request_auth("xai").credential.value == f"{ACCESS}1"  # still usable
    with pytest.raises(AuthOperationError) as info:
        auth.login("xai", "device", ui=ScriptUI(), replace="cn_wrong")
    assert info.value.reason == "connection_changed"
    second = auth.login("xai", "device", ui=ScriptUI(), replace=first.id)
    assert second.id != first.id and second.identity_generation == "2"
    assert auth.request_auth("xai").credential.value == f"{ACCESS}2"


def test_replacement_does_not_retain_the_previous_host_settings(monkeypatch) -> None:
    from lm15.login.flows.base import LoginResult
    from lm15.login.flows.copilot import CopilotFlow

    monkeypatch.setattr(CopilotFlow, "login", lambda *args, **kwargs: LoginResult(
        material={"type": "oauth", "access": "test-token"}, label="GitHub", renewal="none"
    ))
    auth = Auth.memory()
    first = auth.login("github-copilot", "device", ui=ScriptUI(), answers={"enterprise_domain": "old.example"},
                       settings={"enterprise_domain": "old.example"}, allow_unverified=True)
    assert first.settings["enterprise_domain"] == "old.example"
    second = auth.login("github-copilot", "device", ui=ScriptUI(), answers={"enterprise_domain": ""},
                        replace=first.id, allow_unverified=True)
    assert "enterprise_domain" not in second.settings


def test_cancelled_login_cleanup_keeps_the_new_attempt_cancellable(monkeypatch) -> None:
    from lm15.login import manager

    auth = Auth.memory()
    events = [threading.Event(), threading.Event()]
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    errors = []

    class Flow:
        def login(self, ctx, method, settings, answers):
            index = 0 if ctx.cancel is events[0] else 1
            entered[index].set()
            assert release[index].wait(5)
            ctx.check()
            raise AssertionError("A cancelled login should not complete")

    monkeypatch.setattr(manager, "_flow", lambda *args: Flow())

    def login(index):
        try:
            auth.login("xai", "device", ui=ScriptUI(), cancel=events[index])
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=login, args=(0,))
    second = threading.Thread(target=login, args=(1,))
    first.start()
    try:
        assert entered[0].wait(5)
        assert auth.cancel_login("xai") == "cancelled"
        second.start()
        assert entered[1].wait(5)
        release[0].set()
        first.join(5)
        assert not first.is_alive()
        assert auth.cancel_login("xai") == "cancelled"
        assert events[1].is_set()
    finally:
        for signal in release:
            signal.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert len(errors) == 2 and all(isinstance(error, LoginCancelled) for error in errors)


@pytest.mark.asyncio
async def test_async_login_cancellation_prevents_late_connection_persistence(monkeypatch) -> None:
    import asyncio

    from lm15.login import AsyncAuth, manager
    from lm15.login.flows.base import LoginResult

    auth = AsyncAuth.memory()
    entered, release, finished, cancel = (threading.Event() for _ in range(4))

    class Flow:
        def login(self, ctx, method, settings, answers):
            entered.set()
            assert release.wait(5)
            return LoginResult(material={"type": "oauth", "access": "test-token"}, label="late grant", renewal="none")

    monkeypatch.setattr(manager, "_flow", lambda *args: Flow())
    login = auth.sync.login

    def tracked_login(*args, **kwargs):
        try:
            return login(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(auth.sync, "login", tracked_login)
    task = asyncio.create_task(auth.login("xai", "device", ui=ScriptUI(), cancel=cancel))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert cancel.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 5)
        assert auth.sync.status("xai").presence == "absent"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)


def test_logout_is_scoped_idempotent_and_leaves_a_marker(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    auth.set_api_key("groq", "k1")
    xai = auth.login("xai", "device", ui=ScriptUI())
    result = auth.logout("xai")
    assert result.forgot and result.identity_generation == "2"
    assert auth.logout("xai").forgot is False  # idempotent
    assert auth.logout(xai.id).forgot is False  # the old id does not remove anything newer
    status = auth.status("xai")
    assert status.presence == "absent" and status.logged_out
    assert "xai" not in json.loads((sandbox / "credentials.json").read_text(encoding="utf-8"))  # material gone
    assert auth.request_auth("groq").credential.value == "k1"  # other slots untouched
    with pytest.raises(AuthOperationError) as info:
        auth.request_auth("xai")
    assert info.value.reason == "login_required" and "signed out" in str(info.value)


def test_cancel_before_commit_wins_and_after_commit_does_not(sandbox: Path) -> None:
    clock = Clock()
    server = FakeXai(polls=["pending"] * 50)
    auth = make_auth(sandbox, clock, server, real_sleep=True)
    cancel = threading.Event()
    started = threading.Event()

    class _UI(ScriptUI):
        def notify(self, notice):
            super().notify(notice)
            started.set()

    outcome: dict = {}

    def run() -> None:
        try:
            auth.login("xai", "device", ui=_UI(), cancel=cancel)
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    started.wait(5)
    assert auth.cancel_login("xai") == "cancelled"
    thread.join(10)
    assert isinstance(outcome.get("error"), LoginCancelled)
    assert auth.status("xai").presence == "absent"
    document = json.loads((sandbox / "credentials.json").read_text(encoding="utf-8"))
    assert "attempt" not in document["_lm15"]["slots"].get("xai", {})
    # After a commit, cancel reports complete and undo is logout.
    auth._sleep = clock.advance
    auth._opener = FakeXai()
    auth.login("xai", "device", ui=ScriptUI())
    assert auth.cancel_login("xai") == "complete"


def test_one_active_attempt_per_slot(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    auth._reserve("xai", "at_other", None, 900.0)
    with pytest.raises(AuthOperationError) as info:
        auth.login("xai", "device", ui=ScriptUI())
    assert info.value.reason == "login_in_progress"
    with pytest.raises(AuthOperationError) as info:
        auth.set_api_key("xai", "k")
    assert info.value.reason == "login_in_progress"
    clock.advance(20 * 60)  # a stale reservation expires
    auth.login("xai", "device", ui=ScriptUI())


def test_an_interrupt_ends_the_attempt_and_frees_the_slot(sandbox: Path) -> None:
    # Ctrl-C — a notebook's cancel — while waiting for the provider, or at
    # a prompt: the attempt is over, so its reservation must not block the
    # next sign-in for the rest of its lifetime.
    clock = Clock()

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    auth = Auth(FileStore(sandbox / "credentials.json"), clock=clock.wall, monotonic=clock.mono,
                opener=FakeXai(polls=["pending"] * 5), sleep=interrupted)
    with pytest.raises(KeyboardInterrupt):
        auth.login("xai", "device", ui=ScriptUI())
    document = json.loads((sandbox / "credentials.json").read_text(encoding="utf-8"))
    assert "attempt" not in document["_lm15"]["slots"].get("xai", {})
    auth._sleep = clock.advance
    auth._opener = FakeXai()
    assert auth.login("xai", "device", ui=ScriptUI()).provider == "xai"


# ─── MA-032/033/036/037 renewal ────────────────────────────────────────


def test_renewal_is_due_at_lead_and_bumps_revision(sandbox: Path) -> None:
    clock = Clock()
    server = FakeXai()
    auth = make_auth(sandbox, clock, server)
    auth.login("xai", "device", ui=ScriptUI())
    clock.advance(3600 - 301)  # one second before the 5-minute lead: still fresh
    assert auth.request_auth("xai").credential.value == f"{ACCESS}1"
    assert auth.status("xai").usability == "ready"
    clock.advance(2)
    assert auth.status("xai").usability == "renewal_due"
    assert auth.request_auth("xai").credential.value == f"{ACCESS}2"
    assert auth.status("xai").connection.credential_revision == "2"
    document = json.loads((sandbox / "credentials.json").read_text(encoding="utf-8"))
    assert document["xai"]["refresh"] == f"{REFRESH}2" and "renewal_in_flight" not in document["_lm15"]["slots"]["xai"]
    refreshes = [d for u, d in server.calls if d.get("grant_type") == "refresh_token"]
    assert len(refreshes) == 1


def test_short_token_lead_is_lifetime_over_ten(sandbox: Path) -> None:
    auth = Auth.memory()
    assert auth._lead_ms(60.0) == 6000 and auth._lead_ms(3600.0) == 300_000 and auth._lead_ms(None) == 300_000


def test_sibling_refresh_is_reused_not_repeated(sandbox: Path) -> None:
    clock = Clock()
    server = FakeXai()
    auth = make_auth(sandbox, clock, server)
    auth.login("xai", "device", ui=ScriptUI())
    clock.advance(3400)
    other = make_auth(sandbox, clock, server)  # another process, same file
    other.request_auth("xai")  # renews
    auth.request_auth("xai")  # re-reads under the lock: fresh now, no second exchange
    refreshes = [d for u, d in server.calls if d.get("grant_type") == "refresh_token"]
    assert len(refreshes) == 1


def test_permanent_rejection_marks_needs_login_and_never_falls_back(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock, FakeXai(refresh="rejected"))
    auth.login("xai", "device", ui=ScriptUI())
    clock.advance(3400)
    monkeypatch.setenv("XAI_API_KEY", SENTINEL)
    with pytest.raises(AuthOperationError) as info:
        auth.request_auth("xai")
    assert info.value.reason == "credential_rejected" and info.value.commit_state == "committed"
    no_secret(info.value)
    assert auth.status("xai").usability == "needs_login"
    with pytest.raises(AuthOperationError) as info:
        auth.request_auth("xai")
    assert info.value.reason == "login_required"
    router = LMRouter(RouterConfig(auth=auth))
    with pytest.raises(AuthOperationError):
        router.lm("xai:grok-4")  # no env fallback in managed mode


def test_transient_failure_keeps_credentials(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock, FakeXai(refresh="server"))
    auth.login("xai", "device", ui=ScriptUI())
    clock.advance(3400)
    with pytest.raises(ServerError):
        auth.request_auth("xai")
    assert auth.status("xai").usability == "renewal_due"  # nothing lost, nothing marked
    auth2 = make_auth(sandbox, clock, FakeXai(refresh="refused"))
    with pytest.raises(TransportError):
        auth2.request_auth("xai")
    assert auth2.status("xai").usability == "renewal_due"


def test_rate_limited_renewal_is_not_retried_and_can_succeed_later(sandbox: Path) -> None:
    clock = Clock()
    server = FakeXai(refresh="rate_limit")
    auth = make_auth(sandbox, clock, server)
    auth.login("xai", "device", ui=ScriptUI())
    clock.advance(3400)

    with pytest.raises(RateLimitError) as info:
        auth.request_auth("xai")
    assert info.value.status == 429
    refreshes = [d for _, d in server.calls if d.get("grant_type") == "refresh_token"]
    assert len(refreshes) == 1
    status = auth.status("xai")
    assert status.usability == "renewal_due"
    assert status.connection.credential_revision == "1"
    document = json.loads((sandbox / "credentials.json").read_text(encoding="utf-8"))
    assert document["xai"]["access"] == f"{ACCESS}1"
    assert document["xai"]["refresh"] == f"{REFRESH}1"
    assert "renewal_in_flight" not in document["_lm15"]["slots"]["xai"]

    server.refresh = "ok"
    assert auth.request_auth("xai").credential.value == f"{ACCESS}2"
    assert auth.status("xai").connection.credential_revision == "2"
    assert len([d for _, d in server.calls if d.get("grant_type") == "refresh_token"]) == 2


def test_uncertain_exchange_is_indeterminate_not_retried(sandbox: Path) -> None:
    clock = Clock()
    server = FakeXai(refresh="timeout")
    auth = make_auth(sandbox, clock, server)
    auth.login("xai", "device", ui=ScriptUI())
    clock.advance(3400)
    with pytest.raises(AuthOperationError) as info:
        auth.request_auth("xai")
    assert info.value.reason == "indeterminate" and info.value.commit_state == "unknown"
    assert auth.status("xai").usability == "indeterminate"
    with pytest.raises(AuthOperationError) as info:
        auth.request_auth("xai")  # never a blind retry
    assert info.value.reason == "indeterminate"
    assert len([d for u, d in server.calls if d.get("grant_type") == "refresh_token"]) == 1
    auth.login("xai", "device", ui=ScriptUI(), replace=auth.status("xai").connection.id)  # recovery
    assert auth.status("xai").usability == "ready"


# ─── MA-040 (structural) / MA-041 storage ─────────────────────────────


def test_unreadable_store_is_not_absence(sandbox: Path) -> None:
    path = sandbox / "credentials.json"
    path.write_text("{not json", encoding="utf-8")
    auth = Auth.local(path)
    with pytest.raises(AuthOperationError) as info:
        auth.status("xai")
    assert info.value.reason == "storage_unavailable"
    assert path.read_text(encoding="utf-8") == "{not json"  # never overwritten
    path.write_text(json.dumps({"_lm15": {"version": 99, "slots": {}}}), encoding="utf-8")
    with pytest.raises(AuthOperationError) as info:
        auth.connections()
    assert info.value.reason == "unsupported_store_version"
    path.write_text('{"xai": {"a": 1}, "xai": {"b": 2}}', encoding="utf-8")
    with pytest.raises(AuthOperationError):
        auth.status("xai")


def test_generations_are_decimal_strings_and_survive_reload(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    auth.set_api_key("groq", "k")
    document = json.loads((sandbox / "credentials.json").read_text(encoding="utf-8"))
    document["_lm15"]["slots"]["groq"]["generation"] = str(2**60)
    (sandbox / "credentials.json").write_text(json.dumps(document), encoding="utf-8")
    assert auth.status("groq").connection.identity_generation == str(2**60)
    auth.logout("groq")
    assert auth.status("groq").presence == "absent"
    assert json.loads((sandbox / "credentials.json").read_text(encoding="utf-8"))["_lm15"]["slots"]["groq"]["generation"] == str(2**60 + 1)


# ─── MA-005/007/060 identity selection and the legacy boundary ────────


def test_managed_router_order_explicit_then_connection_never_env(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    auth.login("xai", "device", ui=ScriptUI())
    env = {"XAI_API_KEY": SENTINEL, "GEMINI_API_KEY": SENTINEL}
    router = LMRouter(RouterConfig(auth=auth, env=env))
    assert "managed connection" in router.lm("xai:grok-4").credential_origin()
    with pytest.raises(AuthOperationError) as info:
        router.lm("gemini:gemini-2.5-flash")
    assert info.value.reason == "login_required"
    no_secret(info.value)
    explicit = LMRouter(RouterConfig(auth=auth, env=env, api_keys={"xai": "explicit"}))
    assert "explicit" in explicit.lm("xai:grok-4").credential_origin()
    assert "placeholder" in router.lm("ollama:llama3").credential_origin()  # keyless local still works
    with pytest.raises(AuthOperationError) as info:
        router.lm("azure:gpt-4o")  # the machine's cloud identity is not discovered
    assert info.value.reason == "login_required"


def test_explicit_failure_is_not_a_fallback(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    auth.login("xai", "device", ui=ScriptUI())
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("callable failed")

    transport = FakeTransport([_chat_reply()])
    router = LMRouter(RouterConfig(auth=auth, api_keys={"xai": broken}, env={"XAI_API_KEY": SENTINEL}, transport=transport))
    lm = router.lm("xai:grok-4")
    with pytest.raises(RuntimeError):
        lm.complete(__import__("lm15").Request(model="grok-4", messages=(Message.user("hi"),)))
    assert calls == [1] and transport.requests == []  # the callable was tried once; nothing else was consulted or sent


def test_mode_a_subscription_first_then_blocks_after_failure(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    env = {"XAI_API_KEY": SENTINEL}
    # never connected: the env key works (R2: key-only users keep working)
    assert "env" in LMRouter(RouterConfig(env=env)).lm("xai:grok-4").credential_origin()
    auth.login("xai", "device", ui=ScriptUI())
    # subscription beats the ambient key
    assert "stored local login" in LMRouter(RouterConfig(env=env)).lm("xai:grok-4").credential_origin()
    # signed out: the ambient key is blocked and the error says so
    auth.logout("xai")
    with pytest.raises(NotConfiguredError) as info:
        LMRouter(RouterConfig(env=env)).lm("xai:grok-4")
    message = str(info.value)
    assert "signed out" in message and "$XAI_API_KEY is set but is used only when passed explicitly" in message
    no_secret(info.value)
    # explicit key after logout is deliberate
    assert "explicit" in LMRouter(RouterConfig(env=env, api_keys={"xai": "k"})).lm("xai:grok-4").credential_origin()
    # expired without refresh: unusable, blocks too
    (sandbox / "credentials.json").write_text(json.dumps({"xai": {"type": "oauth", "access": SENTINEL, "expires": 1}}), encoding="utf-8")
    with pytest.raises(NotConfiguredError) as info:
        LMRouter(RouterConfig(env=env)).lm("xai:grok-4")
    assert "expired and cannot be renewed" in str(info.value)
    no_secret(info.value)


def test_doctor_mirrors_managed_and_legacy_selection(sandbox: Path) -> None:
    from lm15.doctor import explain_auth

    clock = Clock()
    auth = make_auth(sandbox, clock)
    auth.login("xai", "device", ui=ScriptUI())
    report = explain_auth("xai", config=RouterConfig(auth=auth, env={"XAI_API_KEY": SENTINEL}))
    assert [s.kind for s in report.steps] == ["api_keys", "connection", "env:XAI_API_KEY"]
    assert [s.state for s in report.steps] == ["absent", "selected", "shadowed"] and report.configured
    no_secret(report)
    auth.logout("xai")
    legacy = explain_auth("xai", env={"XAI_API_KEY": SENTINEL})
    assert [s.state for s in legacy.steps] == ["absent", "absent", "shadowed"] and not legacy.configured


def test_legacy_login_xai_uses_the_managed_flow(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lm15.auth as legacy
    import lm15.login.manager as manager

    server = FakeXai()
    monkeypatch.setattr(manager.Auth, "__init__", _patched_init(server))
    echoed: list[str] = []
    credential = legacy.login_xai(echo=echoed.append)
    assert credential.access_token == f"{ACCESS}1" and echoed == ["Open https://auth.x.ai/device and enter code: ABCD-1234"]
    assert legacy.xai_stored_state() == "usable"


def _patched_init(server):
    original = Auth.__init__

    def init(self, store, **kwargs):
        kwargs.setdefault("opener", server)
        original(self, store, **kwargs)

    return init


# ─── MA-058/059/060 connect() and the bound client ────────────────────


def _models_reply() -> FakeResponse:
    body = {"object": "list", "data": [{"id": "grok-4", "object": "model"}, {"id": "grok-3-mini", "object": "model"}]}
    return FakeResponse(200, body=json.dumps(body).encode())


def _chat_reply(text: str = "hello") -> FakeResponse:
    body = {"id": "x", "object": "chat.completion", "model": "grok-4",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    return FakeResponse(200, body=json.dumps(body).encode())


def test_connect_saves_login_before_picker_and_binds(sandbox: Path) -> None:
    from lm15.interactive import connect

    clock = Clock()
    auth = make_auth(sandbox, clock)
    transport = FakeTransport([_models_reply(), _chat_reply()])
    ui = ScriptUI([pick("xai"), pick("device"), pick("grok-4")])
    lm = connect(auth=auth, ui=ui, router_config=RouterConfig(transport=transport))
    assert lm.selection.routed == "xai:grok-4" and lm.connection.kind == "account"
    reply = lm.complete(messages=[Message.user("hi")])
    assert reply.text == "hello"
    sent = dict(transport.requests[-1].headers)
    assert sent["Authorization"] == f"Bearer {ACCESS}1"
    # the picker was cancelled on a second connect: the login stays saved
    ui2 = ScriptUI([pick(lm.connection.id), lambda p: (_ for _ in ()).throw(KeyboardInterrupt())])
    with pytest.raises(AuthOperationError):
        connect(auth=auth, ui=ui2, router_config=RouterConfig(transport=FakeTransport([_models_reply()])))
    assert auth.status("xai").presence == "saved"


def test_connect_offers_env_key_as_explicit_choice_only(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lm15.interactive import connect

    monkeypatch.setenv("GROQ_API_KEY", "gsk-env")
    auth = Auth.memory()
    transport = FakeTransport([_models_reply(), _chat_reply()])
    ui = ScriptUI([pick("groq"), pick("env"), pick("llama")])
    ui.answers[-1] = lambda p: p.options[0].id
    lm = connect(auth=auth, ui=ui, router_config=RouterConfig(transport=transport))
    method_prompt = ui.prompts[1]
    assert isinstance(method_prompt, SelectPrompt) and {o.id for o in method_prompt.options} == {"api_key", "env"}
    assert lm.connection.method_id == "env"
    document = auth.store.read()
    assert document["groq"] == {"type": "env", "name": "GROQ_API_KEY"}  # the name, never the value
    lm.complete(messages=[Message.user("hi")])
    assert dict(transport.requests[-1].headers)["Authorization"] == "Bearer gsk-env"


def test_connect_refuses_without_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    from lm15.interactive import connect

    monkeypatch.setattr("lm15.interactive._interactive_terminal", lambda: False)
    with pytest.raises(AuthOperationError) as info:
        connect(auth=Auth.memory())
    assert info.value.reason == "interaction_required"


def test_bound_client_is_pinned_and_thin(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    first = auth.login("xai", "device", ui=ScriptUI())
    selection = ModelSelection(provider="xai", model="grok-4", connection_id=first.id,
                               identity_generation=first.identity_generation)
    transport = FakeTransport([_chat_reply("one"), _chat_reply("two")])
    client = BoundClient(auth, selection, router_config=RouterConfig(transport=transport))
    request = client.request("hi")
    assert request.model == "xai:grok-4" and request.messages[0].role == "user"
    assert client.complete(request).text == "one"
    with pytest.raises(AuthOperationError) as info:
        client.complete(request.__class__(model="xai:grok-3", messages=request.messages))
    assert info.value.reason == "selection_mismatch"
    # renewal follows the same id; replacement does not
    clock.advance(3400)
    assert client.complete(messages=[Message.user("again")]).text == "two"
    auth.login("xai", "device", ui=ScriptUI(), replace=first.id)
    with pytest.raises(AuthOperationError) as info:
        client.complete(messages=[Message.user("x")])
    assert info.value.reason == "connection_changed"
    auth.logout("xai")
    with pytest.raises(AuthOperationError) as info:
        client.complete(messages=[Message.user("x")])
    assert info.value.reason == "login_required"
    client.close()
    assert auth.status("xai").presence == "absent"  # close is not logout; logout was explicit above


def test_bound_client_keyword_model_cannot_override_selection(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    connection = auth.login("xai", "device", ui=ScriptUI())
    selection = ModelSelection(provider="xai", model="grok-4", connection_id=connection.id,
                               identity_generation=connection.identity_generation)
    transport = FakeTransport([])
    client = BoundClient(auth, selection, router_config=RouterConfig(transport=transport))

    assert client.request("hi", model="grok-4").model == "xai:grok-4"
    assert client.request("hi", model="xai:grok-4").model == "xai:grok-4"
    for method in ("request", "plan", "complete", "stream"):
        with pytest.raises(AuthOperationError) as info:
            getattr(client, method)(messages="hi", model="grok-3")
        assert info.value.reason == "selection_mismatch"
    assert transport.requests == []


def test_model_choices_reject_replacement_before_catalog_request(monkeypatch):
    from lm15 import router as router_module

    auth = Auth.memory()
    original = auth.set_api_key("groq", "old-test-key")
    transport = FakeTransport([_models_reply()])
    router_type = router_module.LMRouter

    def replace_before_router(config):
        auth.set_api_key("groq", "new-test-key", replace=original.id)
        return router_type(config)

    monkeypatch.setattr(router_module, "LMRouter", replace_before_router)
    with pytest.raises(AuthOperationError) as error:
        model_choices(auth, "groq", refresh=True, router_config=RouterConfig(transport=transport))
    assert error.value.reason == "connection_changed"
    assert transport.requests == []


def test_model_choices_carry_provenance_and_capability_filter(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    auth.login("xai", "device", ui=ScriptUI())
    choices = model_choices(auth, "xai", refresh=True, router_config=RouterConfig(transport=FakeTransport([_models_reply()])))
    assert [c.model for c in choices] == ["grok-4", "grok-3-mini"] and all(c.source == "provider" for c in choices)
    assert model_choices(auth, "xai") == ()  # no application registry: nothing invented
    strict = model_choices(auth, "xai", refresh=True, capability="structured-output",
                           router_config=RouterConfig(transport=FakeTransport([_models_reply()])))
    assert strict == ()  # unknown is not offered without opt-in
    with pytest.raises(AuthOperationError):
        model_choices(auth, "gemini")


# ─── MA-043/044 secrecy ───────────────────────────────────────────────


def test_no_secret_in_any_public_rendering(sandbox: Path) -> None:
    clock = Clock()
    auth = make_auth(sandbox, clock)
    connection = auth.login("xai", "device", ui=ScriptUI())
    no_secret(auth, auth.store, connection, auth.status("xai"), auth.connections(), auth.request_auth("xai"))
    error = AuthOperationError("x", reason="login_required")
    no_secret(error)
    assert error.code == "auth_operation" and error.reason == "login_required"
    with pytest.raises(ValueError):
        AuthOperationError("x", reason="made-up")


def test_terminal_ui_prompts_and_does_not_open_browser_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    import io as _io

    out = _io.StringIO()
    answers = iter(["2", "pasted"])
    ui = TerminalUI(out=out, inp=lambda _prompt: next(answers))
    ui.notify(AuthUrlNotice(url="https://example.com/auth", instructions="open it"))
    ui.notify(DeviceCodeNotice(user_code="AB-12", verification_url="https://example.com/d", expires_in_s=600, interval_s=5))
    assert ui.prompt(SelectPrompt("m", "pick", (__import__("lm15.login.types", fromlist=["SelectOption"]).SelectOption("a", "A"),
                                                __import__("lm15.login.types", fromlist=["SelectOption"]).SelectOption("b", "B")))) == "b"
    assert ui.prompt(ManualCodePrompt("r", "paste")) == "pasted"
    text = out.getvalue()
    assert "https://example.com/auth" in text and "AB-12" in text


# ─── external sources (R1): read in place, never copied ───────────────


def test_external_claude_cli_source_reads_in_place(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lm15.auth as legacy

    claude = sandbox / "claude.json"
    claude.write_text(json.dumps({"claudeAiOauth": {"accessToken": ACCESS, "refreshToken": REFRESH,
                                                    "expiresAt": int(time.time() * 1000) + 3_600_000}}), encoding="utf-8")
    monkeypatch.setattr(legacy, "CLAUDE_CODE_CREDENTIALS_PATH", claude)
    auth = Auth.memory()
    with pytest.raises(AuthOperationError):
        auth.request_auth("claude-code")  # a managed scope does not borrow implicitly
    connection = auth.configure("claude-code", method="external:claude-code-cli")
    assert connection.kind == "account"
    assert auth.store.read()["claude-code"] == {"type": "external", "source": "claude-code-cli"}  # no token copied
    assert auth.request_auth("claude-code").credential.value == ACCESS
    auth.logout("claude-code")
    assert json.loads(claude.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == ACCESS  # the tool's file is untouched


@pytest.mark.parametrize(("field", "value"), [
    ("generation", "bad"), ("revision", []), ("settings", 1),
    *[("connection_id", value) for value in ([], {}, 1, False, "", " ")],
    ("previous_ids", [[]]), ("routes", [1]),
    ("settings", {"project": ["bad"], "location": "us-central1"}),
    ("routes", 1), ("attempt", "bad"),
    ("verification", {}), ("verification", {"foo": "bar"}),
    ("verification", {"result": "invalid"}), ("verification", {"result": []}),
    ("verification", {"result": "valid", "detail": 1}),
    ("attempt", {"id": "old", "started_at_s": "bad", "lifetime_s": 900}),
    *[("attempt", {"id": "old", "started_at_s": value, "lifetime_s": 900}) for value in (True, False)],
    ("attempt", {"id": "old", "started_at_s": 0, "lifetime_s": "bad"}),
    *[("attempt", {"id": "old", "started_at_s": 0, "lifetime_s": value}) for value in (0, -1, False)],
])
def test_malformed_slot_fields_raise_storage_error(sandbox, field, value):
    path = sandbox / "credentials.json"
    data = {"_lm15": {"version": 1, "slots": {"groq": {field: value}}}}
    original = json.dumps(data)
    path.write_text(original, encoding="utf-8")
    auth = Auth.local(path)
    for inspect in (lambda: auth.status("groq"), auth.connections):
        with pytest.raises(AuthOperationError) as caught:
            inspect()
        assert caught.value.reason == "storage_unavailable"
    assert path.read_text(encoding="utf-8") == original


def test_cancel_signals_original_event_before_new_attempt(monkeypatch):
    auth = Auth.memory()
    first, second = threading.Event(), threading.Event()
    auth._reserve("xai", "first", None, 900)
    auth._active["xai"] = first
    mutate = auth.store.mutate

    def replacement_after_unlock(fn):
        result = mutate(fn)
        auth._active["xai"] = second
        return result

    monkeypatch.setattr(auth.store, "mutate", replacement_after_unlock)
    assert auth.cancel_login("xai") == "cancelled"
    assert first.is_set()
    assert not second.is_set()


def test_cancel_is_checked_inside_commit_transaction(monkeypatch):
    auth = Auth.memory()
    cancel = threading.Event()
    mutate = auth.store.mutate

    def cancel_before_commit(fn):
        if fn.__name__ == "commit":
            cancel.set()
        return mutate(fn)

    monkeypatch.setattr(auth.store, "mutate", cancel_before_commit)
    with pytest.raises(LoginCancelled):
        auth.login("groq", "api_key", ui=ScriptUI(), answers={"key": "test-key"}, cancel=cancel)
    assert auth.status("groq").presence == "absent"


def test_verification_does_not_mark_replacement_connection(monkeypatch):
    from lm15 import router as router_module

    auth = Auth.memory()
    original = auth.set_api_key("groq", "old-test-key")

    class Router:
        def __init__(self, config):
            pass

        def lm(self, model):
            return self

        def list_models(self):
            auth.set_api_key("groq", "new-test-key", replace=original.id)
            return []

        def close(self):
            pass

    monkeypatch.setattr(router_module, "LMRouter", Router)
    assert auth.verify("groq").result == "valid"
    record = auth.store.read()["_lm15"]["slots"]["groq"]
    assert record["connection_id"] != original.id
    assert "verification" not in record


@pytest.mark.asyncio
async def test_async_cancel_returns_while_prompt_is_blocked():
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    entered, release, finished = (threading.Event() for _ in range(3))

    class BlockingUI(ScriptUI):
        def prompt(self, prompt):
            entered.set()
            assert release.wait(5)
            return "test-key"

    original = auth.sync.login

    def tracked(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    auth.sync.login = tracked
    task = asyncio.create_task(auth.login("groq", "api_key", ui=BlockingUI()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not finished.is_set()
        assert not auth.sync.store.read().get("_lm15", {}).get("slots", {}).get("groq", {}).get("attempt")
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert auth.sync.status("groq").presence == "absent"


def test_cancelled_prompt_does_not_hold_asyncio_run_shutdown():
    import subprocess
    import sys

    script = """
import asyncio
import threading
from lm15.login import AsyncAuth

entered = threading.Event()

class BlockingUI:
    def prompt(self, prompt):
        entered.set()
        threading.Event().wait()
    def notify(self, notice):
        pass

async def main():
    auth = AsyncAuth.memory()
    task = asyncio.create_task(auth.login("groq", "api_key", ui=BlockingUI()))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert not auth.sync.store.read()["_lm15"]["slots"]["groq"].get("attempt")

asyncio.run(main())
print("shutdown completed")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "shutdown completed"


@pytest.mark.asyncio
async def test_async_cancel_prevents_late_reservation(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    entered, release, finished = (threading.Event() for _ in range(3))
    original = auth.sync._reserve

    def delayed_reserve(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(auth.sync, "_reserve", delayed_reserve)
    task = asyncio.create_task(auth.login("groq", "api_key", ui=ScriptUI(), answers={"key": "test-key"}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not auth.sync.store.read().get("_lm15", {}).get("slots", {}).get("groq", {}).get("attempt")
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert not auth.sync.store.read().get("_lm15", {}).get("slots", {}).get("groq", {}).get("attempt")
    assert auth.sync.status("groq").presence == "absent"


def test_cancel_between_reservation_and_login_return_signals_event(monkeypatch):
    auth = Auth.memory()
    mutate = auth.store.mutate
    ui = ScriptUI()

    def cancel_after_reserve(fn):
        result = mutate(fn)
        if fn.__name__ == "reserve":
            assert auth.cancel_login("groq") == "cancelled"
        return result

    monkeypatch.setattr(auth.store, "mutate", cancel_after_reserve)
    with pytest.raises(LoginCancelled):
        auth.login("groq", "api_key", ui=ui)
    assert ui.prompts == []
    assert not auth._active
    assert auth.status("groq").presence == "absent"


@pytest.mark.asyncio
async def test_repeated_async_cancel_waits_for_reservation_cleanup(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    prompted, release_prompt, cleanup_started, allow_cleanup = (threading.Event() for _ in range(4))
    original = auth.sync._release

    class BlockingUI(ScriptUI):
        def prompt(self, prompt):
            prompted.set()
            assert release_prompt.wait(5)
            return "test-key"

    def delayed_release(*args, **kwargs):
        cleanup_started.set()
        assert allow_cleanup.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(auth.sync, "_release", delayed_release)
    task = asyncio.create_task(auth.login("groq", "api_key", ui=BlockingUI()))
    try:
        assert await asyncio.to_thread(prompted.wait, 5)
        task.cancel()
        assert await asyncio.to_thread(cleanup_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not auth.sync.store.read()["_lm15"]["slots"]["groq"].get("attempt")
    finally:
        allow_cleanup.set()
        release_prompt.set()


@pytest.mark.asyncio
async def test_async_cancel_surfaces_reservation_cleanup_failure(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    prompted, release_prompt = threading.Event(), threading.Event()
    mutate = auth.sync.store.mutate

    class BlockingUI(ScriptUI):
        def prompt(self, prompt):
            prompted.set()
            assert release_prompt.wait(5)
            return "test-key"

    def failing_release(fn):
        if fn.__name__ == "release":
            raise AuthOperationError("cleanup write failed", reason="storage_unavailable")
        return mutate(fn)

    monkeypatch.setattr(auth.sync.store, "mutate", failing_release)
    task = asyncio.create_task(auth.login("groq", "api_key", ui=BlockingUI()))
    try:
        assert await asyncio.to_thread(prompted.wait, 5)
        task.cancel()
        with pytest.raises(AuthOperationError) as caught:
            await asyncio.wait_for(task, 1)
        assert caught.value.reason == "storage_unavailable"
        assert auth.sync.store.read()["_lm15"]["slots"]["groq"].get("attempt")
    finally:
        release_prompt.set()


@pytest.mark.asyncio
async def test_cancelled_daemon_result_does_not_log_expected_exception(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    prompted, release_prompt = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    delivered = asyncio.Event()
    errors = []
    previous_handler = loop.get_exception_handler()
    schedule = loop.call_soon_threadsafe

    def tracked_schedule(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "deliver":
            def deliver_and_signal():
                callback(*args)
                delivered.set()
            return schedule(deliver_and_signal, **kwargs)
        return schedule(callback, *args, **kwargs)

    class BlockingUI(ScriptUI):
        def prompt(self, prompt):
            prompted.set()
            assert release_prompt.wait(5)
            return "test-key"

    monkeypatch.setattr(loop, "call_soon_threadsafe", tracked_schedule)
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    task = asyncio.create_task(auth.login("groq", "api_key", ui=BlockingUI()))
    try:
        assert await asyncio.to_thread(prompted.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release_prompt.set()
        await asyncio.wait_for(delivered.wait(), 1)
        await asyncio.sleep(0)
        assert errors == []
    finally:
        release_prompt.set()
        loop.set_exception_handler(previous_handler)


def test_configure_commit_failure_releases_reservation_for_retry(monkeypatch):
    auth = Auth.memory()
    mutate = auth.store.mutate
    fail_once = True

    def fail_commit(fn):
        nonlocal fail_once
        if fn.__name__ == "commit" and fail_once:
            fail_once = False
            raise AuthOperationError("commit write failed", reason="storage_unavailable")
        return mutate(fn)

    monkeypatch.setattr(auth.store, "mutate", fail_commit)
    with pytest.raises(AuthOperationError) as caught:
        auth.set_api_key("groq", "test-key")
    assert caught.value.reason == "storage_unavailable"
    assert not auth.store.read()["_lm15"]["slots"]["groq"].get("attempt")
    assert auth.set_api_key("groq", "test-key").provider == "groq"


def test_configure_cleanup_does_not_mask_commit_error(monkeypatch):
    auth = Auth.memory()
    original = AuthOperationError("original commit failure", reason="storage_unavailable")

    def fail_commit(*args, **kwargs):
        raise original

    mutate = auth.store.mutate

    def fail_cleanup(fn):
        if fn.__name__ == "release":
            raise AuthOperationError("cleanup failure", reason="storage_unavailable")
        return mutate(fn)

    monkeypatch.setattr(auth, "_commit", fail_commit)
    monkeypatch.setattr(auth.store, "mutate", fail_cleanup)
    with pytest.raises(AuthOperationError) as caught:
        auth.set_api_key("groq", "test-key")
    assert caught.value is original


@pytest.mark.asyncio
async def test_native_cancel_after_commit_admission_preserves_committed_connection(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    admitted, allow_write = threading.Event(), threading.Event()
    mutate = auth.sync.store.mutate

    def pause_before_write(fn):
        if fn.__name__ != "commit":
            return mutate(fn)

        def paused(document):
            replacement = fn(document)
            admitted.set()
            assert allow_write.wait(5)
            return replacement

        return mutate(paused)

    monkeypatch.setattr(auth.sync.store, "mutate", pause_before_write)
    task = asyncio.create_task(auth.login("groq", "api_key", ui=ScriptUI(), answers={"key": "test-key"}))
    try:
        assert await asyncio.to_thread(admitted.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        allow_write.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        # AUTH-19: native cancellation can lose result delivery after commit wins.
        assert auth.sync.status("groq").presence == "saved"
        assert auth.sync.cancel_login("groq") == "complete"
    finally:
        allow_write.set()


@pytest.mark.parametrize("material", [
    {"type": "api_key"}, {"type": "api_key", "key": []},
    {"type": "api_key", "key": " "}, {"type": "env"},
    {"type": "env", "name": 1}, {"type": "external"},
    {"type": "cloud", "named": []}, {"type": "local", "base_url": []},
])
def test_malformed_recipe_material_raises_storage_error(sandbox, material):
    path = sandbox / "credentials.json"
    auth = Auth.local(path)
    auth.set_api_key("openrouter", "test-key")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["openrouter"] = material
    original = json.dumps(data)
    path.write_text(original, encoding="utf-8")
    for inspect in (lambda: auth.status("openrouter"), auth.connections,
                    lambda: auth.request_auth("openrouter")):
        with pytest.raises(AuthOperationError) as caught:
            inspect()
        assert caught.value.reason == "storage_unavailable"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("material", [
    {"type": "oauth"}, {"type": "oauth", "access": []}, {"type": "oauth", "access": " "},
    {"type": "oauth", "access": "test-token", "refresh": {}},
    {"type": "oauth", "access": "test-token", "accountId": []},
    {"type": "oauth", "access": "test-token", "expires": float("nan")},
    *[{"type": "oauth", "access": "test-token", field: 10**400}
      for field in ("expires", "issued_at", "lifetime_s")],
    {"type": "oauth", "access": "test-token", "issued_at": True},
    {"type": "oauth", "access": "test-token", "lifetime_s": "3600"},
])
def test_malformed_oauth_material_raises_storage_error(sandbox, material):
    path = sandbox / "credentials.json"
    original = json.dumps({"xai": material})
    path.write_text(original, encoding="utf-8")
    auth = Auth.local(path)
    for inspect in (lambda: auth.status("xai"), auth.connections, lambda: auth.request_auth("xai")):
        with pytest.raises(AuthOperationError) as caught:
            inspect()
        assert caught.value.reason == "storage_unavailable"
    assert path.read_text(encoding="utf-8") == original


def test_oauth_material_without_optional_fields_is_valid():
    from lm15.login.store import validate_document

    document = {"xai": {"type": "oauth", "access": "test-token"}}
    assert validate_document(document, where="test") == document



@pytest.mark.parametrize("material", [{}, {"type": None}, {"type": []}, {"type": "unknown"}])
def test_unknown_material_type_raises_storage_error(sandbox, material):
    path = sandbox / "credentials.json"
    original = json.dumps({"xai": material})
    path.write_text(original, encoding="utf-8")
    auth = Auth.local(path)
    for inspect in (lambda: auth.status("xai"), auth.connections, lambda: auth.request_auth("xai")):
        with pytest.raises(AuthOperationError) as caught:
            inspect()
        assert caught.value.reason == "storage_unavailable"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_unknown_provider_native_cancellation_is_preserved(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    entered, release, finished = (threading.Event() for _ in range(3))
    original = auth.sync.login

    def delayed(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5)
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(auth.sync, "login", delayed)
    task = asyncio.create_task(auth.login("no-such-provider"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert auth.sync.store.read() == {}
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)


@pytest.mark.parametrize("material", [
    {"type": "external", "source": "bogus"},
    {"type": "cloud", "named": "bogus"},
])
def test_unknown_recipe_discriminator_raises_storage_error(sandbox, material):
    path = sandbox / "credentials.json"
    original = json.dumps({"vertex": material})
    path.write_text(original, encoding="utf-8")
    auth = Auth.local(path)
    for inspect in (lambda: auth.status("vertex"), auth.connections,
                    lambda: auth.request_auth("vertex")):
        with pytest.raises(AuthOperationError) as caught:
            inspect()
        assert caught.value.reason == "storage_unavailable"
    assert path.read_text(encoding="utf-8") == original


def test_supported_recipe_discriminators_validate_without_probing():
    from lm15.features import NAMED_CREDENTIALS
    from lm15.login.flows.recipes import EXTERNAL_SOURCES
    from lm15.login.store import validate_document

    for kind, field, values in (("external", "source", EXTERNAL_SOURCES),
                                ("cloud", "named", NAMED_CREDENTIALS)):
        for value in values:
            provider = EXTERNAL_SOURCES[value][0] if kind == "external" else "vertex"
            document = {provider: {"type": kind, field: value}}
            assert validate_document(document, where="test") == document


@pytest.mark.parametrize("expires", [10**100, 1e308, -1e308])
def test_oauth_expiry_outside_datetime_range_is_storage_error(sandbox, expires):
    test_malformed_oauth_material_raises_storage_error(
        sandbox, {"type": "oauth", "access": "test-token", "expires": expires}
    )


@pytest.mark.parametrize("material", [
    {"type": "external", "source": "codex-cli"},
    {"type": "env", "name": "UNRELATED_TEST_SECRET"},
])
def test_cross_provider_recipe_is_rejected_before_resolution(sandbox, material):
    path = sandbox / "credentials.json"
    original = json.dumps({"openrouter": material})
    path.write_text(original, encoding="utf-8")
    auth = Auth.local(path)
    with pytest.raises(AuthOperationError) as caught:
        auth.request_auth("openrouter")
    assert caught.value.reason == "storage_unavailable"
    assert path.read_text(encoding="utf-8") == original


def test_configure_rejects_undeclared_environment_variable(monkeypatch):
    monkeypatch.setenv("UNRELATED_TEST_SECRET", "never-send-this-test-value")
    auth = Auth.memory()
    with pytest.raises(AuthOperationError):
        auth.configure("openrouter", method="env", answers={"name": "UNRELATED_TEST_SECRET"})
    assert "openrouter" not in auth.store.read()


@pytest.mark.parametrize("canonical_first", [False, True])
def test_canonical_provider_settings_win_independent_of_insertion_order(canonical_first):
    from lm15.router import _settings_entry

    entries = [("vertex_anthropic", {"project": "alias-project"}),
               ("vertex-anthropic", {"project": "canonical-project"})]
    if canonical_first:
        entries.reverse()
    assert _settings_entry(RouterConfig(settings=dict(entries)), "vertex-anthropic") == {
        "project": "canonical-project"
    }



@pytest.mark.parametrize("method", ["bound", "verify", "catalog"])
@pytest.mark.parametrize("override", ["api_keys", "credentials"])
def test_saved_identity_ignores_router_credential_overrides(method, override):
    from lm15.features import NAMED_CREDENTIALS

    auth = Auth.memory()
    connection = auth.set_api_key("groq", "saved-test-key")
    transport = FakeTransport([_chat_reply("ok") if method == "bound" else _models_reply()])
    value = "other-test-key" if override == "api_keys" else next(iter(NAMED_CREDENTIALS))
    config = RouterConfig(transport=transport, **{override: {"groq": value}})
    if method == "bound":
        selection = ModelSelection(provider="groq", model="test", connection_id=connection.id,
                                   identity_generation=connection.identity_generation)
        client = BoundClient(auth, selection, router_config=config)
        try:
            assert client.complete(messages="hi").text == "ok"
            auth.logout("groq")
            with pytest.raises(AuthOperationError):
                client.complete(messages="hi")
        finally:
            client.close()
    elif method == "verify":
        assert auth.verify("groq", router_config=config).result == "valid"
    else:
        model_choices(auth, "groq", refresh=True, router_config=config)
    assert len(transport.requests) == 1
    assert dict(transport.requests[0].headers)["Authorization"] == "Bearer saved-test-key"


@pytest.mark.parametrize("provider,material", [
    ("openrouter", {"type": "oauth", "access": "test"}),
    ("groq", {"type": "oauth", "access": "test"}),
    ("xai", {"type": "api_key", "key": "test", "minted": True}),
    ("groq", {"type": "local"}),
    ("groq", {"type": "cloud", "named": "adc"}),
])
def test_provider_incompatible_material_is_storage_error(sandbox, provider, material):
    path = sandbox / "credentials.json"
    original = json.dumps({provider: material})
    path.write_text(original, encoding="utf-8")
    auth = Auth.local(path)
    for inspect in (lambda: auth.status(provider), auth.connections, lambda: auth.request_auth(provider)):
        with pytest.raises(AuthOperationError) as caught:
            inspect()
        assert caught.value.reason == "storage_unavailable"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_cancelled_async_mutation_is_drained(monkeypatch, failure):
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    auth.sync.set_api_key("groq", "test-key")
    original = auth.sync.logout
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def logout(provider):
        entered.set()
        assert release.wait(3)
        try:
            if failure:
                raise ValueError("mutation failed")
            return original(provider)
        finally:
            finished.set()

    monkeypatch.setattr(auth.sync, "logout", logout)
    operation = asyncio.create_task(auth.logout("groq"))
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
        assert (auth.sync.status("groq").presence == "absent") is (not failure)
    finally:
        release.set()
        try:
            await operation
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["connections", "status"])
async def test_read_only_async_auth_cancellation_is_prompt(monkeypatch, method):
    import asyncio
    from lm15.login import AsyncAuth

    auth = AsyncAuth.memory()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read(*args):
        entered.set()
        try:
            assert release.wait(3)
        finally:
            finished.set()

    monkeypatch.setattr(auth.sync, method, read)
    operation = asyncio.create_task(getattr(auth, method)(*(["groq"] if method == "status" else [])))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        operation.cancel()
        done, _pending = await asyncio.wait({operation}, timeout=0.1)
        assert operation in done
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        try:
            await operation
        except asyncio.CancelledError:
            pass


@pytest.mark.parametrize("method", ["connections", "status"])
def test_cancelled_auth_read_does_not_hold_runner_shutdown(method):
    import subprocess
    import sys

    script = """
import asyncio
import threading
import sys
from lm15.login import AsyncAuth

entered = threading.Event()
def blocked_read(*args):
    entered.set()
    threading.Event().wait()

async def main():
    auth = AsyncAuth.memory()
    method = sys.argv[1]
    setattr(auth.sync, method, blocked_read)
    operation = asyncio.create_task(getattr(auth, method)(*(["groq"] if method == "status" else [])))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    operation.cancel()
    try:
        await operation
    except asyncio.CancelledError:
        pass

asyncio.run(main())
print("shutdown completed")
"""
    result = subprocess.run([sys.executable, "-c", script, method], capture_output=True,
                            text=True, encoding="utf-8", timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "shutdown completed"


@pytest.mark.asyncio
async def test_async_read_workers_remain_bounded_after_cancellation(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth
    from lm15.login import manager

    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(manager, "_ASYNC_READ_SLOTS", slots)
    release = threading.Event()
    entered = []

    def read():
        entered.append(threading.get_ident())
        assert release.wait(3)
        return []

    auths = [AsyncAuth.memory() for _ in range(8)]
    for auth in auths:
        monkeypatch.setattr(auth.sync, "connections", read)
    tasks = [asyncio.create_task(auth.connections()) for auth in auths]
    try:
        await asyncio.sleep(0.05)
        assert len(entered) == 2
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(task.cancelled() for task in tasks)
        retry = asyncio.create_task(auths[0].connections())
        tasks.append(retry)
        await asyncio.sleep(0.05)
        assert len(entered) == 2
        retry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retry
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert await asyncio.wait_for(auths[0].connections(), timeout=1) == []


@pytest.mark.asyncio
async def test_abandoned_login_workers_are_bounded(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth
    from lm15.login import manager

    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(manager, "_ASYNC_LOGIN_SLOTS", slots)
    release, saturated = threading.Event(), threading.Event()
    entered = []

    class BlockingUI(ScriptUI):
        def prompt(self, prompt):
            entered.append(threading.get_ident())
            if len(entered) == 2:
                saturated.set()
            assert release.wait(3)
            return "test-key"

    auths = [AsyncAuth.memory() for _ in range(8)]
    tasks = [asyncio.create_task(auth.login("groq", "api_key", ui=BlockingUI())) for auth in auths]
    try:
        assert await asyncio.to_thread(saturated.wait, 1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(task.cancelled() for task in tasks)
        assert len(entered) == 2
        retry = asyncio.create_task(auths[0].login("groq", "api_key", ui=BlockingUI()))
        tasks.append(retry)
        await asyncio.sleep(0.05)
        assert len(entered) == 2
        retry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retry
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    connection = await asyncio.wait_for(auths[-1].login("groq", "api_key", ui=ScriptUI(), answers={"key": "test-key"}), timeout=1)
    assert connection.provider == "groq"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["status", "connections", "login"])
async def test_daemon_auth_workers_preserve_caller_context(monkeypatch, method):
    from contextvars import ContextVar
    from lm15.login import AsyncAuth

    tenant = ContextVar("tenant", default="default")
    auth = AsyncAuth.memory()
    monkeypatch.setattr(auth.sync, method, lambda *args, **kwargs: tenant.get())
    token = tenant.set("request-tenant")
    try:
        result = await getattr(auth, method)(*(["groq"] if method != "connections" else []))
        assert result == "request-tenant"
    finally:
        tenant.reset(token)


@pytest.mark.asyncio
async def test_queued_login_honors_external_cancel_event(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth, manager

    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(manager, "_ASYNC_LOGIN_SLOTS", slots)
    auth = AsyncAuth.memory()
    cancel = threading.Event()
    task = asyncio.create_task(auth.login("groq", "api_key", ui=ScriptUI(), cancel=cancel))
    try:
        await asyncio.sleep(0.03)
        assert not task.done()
        cancel.set()
        with pytest.raises(LoginCancelled):
            await asyncio.wait_for(task, timeout=1)
        assert auth.sync.connections() == ()
    finally:
        slots.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(LoginCancelled):
        await auth.login("groq", "api_key", ui=ScriptUI(), cancel=cancel)
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.parametrize("lifetime", [True, False, float("nan"), float("inf"), float("-inf")])
def test_login_rejects_boolean_lifetime(lifetime):
    auth = Auth.memory()
    with pytest.raises(ValueError, match="lifetime_s"):
        auth.login("groq", "api_key", ui=ScriptUI(), answers={"key": "test-key"}, lifetime_s=lifetime)
    assert auth.connections() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_managed_preparation_drains_repeated_cancellation(stream, fail):
    import asyncio
    from lm15.providers._managed import operation, async_stream

    entered, release = threading.Event(), threading.Event()
    completed = []
    invoked = []

    class Driver:
        def _managed_prepare(self):
            entered.set()
            assert release.wait(3)
            completed.append(True)
            if fail:
                raise ValueError("preparation failed")
            return self

        @operation
        async def complete(self):
            invoked.append(True)
            return "result"

        @async_stream
        async def stream(self):
            invoked.append(True)
            yield "result"

    driver = Driver()
    async def invoke():
        if stream:
            async for _ in driver.stream():
                pass
        else:
            await driver.complete()
    task = asyncio.create_task(invoke())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert completed == [True]
        assert invoked == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_login_stops_when_auth_closes(monkeypatch):
    import asyncio
    from lm15.login import AsyncAuth, manager

    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(manager, "_ASYNC_LOGIN_SLOTS", slots)
    auth = AsyncAuth.memory()
    task = asyncio.create_task(auth.login("groq", "api_key", ui=ScriptUI()))
    try:
        await asyncio.sleep(0.03)
        assert not task.done()
        auth.sync.close()
        with pytest.raises(AuthOperationError, match="closed"):
            await asyncio.wait_for(task, timeout=1)
    finally:
        slots.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(AuthOperationError, match="closed"):
        await auth.login("groq", "api_key", ui=ScriptUI())
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_owned_admission_drains_before_cancellation():
    import asyncio
    from lm15.providers._managed import _owned_thread

    entered, release = threading.Event(), threading.Event()
    completed = []
    def admission():
        entered.set()
        assert release.wait(3)
        completed.append(True)
    task = asyncio.create_task(_owned_thread(admission))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert completed == [True]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("custom", [
    {"type": "api_key", "key": "custom-test-key"},
    {"type": "custom-scheme", "payload": {"account": "test"}},
])
def test_managed_store_preserves_unrelated_provider_entries(sandbox, custom):
    path = sandbox / "credentials.json"
    data = {"xai": {"type": "api_key", "key": "xai-test-key"}, "custom-provider": custom}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    auth = Auth.local(path)
    auth.status("xai")
    assert {item.provider for item in auth.connections()} == {"xai"}
    assert path.read_bytes() == before
    auth.set_api_key("groq", "groq-test-key")
    assert json.loads(path.read_text(encoding="utf-8"))["custom-provider"] == custom


@pytest.mark.parametrize("stage", ["method", "store_reserve", "transaction"])
def test_login_cannot_reserve_after_auth_closes(monkeypatch, stage):
    auth = Auth.memory()
    if stage == "method":
        original = auth._choose_method
        def choose(*args, **kwargs):
            result = original(*args, **kwargs)
            auth.close()
            return result
        monkeypatch.setattr(auth, "_choose_method", choose)
    elif stage == "store_reserve":
        original = auth.store.reserve
        def reserve():
            original()
            auth.close()
        monkeypatch.setattr(auth.store, "reserve", reserve)
    else:
        original = auth.store.mutate
        def mutate(fn):
            auth.close()
            return original(fn)
        monkeypatch.setattr(auth.store, "mutate", mutate)
    with pytest.raises(AuthOperationError, match="closed"):
        auth.login("groq", "api_key", ui=ScriptUI(), answers={"key": "test-key"})
    assert auth.store.read() == {}
    assert auth._active == {}
