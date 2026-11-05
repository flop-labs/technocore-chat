"""Execute the Worker transport with a controllable platform fetch and abort signal.

The SDK is injected by Cloudflare, so CPython substitutes only that boundary. The actual
Worker module runs unchanged; these checks distinguish a deadline from a timer that
abandons an await while the outbound request keeps running.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


class _Signal:
    def __init__(self, milliseconds):
        self.milliseconds = milliseconds
        self.aborted = asyncio.Event()
        self.timer = asyncio.get_running_loop().call_later(milliseconds / 1000, self.aborted.set)


@pytest.fixture()
def worker(monkeypatch):
    signals = []

    def timeout(milliseconds):
        signal = _Signal(milliseconds)
        signals.append(signal)
        return signal

    platform = types.ModuleType("workers")
    platform.__dict__.update(
        Response=object, WorkerEntrypoint=object, asgi=types.SimpleNamespace(), fetch=None
    )
    javascript = types.ModuleType("js")
    javascript.__dict__["AbortSignal"] = types.SimpleNamespace(timeout=timeout)
    monkeypatch.setitem(sys.modules, "workers", platform)
    monkeypatch.setitem(sys.modules, "js", javascript)
    path = Path(__file__).resolve().parents[2] / "mcp/worker/src/worker.py"
    spec = importlib.util.spec_from_file_location("worker_fetch_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, signals


async def _transfer(delay, signal, aborted):
    """The platform stops the active transfer when its AbortSignal fires."""
    if signal is None:
        await asyncio.sleep(delay)
        return
    try:
        await asyncio.wait_for(signal.aborted.wait(), delay)
    except TimeoutError:
        return
    aborted.append(True)
    raise RuntimeError("The operation was aborted due to timeout")


@pytest.mark.parametrize("method,body", [("GET", None), ("POST", "雪".encode())])
@pytest.mark.parametrize("phase,status", [("headers", 200), ("body", 200), ("body", 429)])
def test_worker_deadline_aborts_headers_and_success_or_error_bodies(
    worker, method, body, phase, status
):
    module, signals = worker
    calls, aborted = [], []

    async def fetch(url, **kwargs):
        calls.append((url, kwargs))
        signal = kwargs.get("signal")
        if phase == "headers":
            await _transfer(0.1, signal, aborted)

        async def text():
            if phase == "body":
                await _transfer(0.1, signal, aborted)
            return "the complete answer"

        return types.SimpleNamespace(status=status, text=text)

    module.fetch = fetch

    async def exercise():
        with pytest.raises(OSError, match="aborted due to timeout"):
            await module.workers_fetch(method, "https://origin.invalid/r/room", {}, body, 0.005)
        assert len(calls) == len(signals) == 1, "one outbound request, never a retry"
        assert aborted == [True], "the transfer itself must be aborted"
        assert calls[0][1]["signal"] is signals[0]
        assert signals[0].milliseconds == 5
        if body is not None:
            assert calls[0][1]["body"] == body.decode()

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [200, 404, 429, 503])
@pytest.mark.parametrize(
    "method,payload,timeout", [("GET", None, 30), ("POST", "雪".encode(), 30), ("GET", None, 330)]
)
def test_complete_worker_responses_preserve_status_body_and_request(
    worker, status, method, payload, timeout
):
    module, signals = worker
    calls = []
    body = "wait 7 seconds\n"

    async def fetch(url, **kwargs):
        calls.append((url, kwargs))

        async def text():
            return body

        return types.SimpleNamespace(status=status, text=text)

    module.fetch = fetch

    async def exercise():
        headers = {"User-Agent": "test"}
        assert await module.workers_fetch(
            method, "https://origin.invalid/r/room", headers, payload, timeout
        ) == (status, body)
        assert len(calls) == 1
        assert calls[0][1]["method"] == method
        assert calls[0][1]["headers"] is headers
        if payload is None:
            assert "body" not in calls[0][1]
        else:
            assert calls[0][1]["body"] == payload.decode()
        if signals:
            assert len(signals) == 1 and signals[0].milliseconds == timeout * 1000
            assert not signals[0].aborted.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_worker_ffi_failure_is_a_transport_error(worker, phase):
    module, _ = worker

    async def fetch(*args, **kwargs):
        if phase == "headers":
            raise RuntimeError("connection reset")

        async def text():
            raise RuntimeError("connection reset")

        return types.SimpleNamespace(status=200, text=text)

    module.fetch = fetch

    async def exercise():
        with pytest.raises(OSError, match="connection reset"):
            await module.workers_fetch("GET", "https://origin.invalid", {}, None, 30)

    asyncio.run(exercise())


def test_headers_and_body_share_one_total_deadline(worker):
    module, signals = worker
    aborted = []

    async def fetch(*args, **kwargs):
        signal = kwargs.get("signal")
        await _transfer(0.02, signal, aborted)

        async def text():
            await _transfer(0.02, signal, aborted)
            return "complete"

        return types.SimpleNamespace(status=200, text=text)

    module.fetch = fetch

    async def exercise():
        with pytest.raises(OSError, match="aborted due to timeout"):
            await module.workers_fetch("GET", "https://origin.invalid", {}, None, 0.03)
        assert len(signals) == 1
        assert aborted == [True]

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_worker_caller_cancellation_is_not_translated_to_an_http_error(worker, phase):
    module, _ = worker

    async def fetch(*args, **kwargs):
        if phase == "headers":
            raise asyncio.CancelledError

        async def text():
            raise asyncio.CancelledError

        return types.SimpleNamespace(status=200, text=text)

    module.fetch = fetch

    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await module.workers_fetch("GET", "https://origin.invalid", {}, None, 30)

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_worker_preserves_platform_oserror(worker, phase):
    module, _ = worker
    failure = OSError("platform abort")

    async def fetch(*args, **kwargs):
        if phase == "headers":
            raise failure

        async def text():
            raise failure

        return types.SimpleNamespace(status=200, text=text)

    module.fetch = fetch

    async def exercise():
        with pytest.raises(OSError) as caught:
            await module.workers_fetch("GET", "https://origin.invalid", {}, None, 30)
        assert caught.value is failure

    asyncio.run(exercise())
