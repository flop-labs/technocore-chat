"""Supply-chain invariants for .github/workflows, asserted rather than reviewed.

Every third-party action this repository runs is pinned to a full commit SHA, and the
Dockerfiles pin their bases by digest. Both are stated as policy in comments inside those
files -- and a policy that lives only in a comment is the one that drifts: `queue-guard.yml`
shipped `actions/github-script@v7` while every other workflow carried a 40-character pin,
under `pull_request_target` with a write-capable token, which is the single worst place in
the repository for a mutable reference. A tag can be repointed at any commit by whoever
controls the repository it lives in, so a mutable tag there is arbitrary code with this
repository's secrets.

Run: uv run --group dev python -m pytest tests/test_workflow_pins.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = sorted((_ROOT / ".github" / "workflows").glob("*.yml"))

# `uses: owner/repo@ref` or `uses: owner/repo/path@ref`, ignoring any trailing `# vX.Y.Z`
# comment. Local actions (`./.github/actions/...`) and reusable workflows in this repository
# are not third-party and are excluded below by the same leading-dot / no-slash checks.
_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<ref>[^\s#]+)", re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def test_there_are_workflows_to_check():
    """A glob that silently matches nothing would make every assertion below vacuous."""
    assert _WORKFLOWS, "no workflow files found — the path in this test is wrong"


@pytest.mark.parametrize("path", _WORKFLOWS, ids=lambda p: p.name)
def test_every_third_party_action_is_pinned_to_a_full_commit_sha(path):
    """A 40-hex commit is the only immutable form; `@v7` and `@main` are not."""
    unpinned = []
    for match in _USES_RE.finditer(path.read_text(encoding="utf-8")):
        ref = match.group("ref")
        if ref.startswith(".") or "@" not in ref:
            continue  # a local action or a reusable workflow in this repository
        _, _, version = ref.partition("@")
        if not _SHA_RE.match(version):
            unpinned.append(ref)
    assert not unpinned, f"{path.name}: not pinned to a commit SHA: {', '.join(unpinned)}"


@pytest.mark.parametrize("path", _WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_declares_its_token_permissions(path):
    """The default token permissions are whatever the repository setting says, which is not
    a property of this file and can be widened without touching it. Declaring the grant
    explicitly is what makes it reviewable in the diff that introduces a step needing it."""
    assert "permissions:" in path.read_text(encoding="utf-8"), (
        f"{path.name}: no `permissions:` block — the job runs with the repository default"
    )


@pytest.mark.parametrize(
    "dockerfile", ["docker/Dockerfile", "mcp/Dockerfile"], ids=lambda p: p.replace("/", ":")
)
def test_every_container_image_reference_is_pinned_by_digest(dockerfile):
    """`FROM` and `COPY --from=` both pull an image, and a tag on either is mutable.

    `COPY --from=` is the one that hides: it looks like a file copy, so a tag there reads as
    a version rather than as a fetch of someone else's binary into the build. The shipped
    image copied `ghcr.io/astral-sh/uv:0.12.3` that way, in a file whose own opening comment
    reads "Pinned by digest, not by tag".
    """
    text = (_ROOT / dockerfile).read_text(encoding="utf-8")
    refs = re.findall(r"^\s*(?:FROM|COPY\s+--from=)\s*(\S+)", text, re.MULTILINE)
    assert refs, (
        f"{dockerfile}: no image references found — this test is looking in the wrong place"
    )
    unpinned = [
        r
        for r in refs
        # A bare stage name (`COPY --from=build`) has no registry path and pulls nothing.
        if "/" in r and "@sha256:" not in r
    ]
    assert not unpinned, f"{dockerfile}: not pinned by digest: {', '.join(unpinned)}"
