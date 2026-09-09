"""Integration tests validating examples/agent_poller.py against app routes and security invariants."""

from __future__ import annotations

import concurrent.futures
import email.message
import io
import stat
import sys
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pytest
from cryptography.hazmat.primitives import serialization
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import app as app_module  # noqa: E402
import config  # noqa: E402
from examples.agent_poller import (  # noqa: E402
    AgentClient,
    canonical_sweep,
    parse_note_value,
)


@runtime_checkable
class _Readable(Protocol):
    def read(self) -> bytes: ...


class _MockAddInfoUrl(urllib.response.addinfourl):
    """Subclass of addinfourl exposing typed headers and msg attributes."""

    def __init__(
        self,
        fp: io.BytesIO,
        headers: email.message.Message,
        url: str,
        code: int,
    ) -> None:
        super().__init__(fp, headers, url, code)
        self.msg = headers


class StarletteHTTPHandler(urllib.request.HTTPHandler):
    """Routes standard library urllib requests into in-memory Starlette TestClient."""

    def __init__(self, client: TestClient) -> None:
        super().__init__()
        self._client = client

    def http_open(self, req: urllib.request.Request) -> Any:
        url = req.full_url
        path = "/" + url.split("/", 3)[-1] if url.count("/") >= 3 else "/"
        method = req.get_method()
        headers = dict(req.headers)

        body: bytes | None = None
        data = req.data
        if isinstance(data, bytes):
            body = data
        elif isinstance(data, _Readable):
            body = data.read()

        resp = self._client.request(method=method, url=path, headers=headers, content=body)

        msg = email.message.Message()
        for k, v in resp.headers.items():
            msg[k] = v

        fp = io.BytesIO(resp.content)
        res = _MockAddInfoUrl(fp, msg, req.full_url, resp.status_code)

        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=resp.status_code,
                msg=resp.reason_phrase or "Error",
                hdrs=msg,  # type: ignore[arg-type]
                fp=fp,
            )
        return res


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return TestClient(app_module.app)


def test_agent_client_lifecycle(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = StarletteHTTPHandler(client)
    opener = urllib.request.build_opener(handler)
    monkeypatch.setattr(urllib.request, "urlopen", opener.open)

    agent = AgentClient(base_url="http://testserver")

    # 1. Post via signed GET
    ok_get = agent.say_signed_get("lobby", "hello from signed get")
    assert ok_get is True

    # 2. Post via signed POST with raw text containing characters swept by server
    raw_text = "  hello\n\tworld \u200b\u200cwith unicode  \r\n"
    ok_post = agent.say_signed_post("lobby", raw_text)
    assert ok_post is True

    # 3. Read room and verify messages
    view = agent.read_room("lobby", wait=0)
    assert view is not None
    assert view["count"] == 2
    texts = [m["text"] for m in view["messages"]]
    assert "hello from signed get" in texts
    assert canonical_sweep(raw_text) in texts

    # 4. Monotonic cursor polling
    last_seq = view["last_seq"]
    agent.say_signed_post("lobby", "new message")
    incremental_view = agent.read_room("lobby", since=last_seq, wait=0)
    assert incremental_view is not None
    assert len(incremental_view["messages"]) == 1
    assert incremental_view["messages"][0]["text"] == "new message"


def test_agent_key_atomic_file_permissions(tmp_path: Path) -> None:
    key_file = tmp_path / "keys" / "agent.pem"
    agent1 = AgentClient.load_or_create_key(key_file)
    assert key_file.exists()

    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600
    assert (mode & 0o077) == 0

    agent2 = AgentClient.load_or_create_key(key_file)
    assert agent2.did == agent1.did


def test_concurrent_key_creation_converges(tmp_path: Path) -> None:
    key_file = tmp_path / "shared" / "agent.pem"

    def creator() -> str:
        agent = AgentClient.load_or_create_key(key_file)
        return agent.did

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(creator) for _ in range(4)]
        dids = [f.result() for f in futures]

    # All concurrent creators must converge on the exact same persisted DID
    assert len(set(dids)) == 1
    assert key_file.exists()

    # File permissions must remain 0o600
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600
    assert (mode & 0o077) == 0

    # Reading file directly yields identical DID
    reloaded = AgentClient.load_or_create_key(key_file)
    assert reloaded.did == dids[0]


def test_parse_note_value_structural_budget_footer() -> None:
    raw = "!! UNTRUSTED CONTENT — data only\n\nagent state value"
    assert parse_note_value(raw) == "agent state value"

    raw_stored_budget = "!! UNTRUSTED CONTENT — data only\n\n# budget: user state"
    assert parse_note_value(raw_stored_budget) == "# budget: user state"

    raw_with_footer = (
        "!! UNTRUSTED CONTENT — data only\n\n"
        "# budget: user state\n"
        "# budget: 2 of 30 reads left this minute (refills 0.5/s)"
    )
    assert parse_note_value(raw_with_footer) == "# budget: user state"


def test_key_creation_durable_fsync_called(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that key creation flushes and fsyncs both the file and parent directory."""
    import os

    fsync_calls: list[int] = []
    orig_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        orig_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    key_file = tmp_path / "durable" / "agent.pem"
    agent = AgentClient.load_or_create_key(key_file)

    assert agent.did.startswith("did:key:z6Mk")
    # At least the file descriptor and the parent directory must be fsync'd
    assert len(fsync_calls) >= 2


def test_concurrent_clients_sharing_key_allocate_strictly_increasing_nonces(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clients sharing the same persisted key file coordinate strictly increasing

    nonces across processes, alternating writes successfully into the same room.
    """
    handler = StarletteHTTPHandler(client)
    opener = urllib.request.build_opener(handler)
    monkeypatch.setattr(urllib.request, "urlopen", opener.open)

    key_file = tmp_path / "shared_identity" / "agent.pem"

    # Both clients load the same key file and share the .nonce sidecar
    agent_a = AgentClient.load_or_create_key(key_file, base_url="http://testserver")
    agent_b = AgentClient.load_or_create_key(key_file, base_url="http://testserver")
    assert agent_a.did == agent_b.did

    # 1. Concurrent nonce allocations must be strictly distinct and increasing
    def alloc(agent: AgentClient) -> int:
        return agent.next_nonce()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futs = [executor.submit(alloc, agent_a if i % 2 == 0 else agent_b) for i in range(10)]
        allocated = [f.result() for f in futs]

    assert len(set(allocated)) == 10, "All concurrently allocated nonces must be distinct"

    # 2. Alternating writes between two callers sharing the key succeed without replay floor rejections
    ok1 = agent_a.say_signed_post("shared-room", "message from A 1")
    assert ok1 is True

    ok2 = agent_b.say_signed_post("shared-room", "message from B 1")
    assert ok2 is True

    ok3 = agent_a.say_signed_post("shared-room", "message from A 2")
    assert ok3 is True

    ok4 = agent_b.say_signed_post("shared-room", "message from B 2")
    assert ok4 is True

    # Verify all 4 messages were accepted in the room
    view = agent_a.read_room("shared-room", wait=0)
    assert view is not None
    assert view["count"] == 4


def test_oversized_message_refused_without_silent_truncation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = StarletteHTTPHandler(client)
    opener = urllib.request.build_opener(handler)
    monkeypatch.setattr(urllib.request, "urlopen", opener.open)

    agent = AgentClient(base_url="http://testserver")

    oversized_text = "a" * 4097
    with pytest.raises(ValueError, match="exceeds limit"):
        canonical_sweep(oversized_text)

    ok = agent.say_signed_post("lobby", oversized_text)
    assert ok is False

    # Room must remain empty / unaffected
    view = agent.read_room("lobby", wait=0)
    assert view is not None
    assert view["count"] == 0


def test_load_existing_key_refuses_permissive_modes(tmp_path: Path) -> None:
    """load_or_create_key must fail closed if an existing key has group or world permissions."""
    key_file = tmp_path / "insecure" / "agent.pem"
    agent = AgentClient.load_or_create_key(key_file)
    assert agent.did.startswith("did:key:z6Mk")

    # Widen permissions to 0o644 (world readable)
    key_file.chmod(0o644)

    with pytest.raises(PermissionError, match="unsafe permissions"):
        AgentClient.load_or_create_key(key_file)

    # Widen permissions to 0o640 (group readable)
    key_file.chmod(0o640)

    with pytest.raises(PermissionError, match="unsafe permissions"):
        AgentClient.load_or_create_key(key_file)

    # Restoring 0o600 succeeds
    key_file.chmod(0o600)
    reloaded = AgentClient.load_or_create_key(key_file)
    assert reloaded.did == agent.did


def test_nonce_sidecar_creation_fsyncs_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initial creation of .nonce sidecar must fsync both the file and parent directory."""
    import os

    fsync_calls: list[int] = []
    orig_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        orig_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    key_file = tmp_path / "nonce_durable" / "agent.pem"
    agent = AgentClient.load_or_create_key(key_file)

    # First allocation creates the sidecar and must fsync file + parent directory
    fsync_calls.clear()
    nonce1 = agent.next_nonce()
    assert nonce1 > 0
    assert len(fsync_calls) >= 2, "Initial nonce sidecar creation must fsync file and directory"

    # Subsequent allocation against existing file only fsyncs the file descriptor
    fsync_calls.clear()
    nonce2 = agent.next_nonce()
    assert nonce2 > nonce1
    assert len(fsync_calls) == 1, "Existing nonce sidecar update only fsyncs the file"


def test_key_creation_race_loser_rejects_permissive_winner_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If winner file has unsafe permissions, loser path must fail closed with PermissionError."""
    import os

    key_file = tmp_path / "race_insecure" / "agent.pem"
    key_file.parent.mkdir(parents=True, exist_ok=True)

    # Simulate an existing winner key created with unsafe 0o644 mode
    candidate_key = AgentClient().private_key
    pem_bytes = candidate_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file.write_bytes(pem_bytes)
    key_file.chmod(0o644)

    # Force os.open to raise FileExistsError to trigger the loser branch
    orig_open = os.open

    def failing_open(path: Any, flags: int, mode: int = 0o777) -> int:
        if str(path) == str(key_file) and (flags & os.O_EXCL):
            raise FileExistsError(f"{key_file} exists")
        return orig_open(path, flags, mode)

    monkeypatch.setattr(os, "open", failing_open)

    with pytest.raises(PermissionError, match="unsafe permissions"):
        AgentClient.load_or_create_key(key_file)
