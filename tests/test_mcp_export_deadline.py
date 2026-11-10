"""Exercise the Worker export adapter with a signal-aware platform transport."""

import ast
import asyncio
import codecs
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def export_adapter(fetch, abort_signal):
    # Execute the real adapter without booting the Cloudflare-only MCP entrypoint.
    source = Path(__file__).resolve().parents[1] / "mcp/worker/src/worker.py"
    tree = ast.parse(source.read_text())
    names = {"_chunk_bytes", "_export_seq", "workers_export_fetch"}
    tree.body = [node for node in tree.body if getattr(node, "name", None) in names]
    namespace = {
        "Any": Any,
        "codecs": codecs,
        "json": json,
        "fetch": fetch,
        "AbortSignal": abort_signal,
    }
    exec(compile(tree, str(source), "exec"), namespace)
    return namespace["workers_export_fetch"]


@pytest.mark.parametrize("stage", ["headers", "body", "error_body"])
def test_export_deadline_aborts_stalled_transport(stage):
    async def scenario():
        signals = []
        calls = []
        released = []

        class Signal:
            @staticmethod
            def timeout(milliseconds):
                event = asyncio.Event()
                handle = asyncio.get_running_loop().call_later(milliseconds / 1000, event.set)
                signals.append((milliseconds, event, handle))
                return event

        async def stall(signal):
            await (signal or asyncio.Event()).wait()
            raise RuntimeError("platform abort")

        async def fetch(url, **kwargs):
            calls.append((url, kwargs))
            signal = kwargs.get("signal")
            if stage == "headers":
                await stall(signal)

            async def read():
                await stall(signal)

            reader = SimpleNamespace(read=read, releaseLock=lambda: released.append(True))
            return SimpleNamespace(
                status=429 if stage == "error_body" else 200,
                headers={"X-Room-Generation": "7"},
                body=SimpleNamespace(getReader=lambda: reader),
                text=lambda: stall(signal),
            )

        adapter = export_adapter(fetch, Signal)
        try:
            with pytest.raises(OSError, match="platform abort"):
                await asyncio.wait_for(adapter("https://origin/export", {}, 0.01, None, 2), 1)
            assert len(calls) == 1  # No retry or partial page on timeout.
            assert signals[0][0] == 10
            assert calls[0][1]["signal"] is signals[0][1]
            assert released == ([True] if stage == "body" else [])
        finally:
            for _, _, handle in signals:
                handle.cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 429, 500])
def test_export_keeps_complete_refusal_bodies(status):
    async def scenario():
        signal = object()
        calls = []

        async def fetch(url, **kwargs):
            calls.append(kwargs)

            async def text():
                return "retry after 12 seconds"

            return SimpleNamespace(status=status, headers={}, text=text)

        adapter = export_adapter(fetch, SimpleNamespace(timeout=lambda ms: signal))
        assert await adapter("https://origin/export", {}, 2, None, 2) == (
            status,
            "retry after 12 seconds",
            {},
        )
        assert len(calls) == 1
        assert calls[0]["signal"] is signal

    asyncio.run(scenario())


@pytest.mark.parametrize("limit, cancelled", [(1, True), (5, False)])
def test_export_deadline_preserves_bounded_jsonl_and_generation(limit, cancelled):
    async def scenario():
        events = []
        signal = object()
        raw = '{"seq":1,"text":"é"}\n{"seq":2}\n'
        chunks = iter([raw.encode()[:19], raw.encode()[19:]])

        async def read():
            value = next(chunks, None)
            return SimpleNamespace(done=value is None, value=value)

        async def cancel():
            events.append("cancel")

        reader = SimpleNamespace(
            read=read, cancel=cancel, releaseLock=lambda: events.append("release")
        )

        async def fetch(url, **kwargs):
            assert kwargs["signal"] is signal
            assert kwargs["headers"] == {"Accept": "application/x-ndjson"}
            return SimpleNamespace(
                status=200,
                headers={"X-Room-Generation": "7"},
                body=SimpleNamespace(getReader=lambda: reader),
            )

        adapter = export_adapter(fetch, SimpleNamespace(timeout=lambda ms: signal))
        result = await adapter(
            "https://origin/export", {"Accept": "application/x-ndjson"}, 2, None, limit
        )
        assert result == (200, raw, {"X-Room-Generation": "7"})
        assert events == (["cancel", "release"] if cancelled else ["release"])

    asyncio.run(scenario())
