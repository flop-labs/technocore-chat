"""Run: uv run --group dev python -m pytest tests"""

import config


def test_dbg_is_silent_below_the_configured_level(capsys):
    """`_dbg` costs one comparison when suppressed (module docstring: "these sit on the
    hottest paths in the service") — verify the suppressed side actually stays silent,
    not just cheap.
    """
    with config.override(DEBUG=1):
        config._dbg(2, "take", ip="1.2.3.4", left=3)
    assert capsys.readouterr().err == ""


def test_dbg_emits_at_and_above_the_configured_level(capsys):
    """DEBUG >= level emits one line to stderr: `event key=value ...`, in call order.
    `DEBUG == level` is the boundary in the comparison (`if DEBUG < level: return`), so
    both the equal and the strictly-greater case are pinned here, not just one.
    """
    with config.override(DEBUG=2):
        config._dbg(2, "take", ip="1.2.3.4", left=3)
        config._dbg(1, "refund", ip="1.2.3.4")
    err = capsys.readouterr().err.splitlines()
    assert err == ["take ip=1.2.3.4 left=3", "refund ip=1.2.3.4"]


def test_dbg_never_writes_stdout(capsys):
    """stderr is operator territory; stdout is the HTTP response body's channel. A debug
    line on stdout would eventually leak into a response, not just a log.
    """
    with config.override(DEBUG=3):
        config._dbg(3, "compact", room="lobby", seq=42)
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "compact room=lobby seq=42\n"
