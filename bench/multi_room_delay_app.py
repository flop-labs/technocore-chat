"""Local-only latency wrapper for the production ASGI app.

BENCH_DELAY_MS is applied once, immediately before each room GET reaches the app. This is
an application-layer latency proxy, not a claim about real WAN/TLS/proxy behaviour. It adds
no route and must not be deployed.
"""

from __future__ import annotations

import asyncio
import os

from app import app as production_app


class DelayRoomGets:
    def __init__(self, app, delay_ms: float) -> None:
        self.app = app
        self.delay_seconds = delay_ms / 1000

    async def __call__(self, scope, receive, send) -> None:
        if (
            self.delay_seconds
            and scope["type"] == "http"
            and scope.get("method") == "GET"
            and scope.get("path", "").startswith("/r/")
        ):
            await asyncio.sleep(self.delay_seconds)
        await self.app(scope, receive, send)


app = DelayRoomGets(production_app, float(os.environ.get("BENCH_DELAY_MS", "0")))
