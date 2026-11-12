"""Failed captures must not mix a new release's assets with the previous manifest."""

from __future__ import annotations

import importlib.util
import io
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.response import addinfourl

import pytest

EDGE = Path(__file__).resolve().parents[2] / "edge"


@pytest.fixture()
def snapshot(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("snapshot_capture", EDGE / "snapshot.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "public"
    manifest = tmp_path / "routing.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "snapshot.py",
            "--base",
            "https://origin.example",
            "--out",
            str(out),
            "--manifest",
            str(manifest),
        ],
    )

    def origin(url, timeout):
        headers = Message()
        headers["Content-Type"] = "text/plain; charset=utf-8"
        body = ("new origin " + urlsplit(url).path).encode()
        return addinfourl(io.BytesIO(body), headers, url, code=200)

    monkeypatch.setattr(module.urllib.request, "urlopen", origin)
    return SimpleNamespace(module=module, out=out, manifest=manifest, root=tmp_path)


def seed_capture(snapshot):
    for path in (*snapshot.module.PATHS, *snapshot.module.EDGE_ONLY):
        dest = snapshot.out / snapshot.module.asset_name(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(("old snapshot " + path).encode())
    snapshot.manifest.write_text("old routing", encoding="utf-8")


def captured_files(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["http", "body", "static", "edge-only", "cache-key"])
def test_a_failed_capture_leaves_all_previous_outputs_untouched(
    monkeypatch, snapshot, existing, failure
):
    if existing:
        seed_capture(snapshot)
    before = captured_files(snapshot.root)

    if failure in {"http", "body"}:
        origin = snapshot.module.urllib.request.urlopen

        def failing_origin(url, timeout):
            if urlsplit(url).path == "/config":
                if failure == "http":
                    raise HTTPError(url, 503, "Unavailable", Message(), None)
                response = origin(url, timeout)

                def broken_read():
                    raise IncompleteRead(b"partial config", 10)

                monkeypatch.setattr(response, "read", broken_read)
                return response
            return origin(url, timeout)

        monkeypatch.setattr(snapshot.module.urllib.request, "urlopen", failing_origin)
    elif failure == "static":
        monkeypatch.setattr(
            snapshot.module, "STATIC_FIRST", {"/skill.md": str(snapshot.root / "missing")}
        )
    elif failure == "edge-only":
        monkeypatch.setattr(
            snapshot.module,
            "EDGE_ONLY",
            {"/favicon.ico": (str(snapshot.root / "missing"), "image/x-icon")},
        )
    else:
        monkeypatch.setattr(snapshot.module, "rooms_key", lambda: {})

    if failure in {"static", "edge-only"}:
        with pytest.raises(FileNotFoundError):
            snapshot.module.main()
    else:
        assert snapshot.module.main() == 1

    assert captured_files(snapshot.root) == before
    if not existing:
        assert not snapshot.out.exists()
        assert not snapshot.manifest.exists()
