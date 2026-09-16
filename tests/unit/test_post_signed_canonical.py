"""Run the embedded helper and real signer with disposable state and mocked I/O."""

import base64
import contextlib
import io
import json
import os
import runpy
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest

TEXT_CASES = [
    pytest.param("  --literal option  ", "--literal option", id="leading-option-after-sweep"),
    pytest.param("  hello world  ", "hello world", id="trim"),
    pytest.param("hello\n\tworld\x01", "hello  world", id="controls"),
    pytest.param("hello\u200dworld", "hello world", id="format"),
    pytest.param("hello\udcffworld", "hello world", id="surrogateescape"),
    pytest.param("hello\ue000world", "hello world", id="private-use"),
    pytest.param("hello\u2028world", "hello world", id="line-separator"),
    pytest.param("hello\u2029world", "hello world", id="paragraph-separator"),
    pytest.param("  cafe\u0301  world  ", "cafe\u0301  world", id="no-nfc-or-space-folding"),
]


class HelperRun:
    """Separate exec namespaces model restarts; only files and server records survive."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
        self.repo = Path(__file__).resolve().parents[2]
        self.helper = self.repo / "post_signed.sh"
        self.home = tmp_path / "home"
        self.seed_file = self.home / ".config" / "technocore" / "sign_seed"
        self.seed_file.parent.mkdir(parents=True)
        self.seed_file.write_text("0" * 64 + "\n")  # Public, disposable fixture only.
        self.seed_file.chmod(0o600)
        self.mode = mode
        self.reveal = False
        self.attempts = []
        self.markers = []
        self.messages = []
        self.monkeypatch = monkeypatch
        self.signer = runpy.run_path(str(self.repo / "scripts" / "sign.py"))

    def pending(self):
        return list((self.seed_file.parent / "nonces").glob("*.pending"))

    def signer_process(self, args, **kwargs):
        # Execute the exact signer program or canonicalization snippet in-process,
        # under the helper-supplied argv/stdin/environment. No package downloads.
        assert args[:4] == ["uv", "run", "--frozen", "python"]
        stdout, stderr = io.StringIO(), io.StringIO()
        env = kwargs.get("env", os.environ.copy())
        with self.monkeypatch.context() as m, patch.dict(os.environ, env, clear=True):
            m.setattr(sys, "stdin", io.StringIO(kwargs.get("input", "")))
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    if args[4] == "-c":
                        assert "SIGN_SEED" not in env
                        exec(compile(args[5], "<signer-sweep>", "exec"), {"__name__": "__main__"})
                    else:
                        assert args[4] == "scripts/sign.py"
                        assert env["SIGN_SEED"] == "0" * 64
                        m.setattr(sys, "argv", args[4:])
                        runpy.run_path(str(self.repo / "scripts" / "sign.py"), run_name="__main__")
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        raise subprocess.CalledProcessError(
                            1, args, output=stdout.getvalue(), stderr=str(exc)
                        ) from exc
        return subprocess.CompletedProcess(args, 0, stdout.getvalue(), stderr.getvalue())

    def urlopen(self, request, timeout):
        assert request.full_url.startswith("http://127.0.0.1:9/r/test-room")
        assert timeout in (5, 20)
        if request.get_method() == "GET":
            return io.BytesIO(
                json.dumps({"messages": self.messages if self.reveal else []}).encode()
            )
        assert request.get_method() == "POST"
        payload = json.loads(request.data)
        self.attempts.append(payload)
        markers = self.pending()
        assert len(markers) == 1, "the attempt must be saved before the POST"
        self.markers.append(json.loads(markers[0].read_text()))
        cleaned = self.signer["swept"](payload["text"], self.signer["MAX_TEXT_CHARS"])
        self.signer["public_key"](payload["did"]).verify(
            base64.urlsafe_b64decode(payload["sig"] + "=="),
            f"test-room|{payload['nonce']}|{cleaned}".encode(),
        )
        self.messages.append(
            {
                "from": payload["did"],
                "sig": payload["sig"],
                "nonce": int(payload["nonce"]),
                "text": cleaned,
            }
        )
        if len(self.attempts) == 1:
            if self.mode == "disconnect":
                raise urllib.error.URLError("fixture: committed write, response lost")
            if self.mode == "5xx":
                raise urllib.error.HTTPError(
                    request.full_url, 503, "fixture", Message(), io.BytesIO(b"lost")
                )
            return io.BytesIO(b"\xff")
        return io.BytesIO(b"ok")

    def send(self, text: str):
        source = self.helper.read_text().split("<<'INNERPY'\n", 1)[1].rsplit("\nINNERPY", 1)[0]
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with self.monkeypatch.context() as m:
            m.chdir(self.repo)
            m.setenv("HOME", str(self.home))
            m.delenv("SIGN_SEED", raising=False)
            m.setattr(
                sys,
                "argv",
                [str(self.helper), "test-room", text, str(self.seed_file), "http://127.0.0.1:9"],
            )
            m.setattr(subprocess, "run", self.signer_process)
            m.setattr(urllib.request, "urlopen", self.urlopen)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    exec(compile(source, str(self.helper), "exec"), {"__name__": "__main__"})
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
                except subprocess.CalledProcessError:
                    code = 1
        return subprocess.CompletedProcess(["helper"], code, stdout.getvalue(), stderr.getvalue())


@pytest.mark.parametrize(("raw", "canonical"), TEXT_CASES)
@pytest.mark.parametrize("mode", ["disconnect", "5xx", "unreadable"])
@pytest.mark.parametrize("legacy", [False, True], ids=["canonical-marker", "legacy-raw-marker"])
def test_swept_text_reconciles_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    canonical: str,
    mode: str,
    legacy: bool,
) -> None:
    helper = HelperRun(tmp_path, monkeypatch, mode)
    first = helper.send(raw)
    assert first.returncode == 2, first.stdout + first.stderr
    assert len(helper.attempts) == 1
    assert helper.attempts[0]["text"] == canonical
    assert helper.markers[0]["text"] == canonical
    (marker,) = helper.pending()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    if legacy:
        record = json.loads(marker.read_text())
        record["text"] = raw  # Persisted by the previous helper, signed after sweeping.
        marker.write_text(json.dumps(record) + "\n")
    helper.reveal = True
    second = helper.send(raw)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "not sending the same logical message again" in second.stdout
    assert len(helper.attempts) == 1
    assert not marker.exists()
    third = helper.send("a genuinely different later message")
    assert third.returncode == 0, third.stdout + third.stderr
    assert len(helper.attempts) == 2
    assert int(helper.attempts[1]["nonce"]) > int(helper.attempts[0]["nonce"])


@pytest.mark.parametrize("mode", ["disconnect", "5xx", "unreadable"])
def test_swept_text_reconciles_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    helper = HelperRun(tmp_path, monkeypatch, mode)
    helper.reveal = True
    result = helper.send("  first\nsecond\u200dthird  ")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "exact signed record is present" in result.stdout
    assert len(helper.attempts) == 1
    assert helper.attempts[0]["text"] == "first second third"
    assert not helper.pending()


@pytest.mark.parametrize("field", ["from", "nonce", "sig", "text"])
def test_canonicalization_does_not_relax_exact_record_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    helper = HelperRun(tmp_path, monkeypatch, "disconnect")
    assert helper.send("  first\nsecond  ").returncode == 2
    helper.reveal = True
    original = helper.messages[0][field]
    helper.messages[0][field] = "different"
    assert helper.send("first second").returncode == 2
    assert len(helper.attempts) == 1
    assert helper.pending()
    helper.messages[0][field] = original
    recovered = helper.send("first second")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert len(helper.attempts) == 1


@pytest.mark.parametrize("raw", [" \n\t\u200d ", "x" * 4097])
def test_invalid_swept_text_never_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    helper = HelperRun(tmp_path, monkeypatch, "disconnect")
    assert helper.send(raw).returncode != 0
    assert not helper.attempts
    assert not helper.pending()
