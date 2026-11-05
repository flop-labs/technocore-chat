"""Pre-router hiding for token-gated operational routes."""

import secrets

from starlette.requests import Request

import config


def stats_hidden(request: Request) -> bool:
    """Whether this caller must see the stats path as an ordinary missing route.

    Both values are bytes: Starlette exposes wire bytes as latin-1 text, while configured
    non-ASCII tokens are UTF-8. This keeps compare_digest constant-time without its
    non-ASCII string TypeError. The empty-token check must precede comparison because an
    unconfigured endpoint is hidden even though compare_digest(b"", b"") is true.
    """
    supplied = request.headers.get("x-stats-token", "").encode("latin-1")
    token = config.STATS_TOKEN
    return not token or not secrets.compare_digest(supplied, token.encode())


class HideStatsRoute:
    """Apply the stats gate after shared HTTP middleware but before routing."""

    def __init__(self, app, response_factory):
        self.app = app
        self.response_factory = response_factory

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] in {"/stats", "/stats/"}:
            if stats_hidden(Request(scope)):
                await self.response_factory()(scope, receive, send)
                return
        await self.app(scope, receive, send)
