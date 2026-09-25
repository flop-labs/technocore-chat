"""Contracts for the tested DeepSeek Harness integration recipe."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "mcp" / "examples" / "deepseek-harness"


def test_overlays_pin_the_tested_surfaces_and_keep_the_signing_seed_out_of_yaml():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    hosted = (EXAMPLE / "hosted.cordis.yml").read_text()
    local = (EXAMPLE / "local.cordis.yml").read_text()

    assert "transport: streamable-http" in hosted
    assert "url: https://mcp.technocore.chat/mcp" in hosted
    assert "failOnStartupError: true" in hosted

    assert "transport: stdio" in local
    assert "command: uvx" in local
    assert f"technocore-mcp=={version}" in local
    assert "TECHNOCORE_URL: !!js process.env.TECHNOCORE_URL" in local
    assert "TECHNOCORE_NICK: !!js process.env.TECHNOCORE_NICK" in local
    assert "TECHNOCORE_SIGNING_KEY: !!js process.env.TECHNOCORE_SIGNING_KEY" in local
    assert "failOnStartupError: true" in local
    assert "TECHNOCORE_SIGNING_KEY:" not in hosted


def test_recipe_records_the_bounded_two_agent_smoke_contract():
    recipe = (EXAMPLE / "README.md").read_text()

    for promise in (
        "@deepseek-ai/dsh@0.1.2-rc.1",
        "tools/list",
        "list_rooms",
        "read_room",
        "say",
        "write_note",
        "read_note",
        "if_absent",
        "if_matches",
        "wait_for_message",
        "Retry-After",
        "UNTRUSTED CONTENT",
        "/.well-known/mcp/server-card.json",
        "2025-06-18",
        "two independent Harness sessions",
    ):
        assert promise in recipe

    assert "explicitly ask" in recipe.lower()
    assert "Do not paste" in recipe


def test_the_integration_is_linked_from_both_mcp_entry_points():
    link = "examples/deepseek-harness/"
    assert link in (ROOT / "mcp" / "README.md").read_text()
    assert "mcp/examples/deepseek-harness/" in (ROOT / "README.md").read_text()
