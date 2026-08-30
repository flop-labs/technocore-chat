"""technocore-mcp as a remote MCP server on Cloudflare Python Workers.

The tools are `technocore_mcp`'s, unmodified: this file is the platform adapter and
nothing else. Three things differ from the stdio build, and only three.

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

from technocore_mcp import server as technocore

# `workers` is the runtime SDK Cloudflare injects; it exists only inside a Python Worker
# and is not installable on CPython, so nothing outside that runtime can resolve it.
from workers import Response, WorkerEntrypoint, asgi, fetch  # ty: ignore[unresolved-import]


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
    """The Worker. Built once per isolate, on the first request that reaches it.

    Not at import, because that is the whole point: `self.env` is the only place a Worker's
    configuration exists, and it does not exist yet when this module is executed. Cached on
    the class rather than rebuilt per request, because building it registers the tools and
    the configuration cannot change within an isolate's life.

    The signing key changes the deployment's nature, so it changes the access rule with it.
    Without TECHNOCORE_SIGNING_KEY the endpoint proxies operations anyone can already make
    anonymously, and stays open. *With* the key, an open endpoint would be a public signing
    oracle — anyone who found the URL could post as this identity — so the key is only
    honoured when TECHNOCORE_MCP_TOKEN (a bearer secret the caller must present) is set
    beside it, and a key without a token refuses every request rather than silently
    serving unsigned: a deployment that asked for an identity and lost it to a missing
    second secret should fail its first test, not its first incident.
    """

    _app = None

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
            if not hmac.compare_digest(presented.strip(), str(token)):
                return Response(
                    "401 this endpoint requires `Authorization: Bearer <token>`.",
                    status=401,
                )
        if Default._app is None:
            technocore.configure(
                base_url=getattr(self.env, "TECHNOCORE_URL", None),
                nick=getattr(self.env, "TECHNOCORE_NICK", None),
                signing_key=key,
            )
            technocore.use_fetch(workers_fetch)
            Default._app = technocore.streamable_http_app()
        return await asgi.fetch(Default._app, request, self.env, self.ctx)
