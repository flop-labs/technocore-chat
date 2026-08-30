from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_server(tmp_path: Path) -> tuple[int, subprocess.Popen]:
    env = {
        **os.environ,
        "CHAT_ROOT": str(tmp_path / "store"),
        "CHAT_RATE_READ": "1000000",
        "CHAT_RATE_WRITE": "1",
        "CHAT_RATE_ROOMS_PER_DAY": "1000000",
    }
    failures = []
    for attempt in range(5):
        port = _free_port()
        stderr_path = tmp_path / f"uvicorn-{attempt}.stderr"
        stderr = stderr_path.open("wb")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--app-dir",
                str(ROOT / "src"),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--http",
                "h11",
                "--no-proxy-headers",
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        ready = False
        for _ in range(50):
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=0.2
                ) as response:
                    response.read()
                ready = True
                break
            except OSError:
                time.sleep(0.1)
        stderr.close()
        if ready:
            return port, process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        failures.append(stderr_path.read_text(encoding="utf-8", errors="replace"))
    pytest.fail("uvicorn did not become ready after retries:\n" + "\n".join(failures))


@pytest.fixture()
def h11_server(tmp_path) -> Iterator[int]:
    port, process = _start_server(tmp_path)
    try:
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _receive_before(connection: socket.socket, deadline: float, phase: str) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        pytest.fail(f"exceeded the overall deadline while {phase}")
    connection.settimeout(remaining)
    try:
        return connection.recv(4096)
    except TimeoutError:
        pytest.fail(f"timed out while {phase}")


def _incomplete_events_post(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall(
            b"POST /r/events HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: keep-alive\r\n\r\n"
            b"100000\r\n" + b"x" * 1024 + b"\r\n"
        )
        deadline = time.monotonic() + 3
        response = bytearray()
        while b"\r\n\r\n" not in response:
            try:
                chunk = _receive_before(connection, deadline, "waiting for response headers")
            except ConnectionResetError:
                pytest.fail("server reset the connection before sending complete response headers")
            if not chunk:
                break
            response.extend(chunk)

        assert response.startswith(b"HTTP/1.1 403")
        assert b"connection: close" in response.lower()

        while True:
            try:
                chunk = _receive_before(connection, deadline, "waiting for connection close")
            except ConnectionResetError:
                break
            if not chunk:
                break


def _read_response(connection: socket.socket, phase: str) -> tuple[int, dict[bytes, bytes]]:
    deadline = time.monotonic() + 3
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = _receive_before(connection, deadline, f"waiting for {phase} headers")
        if not chunk:
            pytest.fail(f"connection closed before {phase} headers completed")
        response.extend(chunk)

    head, body = bytes(response).split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers = {
        name.lower(): value.strip() for line in lines[1:] for name, value in [line.split(b":", 1)]
    }
    length = int(headers.get(b"content-length", b"0"))
    while len(body) < length:
        chunk = _receive_before(connection, deadline, f"waiting for {phase} body")
        if not chunk:
            pytest.fail(f"connection closed before {phase} body completed")
        body += chunk
    return status, headers


def test_events_rejects_an_incomplete_chunk_without_waiting_for_the_body(
    h11_server,
):
    _incomplete_events_post(h11_server)


def test_rate_limited_events_reuses_a_connection_after_a_complete_body(
    h11_server,
):
    request = urllib.request.Request(
        f"http://127.0.0.1:{h11_server}/r/lobby",
        data=json.dumps({"from": "bot", "text": "spend token"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        assert response.status == 200
        response.read()

    with socket.create_connection(("127.0.0.1", h11_server), timeout=3) as connection:
        connection.sendall(
            b"POST /r/events HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: keep-alive\r\n\r\n{}"
        )
        status, headers = _read_response(connection, "rate-limited events response")
        assert status == 429
        assert b"connection" not in headers

        connection.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        status, _ = _read_response(connection, "keep-alive health check")
        assert status == 200
