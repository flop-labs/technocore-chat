"""Pin the served text policy; the browser probe exercises selection and clipboard."""

import re

from starlette.testclient import TestClient

import app


def test_message_bodies_preserve_spaces_without_disabling_wrapping():
    page = TestClient(app.app).get("/humans").text
    body = re.search(r"\.body\s*\{([^}]+)\}", page)
    assert body is not None
    policy = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", body[1]))
    assert policy.get("white-space", "normal").strip() in {"pre-wrap", "break-spaces"}, (
        "collapsing message spaces changes quoted literals when a reader copies them"
    )
