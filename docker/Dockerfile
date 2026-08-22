# Pinned by digest, not by tag: `3.12-slim` moves, and a service parsing hostile path
# segments should run the interpreter its tests ran on. Matches .python-version.
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHAT_ROOT=/data \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies come from uv.lock — one resolved set, transitive versions included, and no
# second copy of the pins here to drift out of step with pyproject.toml. Separate layer
# from the source so editing app.py does not reinstall cryptography.
#
# cryptography rather than a pure-Python Ed25519: the signed lane is a gate (mailboxes and
# owned rooms refuse writes without it), and a gate is real verification or nothing.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin chat \
    && mkdir -p /data && chown chat:chat /data

ENV PATH="/app/.venv/bin:$PATH"
# SKILL.md comes from the repo root — it is served at /skill.md and is the same file
# agents install, so there is exactly one copy of it anywhere.
COPY SKILL.md ./
COPY src/ ./

USER 10001
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz').read()"

# Single asyncio process: file appends stay serialised in one lock domain and the
# workload is IO-bound on <1 MiB files. Scale with --workers only behind a proxy;
# store.py's flock keeps multi-process appends correct.
# --http h11, not the faster httptools default: measured (tests/http_hardening_probe.py),
# httptools answered 200 OK to a single 256 KB header value, so the only header bound was
# Cloudflare's 128 KB — generous against a 128 MiB container. h11 rejects oversized header
# blocks and exposes the cap as an explicit number rather than a library default.
# --limit-concurrency bounds slow-body/slowloris connections (503 past the cap) because a
# keep-alive timeout does not apply while headers are still arriving.
# The h11 cap bounds the request line too, and the GET write lane puts the message in
# the URL — a full-length ASCII message URL-encodes to ~4 KiB — so 16 KiB is the floor
# that keeps the primary lane working. Header blocks are capped far tighter in app.py
# (48 headers / 8 KiB, MAX_HEADERS / MAX_HEADER_BYTES), where the bound can be exact
# instead of "whatever was buffered".
# --no-proxy-headers, deliberately. uvicorn's ProxyHeadersMiddleware REWRITES
# scope["client"] from X-Forwarded-For, and `--forwarded-allow-ips "*"` told it to do that
# for any peer — so on a directly reachable origin `request.client.host` was whatever the
# caller typed. app.py's client_ip() falls back to exactly that value, and the rate
# limiter, the write budget and the long-poll waiter caps all key on it: one header bought
# a fresh identity per request, which is the bypass client_ip()'s own docstring says the
# empty default exists to prevent. Two mechanisms consuming the same header, and the one
# nobody configured won. Now there is one: CHAT_CLIENT_IP_HEADER, off unless an operator
# sets it, because opting in is an assertion that the origin is locked to their proxy.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", \
     "--http", "h11", "--h11-max-incomplete-event-size", "16384", \
     "--limit-concurrency", "128", "--backlog", "128", \
     "--no-server-header", "--no-proxy-headers", \
     "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "10"]
