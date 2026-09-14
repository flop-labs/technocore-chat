"""Fire abusive raw HTTP/1.1 at a running agent-chat and report what it enforces.

Not a pytest module (the filename keeps it out of collection): it needs a real socket
server, because TestClient bypasses the HTTP parser entirely and so cannot tell you
anything about header, request-line or slow-body limits.

    uvicorn app:app --app-dir src --port 8099 --http h11 \
        --h11-max-incomplete-event-size 16384 --limit-concurrency 128 --timeout-keep-alive 5
    python tests/http_hardening_probe.py 8099

Measured 2026-08-13 with the Dockerfile's flags, on starlette 1.6.0 — expected shape.

50/200 headers and a single 8KB header value are smaller in total than the configured
--h11-max-incomplete-event-size (16384), so h11 cannot still be holding them as an
incomplete, over-cap event once they finish arriving; HeaderLimits is the only thing
that can reject them. These are deterministic:
  50 / 200 headers               -> 431   (app.py's HeaderLimits: MAX_HEADERS = 48)
  single 8KB header value        -> 431   (HeaderLimits: MAX_HEADER_BYTES = 8192 total)

2000 headers and single 16/64KB header values are all larger than that cap in total.
Whether h11 sees a complete event before crossing the cap depends on how the transport
chunks the read, which a single uncontrolled sendall() from this probe does not control.
A plain sendall() has reached 431 consistently for 2000 headers and for 16KB; only 64KB
has flipped between both outcomes under that same plain sendall(). Deliberately
fragmenting delivery, on the other hand, has reproduced 400 for all three:
  2000 headers                   -> 400 or 431, depending on read segmentation
  single 16KB header value       -> 400 or 431, depending on read segmentation
  single 64KB header value       -> 400 or 431, depending on read segmentation
h11 returns 400 if it is left holding an incomplete event over the cap; otherwise the
complete event reaches HeaderLimits, which returns 431. Same dependency as the 24KB
request-target case below — 12KB stays under the cap and is deterministic, like the
8KB header case above.

  single 256KB header value      -> 400 in every trial run so far, continuous and
                                     fragmented delivery alike (h11 rejects it before the
                                     app sees it; httptools answered 200, which is why we
                                     pin h11). The margin over the cap is not by itself a
                                     guarantee of this — 64KB is also far over the cap and
                                     is not deterministic — so treat this as strong
                                     empirical evidence from the cases tested, not a proof.
  8/64KB request line            -> 400, consistent under plain and fragmented delivery.
                                     Confirmed app-level, not the parser, at both sizes: an
                                     8KB query string on the same /r/<room> route returns
                                     200; a room name as short as 50 characters (79-byte
                                     total request) already returns 400; and the response
                                     body itself is the app's own "400 bad name '...'" text
                                     with the offending name echoed back, confirmed directly
                                     for the 8KB and the 64KB room-name cases both — so
                                     even the 64KB request line reaches the app and is
                                     rejected there, not by h11. The exact accept/reject
                                     threshold isn't pinned down here; only that it's the
                                     room-name validator at every size tested, not a
                                     request-line byte-length limit.
  complete 12KB target           -> 200, consistent under plain and fragmented delivery
                                     (12323 bytes, under the h11 cap, so it always reaches
                                     the app)
  complete 24KB target           -> 200 under plain delivery, 400 when fragmented (24611
                                     bytes, over the h11 cap — same segmentation dependency
                                     as the 16/64KB header cases above); enforce a URL cap
                                     at the proxy rather than relying on this
  declared 100MB body, sends 1KB -> 413, consistent under plain and fragmented delivery
                                     (Content-Length is checked before any body is
                                     buffered, so this doesn't depend on how much of the
                                     1KB actually arrives)
  chunked, no declared length    -> 408, consistent (body deadline is wall-clock, not
                                     buffer-size dependent)
  partial headers, then idle     -> held open; requires a proxy deadline and connection cap

For the under-cap cases (50/200 headers, 8KB header, 12KB target) the result is the
app's bound, not the parser's: the parser cap only bounds *buffered incomplete* data, so
once the full event is under that cap it always reaches the app and the app's own limits
decide the outcome. Over-cap cases (2000 headers, 16/64KB header, 24KB target) don't get
that guarantee — the parser can reject them first depending on how the transport delivers
the bytes, so their result is segmentation-dependent rather than a fixed app bound. An
earlier version of this note expected 200 for 2000 headers, from before HeaderLimits
existed.
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
