"""The one call in this package that touches the network, behind one seam.

Every tool is "build a request, send it once, hand back the body", so exactly one
function has to know how bytes are actually moved — and that is the one function whose
answer differs per platform:

* **CPython** (the `technocore-mcp` stdio server, `uvx`, the Docker image, the tests)
  has sockets, so `urllib_fetch` below is the whole implementation, stdlib only. It is
  the reason this package still resolves nothing but the MCP SDK.
* **Cloudflare Python Workers** runs on Pyodide, which has no raw sockets at all:
  outbound HTTP is the platform's JavaScript `fetch`, reached over the Pyodide FFI.
  `urllib` there does not fail at import — it fails at connect, in production — so
  `mcp/worker/src/worker.py` injects its own `Fetch` instead of using this one.

The seam is deliberately the *whole* transport and nothing else: method, URL, headers
and an optional body in; status and body text out. No exception translation, no URL
building, no JSON encoding — everything a tool's answer depends on stays in `server.py`,
written once, identical on both platforms. A seam drawn any higher would be two copies
of the part that matters.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable

import anyio.to_thread

# One HTTP request: (method, url, headers, body, timeout). `body` is the exact bytes to
# send, already encoded (or None for a bodiless GET) — encoding happens above the seam so
# both platforms put identical bytes on the wire. Returns `(status, body_text)` for every
# HTTP answer, success or failure — a 429 is a value here, not an exception, because its
# body is the part the model needs. Raise `OSError` (and only `OSError`) when there was
# no HTTP answer at all.
Fetch = Callable[[str, str, dict[str, str], bytes | None, float], Awaitable[tuple[int, str]]]


def _blocking_request(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, str]:
    """`urlopen`, with an error response read as an answer rather than raised.

    `urllib` turns every 4xx/5xx into an `HTTPError` — which is also a response object,
    so the body is still there to be read. Reading it here is what lets the layer above
    treat "the service said no, and here is why" as ordinary data.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        # The one case with no HTTP answer: DNS, refused connection, TLS, timeout.
        # `URLError` is already an `OSError`, but its `reason` is the readable half.
        raise OSError(exc.reason) from None


async def urllib_fetch(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, str]:
    """The CPython `Fetch`: `urllib` on a worker thread.

    `urlopen` blocks, and the SDK's transports are async — a blocking call on the event
    loop thread would stall a `wait_for_message` long-poll into a dead server for every
    other in-flight request. `anyio.to_thread` rather than `asyncio.to_thread` because
    the SDK is anyio-based and runs under trio just as happily.
    """
    return await anyio.to_thread.run_sync(_blocking_request, method, url, headers, body, timeout)
