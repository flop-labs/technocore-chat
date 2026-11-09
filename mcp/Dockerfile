# The MCP stdio server, and only that. The chat service has its own image in
# docker/Dockerfile — this one serves no HTTP, opens no port and owns no data directory.
#
# Pinned by digest for the same reason the service image is: `3.12-slim` moves.
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# Unbuffered because the transport *is* stdout: a buffered frame is a reply the client
# never sees until the next one flushes it.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    TECHNOCORE_URL=https://technocore.chat

# From the checkout beside it, not from PyPI (#475). Installing the published wheel made
# `docker build` produce an image of whatever was last released rather than of the code in
# front of you: between releases that is silently the *previous* version, so the one build
# a contributor runs to check their change could not see it. The old comment argued the
# image should run the same artifact `uvx technocore-mcp` does — but `uvx` is how a user
# runs a release, and this is how a developer runs a branch, and those want opposite
# things. A user who wants the release still has `uvx technocore-mcp` and
# `pip install technocore-mcp`, both unchanged.
#
# `mcp/` has to be in the build context for this, which is why .dockerignore now admits
# it; the build context is still the repo root, so run this from there.
COPY mcp/ /build/mcp/
RUN pip install --no-cache-dir /build/mcp \
    && rm -rf /build \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp

USER 10001

# stdio: the process holds stdin open and speaks JSON-RPC over it. Sitting idle with no
# output is the correct behaviour, not a hang.
CMD ["technocore-mcp"]
