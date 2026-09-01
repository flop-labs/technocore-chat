# Pinned by digest, not by tag: `3.12-slim` moves, and a service parsing hostile path
# segments should run the interpreter its tests ran on. Matches .python-version.
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHAT_ROOT=/data \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Digest, not the `0.12.3` tag this used to carry. `COPY --from=<image>` is an image pull
# wearing the syntax of a file copy, so the tag read as a version pin when it is in fact a
# mutable reference: a registry tag can be repointed at any content by whoever controls the
# repository, and this line drops the result straight onto PATH as the binary that installs
# every dependency in the image. The digest is the released 0.12.3 index, which is what a
# reproducible build of this image needs anyway.
COPY --from=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies come from uv.lock — one resolved set, transitive versions included, and no
# second copy of the pins here to drift out of step with pyproject.toml. Separate layer
# from the source so editing app.py does not reinstall cryptography.
#
# libsodium (PyNaCl) rather than a pure-Python Ed25519: the signed lane is a gate
# (mailboxes and owned rooms refuse writes without it), and a gate is real verification or
# nothing. cryptography stays for key generation and the documented X25519/AES-GCM
# examples; only verification uses libsodium, at roughly twice the verifies per second.
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

# --timeout=20s, not the 3s this used to be. The probe is `docker exec` plus a cold
# CPython, so most of its budget is interpreter startup rather than anything about
# liveness: measured on a loaded 4-core box, `python -c "pass"` inside this container took
# 0.79s/1.02s/1.02s and the whole probe 1.87s-9.88s, while the app answered /healthz
# through the tunnel in 0.165s-0.187s. A 3s budget therefore reported a busy service as a
# dead one — 49 consecutive failures while it served 43 req/s normally. A generous timeout
# costs nothing in detection, because a dead uvicorn refuses the connection in
# milliseconds: only slowness ever waits out the clock.
#
# Deployers should also set `init: true` (compose) or `--init` (docker run), and it is not
# optional under load. A timed-out healthcheck exec is killed and re-parented to PID 1;
# PID 1 here is uvicorn, which never wait()s on children it did not spawn, so each
# timed-out check leaked a zombie — 101 of this container's 128 pids_limit were gone
# before anyone looked. The timeout above removes the cause; `init: true` removes the
# consequence, and only the deployer can supply it.
#
# The image deliberately does NOT ship its own init (tini as ENTRYPOINT), though it would
# make the guard unconditional. Two reasons: the timeout fix means healthchecks stop being
# killed at all, so the leak has no source to feed it; and this image is kept minimal on
# purpose — digest-pinned base, no build toolchain, nothing in it that the service does not
# execute — so adding a package and an ENTRYPOINT layer to defend against a case that no
# longer arises is the wrong trade. Revisit it if anything else in the image starts
# spawning short-lived children.
HEALTHCHECK --interval=30s --timeout=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz').read()"

# Single asyncio process: file appends stay serialised in one lock domain and the
# workload is IO-bound on <1 MiB files. Scale with --workers only behind a proxy;
# store.py's flock keeps multi-process appends correct.
#
# What --workers multiplies, which is not obvious and broke production once:
#
# * --limit-concurrency below is PER PROCESS. N workers means N x 128 in flight, and the
#   ceiling is what a flood actually hits — the box sat at continuous
#   "WARNING: Exceeded concurrency limit" -> 503 while three of its four cores idled,
#   because extra cores do nothing for a per-process connection cap. Workers were the fix;
#   more CPU was not. Divide this flag by N if you want the old total, or leave it and
#   accept N x the in-flight bound deliberately.
# * limit.py's _buckets is per process too, so a per-IP budget becomes per-IP-per-worker.
#   Do NOT naively divide CHAT_RATE_* by N to compensate: keep-alive pins a connection to
#   one worker, so a single agent talking to one worker gets RATE/N rather than RATE, and
#   only a caller that reconnects across all N ever reaches the nominal budget. The
#   authoritative per-IP limit belongs in the proxy (see README); these are a backstop.
# * CHAT_MAX_WAITERS_TOTAL and CHAT_MAX_WAITERS_PER_IP are per process, and unlike the
#   rate buckets they are safe to divide: a refused waiter slot degrades to an immediate
#   empty reply, not an error, so N x 64 parked sockets is the number to hold down.
# * /stats request counters are per process and say so — the payload carries
#   "scope": "per_worker" and a "workers" figure. That figure reads WEB_CONCURRENCY, which
#   uvicorn also accepts as the default for --workers, so prefer `WEB_CONCURRENCY=3` over
#   `--workers 3`: one variable then sets the process count and makes /stats honest at the
#   same time. With --workers the count still applies but /stats will report workers=1.
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
