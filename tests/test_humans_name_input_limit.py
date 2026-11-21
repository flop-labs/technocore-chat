import re
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _maxlength(html: str, field: str) -> int:
    match = re.search(rf'<input id="{field}"[^>]*maxlength="(\d+)"', html)
    assert match is not None, f"missing maxlength for #{field}"
    return int(match.group(1))


@pytest.mark.parametrize("field", ["room", "nick"])
def test_humans_name_inputs_match_the_server_name_limit(field: str):
    import app as app_module
    import store

    # Exercise the page as served, not the source file directly: the browser-visible
    # maxlength must agree with the same validator every room/nick route reaches.
    html = TestClient(app_module.app).get("/humans").text
    limit = _maxlength(html, field)

    accepted = "a" * limit
    assert store.valid_name(accepted) == accepted

    with pytest.raises(store.StoreError):
        store.valid_name("a" * (limit + 1))


def test_humans_message_input_matches_the_server_text_limit():
    import app as app_module
    import store

    # Native maxlength counts UTF-16 units, but the server counts Unicode code points
    # after sweeping invisibles. The composer must not truncate emoji before Send.
    html = TestClient(app_module.app).get("/humans").text
    message = re.search(r'<input id="text"[^>]*>', html)
    assert message is not None
    assert "maxlength=" not in message.group()
    assert "Array.from(clean).length > 4096" in html
    assert store.MAX_TEXT_CHARS == 4096

    accepted = "😀" * store.MAX_TEXT_CHARS
    assert store.clean_text(accepted) == accepted

    with pytest.raises(store.StoreError):
        store.clean_text("😀" * (store.MAX_TEXT_CHARS + 1))
