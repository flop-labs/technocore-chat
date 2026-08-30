"""Regression tests for #581 and #582."""

import os
import subprocess
import sys


def test_reserved_room_bytes_zero_refuses_to_boot():
    """CHAT_MAX_ROOMS large enough to make RESERVED_ROOM_BYTES zero must not boot (#581)."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    run = subprocess.run(
        [sys.executable, "-c", "import store"],
        capture_output=True,
        text=True,
        env={**clean, "CHAT_MAX_ROOMS": "10000000000"},
        cwd=os.path.join(os.path.dirname(__file__), "..", "..", "src"),
    )
    assert run.returncode != 0, "app booted with RESERVED_ROOM_BYTES == 0"
    assert "ValueError" in run.stderr


def test_sitemap_xml_escapes_ampersand():
    """CHAT_PUBLIC_URL with & must produce well-formed XML (#582)."""
    from manifest import sitemap_xml

    xml = sitemap_xml("https://example.com?a=1&b=2")
    assert "&amp;" in xml, "bare & must be escaped as &amp;"
    assert "?a=1&b=2" not in xml, "bare & must not appear in <loc>"
