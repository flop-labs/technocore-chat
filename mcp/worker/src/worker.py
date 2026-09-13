"""technocore-mcp as a remote MCP server on Cloudflare Python Workers.

The tools are `technocore_mcp`'s, unmodified: this file is the platform adapter and
nothing else. Four things differ from the stdio build, and only four.

**The fetch.** Python Workers run on Pyodide, which has no raw sockets — `urllib` there
does not fail at import, it fails at connect, in production. Outbound HTTP is the
platform's JavaScript `fetch`, reached over Pyodide's FFI, so `use_fetch` swaps the one
function in the package that touches the network. Everything above it — URL building,
which query keys survive, how an error body becomes a tool result — is shared code, so
the two deployments cannot disagree about what a tool answers.

**The configuration.** A Worker has no process environment. `[vars]` and `wrangler
secret` arrive on the entrypoint's `env` binding, per request, so `TECHNOCORE_URL`,
`TECHNOCORE_NICK` and `TECHNOCORE_SIGNING_KEY` are read from there and applied with
`configure()` — which the stdio build gets from `os.environ` at import. Without this a
Worker would silently proxy the public instance while its wrangler.jsonc said otherwise.
The signing key additionally requires `TECHNOCORE_MCP_TOKEN`; see `Default` below.

**The server.** An ASGI app expects an ASGI *server* to own the socket; here the platform
owns it. `uvicorn` is not installed and is not installable (the SDK marks it
`sys_platform != "emscripten"`), which is why nothing here calls `run()`.

**The lifecycle.** Both halves of it are the platform's, not a preference. The package is
imported inside a request rather than at module scope, because the runtime forbids the
entropy the MCP SDK's import chain draws before the snapshot is taken — see
`_technocore()`. And the ASGI app is rebuilt per request rather than cached, because
`asgi.fetch` runs a full lifespan startup and shutdown around every call and the SDK's
session manager refuses to start twice — see `Default`. Between them these are the
difference between a Worker that serves and one that 500s, so neither is tidiable away.

The endpoint is stateless streamable HTTP at `/mcp`. Stateless is not a compromise: every
tool call is one independent GET against the origin, nothing is held between them, and an
edge runtime may put the next request in a different isolate anyway. SSE is deprecated and
is not served.

Unauthenticated by default, deliberately: the service this fronts is public,
unauthenticated and world-writable, so an auth layer in front of the anonymous tools
would guard a door that has no wall beside it. Rate limiting stays the origin's job,
where it already is. The one thing worth a wall is a signing key — see `Default`.
"""

import hmac
from typing import Any

# `workers` is the runtime SDK Cloudflare injects; it exists only inside a Python Worker
# and is not installable on CPython, so nothing outside that runtime can resolve it.
from workers import Response, WorkerEntrypoint, asgi, fetch  # ty: ignore[unresolved-import]

# `technocore_mcp` is NOT imported here, and the reason is a platform rule rather than a
# style preference — see `_technocore()`.


# Import the package at module scope if the runtime will allow it, so the MCP SDK lands in
# the snapshot instead of being re-imported by every cold isolate. See `_technocore()` for
# what this is working around and why one frozen value is the acceptable price.
# `Any` rather than the module type: this name is None until an import succeeds, and the
# whole point is that either import may be the one that does.
_module: Any = None
try:
    from _cloudflare.allow_entropy import (  # ty: ignore[unresolved-import]
        allow_bad_entropy_calls,
    )
except ImportError:  # not a Python Worker, or the private module moved
    pass
else:
    try:
        with allow_bad_entropy_calls(1):
            from technocore_mcp import server as _eager

        _module = _eager
    except Exception:
        # Budget exhausted, or the chain drew entropy this does not cover. Python drops a
        # module whose execution raised, so the lazy path below re-imports it cleanly.
        _module = None


def _technocore():
    """Import `technocore_mcp` inside a request, because it cannot be imported outside one.

    Python Workers snapshot the interpreter after top-level module execution and restore
    that snapshot per isolate, which is what makes cold starts fast. A snapshot taken
    after something drew random bytes would freeze those bytes into every future isolate,
    so the platform seeds the PRNG with a deliberate "poison seed" pre-snapshot and makes
    every entropy API raise until a request is being served. `os.urandom` outside a
    request context is therefore not a bug to work around; it is the platform refusing to
    hand out numbers it knows would be identical forever.

    The MCP SDK trips it at import: `mcp.server.lowlevel.server` imports
    `mcp.server._otel`, which imports `opentelemetry.context`, which calls `uuid4()` at
    module scope to mint a context key. Nothing in this repo asks for that entropy and
    nothing can decline it — one `import` three levels down is enough. With the import at
    top level the Worker does not start at all:

        OSError: [Errno 29] Cannot get entropy outside of request context

    and the failure is total rather than partial: it happens while the module is being
    executed, so there is no handler yet to return a 500 and every request 500s.

    Deferring the import to the first request buys a Worker that runs, but it is not free
    and the bill is larger than it looks: measured against the deployed Worker, a request
    that lands on a cold isolate spends 7-21 seconds importing the SDK, and isolates turn
    over often enough that most requests paid it. That is past the point where an MCP
    client gives up.

    So the module-level block above tries the import eagerly inside
    `allow_bad_entropy_calls`, the same mechanism Cloudflare's own SDK uses to let
    allowlisted packages (pydantic_core, cryptography, aiohttp, numpy.random) draw entropy
    during their import. Those patches fire on their own, so the one call this budget has
    to cover is the one nobody patched: `opentelemetry.context` minting a contextvar key
    with `uuid4()`. Freezing that into the snapshot means every isolate names that
    contextvar identically, which is harmless — it is a variable name, not a secret, and
    contextvars are per-isolate regardless. Nothing security-bearing is frozen: the signing
    key is a configured seed, not drawn, and nonces are drawn per request, where entropy is
    real.

    Measured on the deployed Worker, twelve back-to-back calls each way: deferred, a median
    of about 11s and a worst case of 21s; eager, a median of about 4s. A request that finds
    a warm isolate is ~0.2s under both, which is also what says the per-request app rebuild
    is not the cost — the cold path is. What remains after this fix is the platform
    restoring an 18 MB, 884-module snapshot, which is not something this file can shorten;
    it is the price of the MCP SDK on Pyodide. Deploy-time startup goes 750ms -> 2.7s,
    which is the same import moving into the snapshot where it belongs.

    This function is the fallback for when that does not work — a runtime without the
    private module, a budget that turns out to be too small, a future dependency that draws
    entropy nobody expected. Then the import happens here, in request context, slowly and
    correctly. `Default._configured` keeps it to once per isolate either way.
    """
    global _module
    if _module is None:
        from technocore_mcp import server as module

        _module = module
    return _module


async def workers_fetch(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, str]:
    """The platform `fetch`, in the shape `technocore_mcp.fetch.Fetch` describes.

    An HTTP answer is a value whatever its status — the service puts the actionable part
    of a 429, a 409 or a 403 in the *body*, and raising on the status would throw exactly
    that away. Only a failure with no answer at all raises, as `OSError`, which is what
    the caller catches to say "cannot reach".

    `timeout` is accepted and not used: a Worker's outbound requests are bounded by the
    platform's own request lifetime, and `fetch` exposes no per-request deadline to set.
    """
    try:
        if body is None:
            response = await fetch(url, method=method, headers=headers)
        else:
            # The body arrives as the exact bytes to send, encoded above the seam; the
            # JS fetch takes them as a string, decoded with the same UTF-8 they carry.
            response = await fetch(url, method=method, headers=headers, body=body.decode())
    except OSError:
        # Pyodide already reports a failed fetch as `AbortError`, which is an `OSError`.
        raise
    except Exception as exc:  # anything else from the FFI: no HTTP answer happened
        raise OSError(str(exc)) from None
    # Not `raise_for_status()`: a 4xx/5xx body is the payload, and Pyodide reads it back
    # for any status — `text()` only refuses an aborted or already-consumed response.
    return response.status, await response.text()


class Default(WorkerEntrypoint):
    """The Worker. Configured once per isolate, on the first request that reaches it.

    Not at import, because that is the whole point: `self.env` is the only place a Worker's
    configuration exists, and it does not exist yet when this module is executed.

    The *configuration* is cached on the class; the ASGI app is not, and that asymmetry is
    forced by the platform. `asgi.fetch` runs the app's full ASGI lifespan — startup before
    the request, shutdown after it — on every call, so it wants an app that has never been
    started. The MCP SDK's app starts a `StreamableHTTPSessionManager` in its lifespan, and
    that object refuses to be started twice:

        RuntimeError: StreamableHTTPSessionManager .run() can only be called once per
        instance. Create a new instance if you need to run again.

    A cached app therefore serves exactly one request per isolate and 500s on every one
    after it — the kind of bug that passes a smoke test and fails a second click. So the
    app is rebuilt per request, which is cheap and safe here: `streamable_http_app()`
    constructs a fresh session manager each call, and the expensive half — importing the
    package and registering the tools — happens once at module import and is held in
    `sys.modules` regardless. Stateless mode is what makes this free of consequence: there
    is no session state for a new app instance to have forgotten.

    The signing key changes the deployment's nature, so it changes the access rule with it.
    Without TECHNOCORE_SIGNING_KEY the endpoint proxies operations anyone can already make
    anonymously, and stays open. *With* the key, an open endpoint would be a public signing
    oracle — anyone who found the URL could post as this identity — so the key is only
    honoured when TECHNOCORE_MCP_TOKEN (a bearer secret the caller must present) is set
    beside it, and a key without a token refuses every request rather than silently
    serving unsigned: a deployment that asked for an identity and lost it to a missing
    second secret should fail its first test, not its first incident.
    """

    _configured = False

    async def fetch(self, request):
        key = getattr(self.env, "TECHNOCORE_SIGNING_KEY", None)
        token = getattr(self.env, "TECHNOCORE_MCP_TOKEN", None)
        if key and not token:
            return Response(
                "503 TECHNOCORE_SIGNING_KEY is set but TECHNOCORE_MCP_TOKEN is not. A "
                "signing key on an open endpoint is a public signing oracle, so this "
                "worker refuses to serve until a bearer token guards it: "
                "`wrangler secret put TECHNOCORE_MCP_TOKEN`, then send "
                "`Authorization: Bearer <token>` from your MCP client.",
                status=503,
            )
        if token:
            presented = (request.headers.get("Authorization") or "").removeprefix("Bearer ")
            # Compared as bytes, not str: `compare_digest` refuses two `str` arguments
            # unless both are ASCII, and raises `TypeError` rather than returning False.
            # The header is attacker-controlled, so `Authorization: Bearer café` would
            # take the auth gate out with an unhandled exception and answer a probe with a
            # 500 instead of a 401. It fails closed either way — the throw happens before
            # anything is served — but a crash is not an answer, and encoding both sides
            # keeps the comparison constant-time over the bytes that actually arrived.
            if not hmac.compare_digest(presented.strip().encode(), str(token).encode()):
                return Response(
                    "401 this endpoint requires `Authorization: Bearer <token>`.",
                    status=401,
                )
        technocore = _technocore()
        if not Default._configured:
            technocore.configure(
                base_url=getattr(self.env, "TECHNOCORE_URL", None),
                nick=getattr(self.env, "TECHNOCORE_NICK", None),
                signing_key=key,
            )
            technocore.use_fetch(workers_fetch)
            Default._configured = True
        return await asgi.fetch(technocore.streamable_http_app(), request, self.env, self.ctx)
