"""Fire abusive raw HTTP/1.1 at a running agent-chat and report what it enforces.

Not a pytest module (the filename keeps it out of collection): it needs a real socket
server, because TestClient bypasses the HTTP parser entirely and so cannot tell you
anything about header, request-line or slow-body limits.

    uvicorn app:app --app-dir src --port 8099 --http h11 \
        --h11-max-incomplete-event-size 16384 --limit-concurrency 128 --timeout-keep-alive 5
    python tests/http_hardening_probe.py 8099

Measured 2026-08-13 with the Dockerfile's flags, on starlette 1.6.0 — expected shape:
  50 / 200 / 2000 headers      -> 431   (app.py's HeaderLimits: MAX_HEADERS = 48)
  single 8/16/64KB header value-> 431   (HeaderLimits: MAX_HEADER_BYTES = 8192 total)
  single 256KB header value    -> 400   (h11 rejects it before the app sees it; httptools
                                         answered 200, which is why we pin h11)
  oversized room-name path    -> 400 (may be the app's name validator, not the parser)
  complete 12/24KiB target     -> may reach the app; enforce a URL cap at the proxy
  declared 100MB body          -> 413   (Content-Length refused before buffering)
  chunked, no declared length  -> 408 after the total body deadline (10 seconds)
  partial headers, then idle   -> held open; requires a proxy deadline and connection cap

The 431s are the app's bound, not the parser's, and that is the point: the parser cap bounds
only *buffered incomplete* data, so the deterministic limit has to live in the app. An earlier
version of this note expected 200 for 2000 headers, from before HeaderLimits existed.
"""

import socket
import sys

HOST, PORT = "127.0.0.1", int(sys.argv[1])


def send(raw: bytes, label: str, read_timeout: float = 5.0) -> None:
    s = socket.create_connection((HOST, PORT), timeout=read_timeout)
    try:
        s.sendall(raw)
        s.settimeout(read_timeout)
        try:
            resp = s.recv(200)
        except TimeoutError:
            resp = b"<timeout, connection held open>"
        first = resp.split(b"\r\n")[0].decode(errors="replace") if resp else "<closed, no response>"
        print(f"  {label:38} -> {first}")
    except Exception as e:
        print(f"  {label:38} -> EXC {type(e).__name__}: {e}")
    finally:
        s.close()


print("header count / size limits:")
for n in (50, 200, 2000):
    hdrs = b"".join(b"X-Pad-%d: v\r\n" % i for i in range(n))
    send(b"GET /healthz HTTP/1.1\r\nHost: x\r\n" + hdrs + b"\r\n", f"{n} headers")

for kb in (8, 16, 64, 256):
    big = b"X-Big: " + b"a" * (kb * 1024) + b"\r\n"
    send(b"GET /healthz HTTP/1.1\r\nHost: x\r\n" + big + b"\r\n", f"single {kb}KB header value")

print("request line:")
for kb in (8, 64):
    send(b"GET /r/" + b"a" * (kb * 1024) + b" HTTP/1.1\r\nHost: x\r\n\r\n", f"{kb}KB request line")
for kb in (12, 24):
    send(
        b"GET /healthz?" + b"a" * (kb * 1024) + b" HTTP/1.1\r\nHost: x\r\n\r\n",
        f"{kb}KB target, valid route",
    )

print("body handling:")
send(
    b"POST /r/lobby HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
    b"Content-Length: 100000000\r\n\r\n" + b"x" * 1000,
    "declared 100MB body, sends 1KB",
)
send(
    b"POST /r/lobby HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
    b"Transfer-Encoding: chunked\r\n\r\n" + b"1000\r\n" + b"x" * 4096 + b"\r\n",
    "chunked, no declared length",
    read_timeout=12,
)

print("slowloris (headers never completed):")
s = socket.create_connection((HOST, PORT), timeout=3)
s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")
try:
    s.settimeout(8)
    r = s.recv(100)
    print(f"  {'partial headers, then idle':38} -> {r.split(chr(13).encode())[0] or '<closed>'}")
except TimeoutError:
    print(
        f"  {'partial headers, then idle':38} -> still open after 8s (keep-alive timeout does not apply)"
    )
finally:
    s.close()
