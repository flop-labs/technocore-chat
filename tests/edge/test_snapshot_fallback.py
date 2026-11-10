"""A deploy must not promote the previous fallback into its new origin snapshot."""

from __future__ import annotations

import importlib.util
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

EDGE = Path(__file__).resolve().parents[2] / "edge"


@pytest.fixture()
def snapshot(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("snapshot_fallback", EDGE / "snapshot.py")
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
    return SimpleNamespace(module=module, out=out, manifest=manifest)


def origin(monkeypatch, snapshot, marked_path=None, *, header="X-Origin-Fallback", status=200):
    class Response:
        def __init__(self, path):
            self.status = status if path == marked_path else 200
            self.headers = Message()
            self.headers["Content-Type"] = "text/plain; charset=utf-8"
            if path == marked_path:
                self.headers[header] = "1"
            self.body = f"previous snapshot of {path}".encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return self.body

    monkeypatch.setattr(
        snapshot.module.urllib.request,
        "urlopen",
        lambda url, timeout: Response(url.removeprefix("https://origin.example")),
    )


@pytest.mark.parametrize("path", ["/config", "/openapi.json", "/humans"])
def test_a_marked_200_cannot_become_the_next_deployment_snapshot(
    monkeypatch, snapshot, capsys, path
):
    origin(monkeypatch, snapshot, path)
    dest = snapshot.out / snapshot.module.asset_name(path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"last known good capture")
    snapshot.manifest.write_text("last known good routing", encoding="utf-8")

    assert snapshot.module.main() == 1
    assert dest.read_bytes() == b"last known good capture"
    assert snapshot.manifest.read_text(encoding="utf-8") == "last known good routing"
    error = capsys.readouterr().err
    assert path in error and "X-Origin-Fallback" in error


def test_fallback_header_names_are_case_insensitive(monkeypatch, snapshot):
    origin(monkeypatch, snapshot, "/config", header="x-origin-fallback")
    assert snapshot.module.main() == 1
    assert not snapshot.manifest.exists()


def test_unmarked_origin_documents_still_produce_a_complete_snapshot(monkeypatch, snapshot):
    origin(monkeypatch, snapshot)
    assert snapshot.module.main() == 0
    routing = json.loads(snapshot.manifest.read_text(encoding="utf-8"))
    assert set(routing["types"]) == set(snapshot.module.PATHS) | set(snapshot.module.EDGE_ONLY)
    assert (snapshot.out / "config").read_bytes() == b"previous snapshot of /config"
    for path, source in snapshot.module.STATIC_FIRST.items():
        assert (snapshot.out / snapshot.module.asset_name(path)).read_bytes() == (
            EDGE.parent / source
        ).read_bytes()


def test_static_first_bytes_come_from_the_tree_even_if_the_fetched_copy_is_marked(
    monkeypatch, snapshot
):
    origin(monkeypatch, snapshot, "/skill.md")
    assert snapshot.module.main() == 0
    assert (snapshot.out / "skill.md").read_bytes() == (EDGE.parent / "SKILL.md").read_bytes()


def test_a_non_200_is_still_a_failed_snapshot(monkeypatch, snapshot, capsys):
    origin(monkeypatch, snapshot, "/config", status=503)
    assert snapshot.module.main() == 1
    assert not snapshot.manifest.exists()
    assert "/config -> HTTP 503" in capsys.readouterr().err
