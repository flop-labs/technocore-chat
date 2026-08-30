"""technocore-mcp as a remote MCP server on Cloudflare Python Workers.

The tools are `technocore_mcp`'s, unmodified: this file is the platform adapter and
nothing else. Two things differ from the stdio build, and only two.

**The fetch.** Python Workers run on Pyodide, which has no raw sockets — `urllib` there
does not fail at import, it fails at connect, in production. Outbound HTTP is the
platform's JavaScript `fetch`, reached over Pyodide's FFI, so `use_fetch` swaps the one
function in the package that touches the network. Everything above it — URL building,
which query keys survive, how an error body becomes a tool result — is shared code, so
the two deployments cannot disagree about what a tool answers.

**The server.** An ASGI app expects an ASGI *server* to own the socket; here the platform
owns it, and `workers.asgi.entrypoint` is the adapter. `uvicorn` is not installed and is
not installable (the SDK marks it `sys_platform != "emscripten"`), which is why nothing
here calls `run()`.

The endpoint is stateless streamable HTTP at `/mcp`. Stateless is not a compromise: every
tool call is one independent GET against the origin, nothing is held between them, and an
edge runtime may put the next request in a different isolate anyway. SSE is deprecated and
is not served.

Unauthenticated, deliberately: the service this fronts is public, unauthenticated and
world-writable, so an OAuth layer here would guard a door that has no wall beside it.
Rate limiting stays the origin's job, where it already is.
"""

from technocore_mcp import server as technocore

# `workers` is the runtime SDK Cloudflare injects; it exists only inside a Python Worker
# and is not installable on CPython, so nothing outside that runtime can resolve it.
from workers import asgi, fetch  # ty: ignore[unresolved-import]


async def workers_fetch(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    """The platform `fetch`, in the shape `technocore_mcp.fetch.Fetch` describes.

    An HTTP answer is a value whatever its status — the service puts the actionable part
    of a 429, a 409 or a 403 in the *body*, and raising on the status would throw exactly
    that away. Only a failure with no answer at all raises, as `OSError`, which is what
    the caller catches to say "cannot reach".

    `timeout` is accepted and not used: a Worker's outbound requests are bounded by the
    platform's own request lifetime, and `fetch` exposes no per-request deadline to set.
    """
    try:
        response = await fetch(url, method="GET", headers=headers)
    except OSError:
        # Pyodide already reports a failed fetch as `AbortError`, which is an `OSError`.
        raise
    except Exception as exc:  # anything else from the FFI: no HTTP answer happened
        raise OSError(str(exc)) from None
    # Not `raise_for_status()`: a 4xx/5xx body is the payload, and Pyodide reads it back
    # for any status — `text()` only refuses an aborted or already-consumed response.
    return response.status, await response.text()


technocore.use_fetch(workers_fetch)

Default = asgi.entrypoint(technocore.streamable_http_app())
