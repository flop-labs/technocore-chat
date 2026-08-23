from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from verify_mcp_dist import verify_artifacts

ROOT = Path(__file__).resolve().parents[1]
LEGAL_FILES = ("LICENSE", "NOTICE")


def _wheel(path: Path, *, correct_paths: bool) -> None:
    dist_info = "technocore_mcp-0.7.0.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: technocore-mcp\n"
            "Version: 0.7.0\n"
            "License-Expression: Apache-2.0\n"
            "License-File: LICENSE\n"
            "License-File: NOTICE\n",
        )
        for name in LEGAL_FILES:
            member = f"{dist_info}/licenses/{name}" if correct_paths else f"package_data/{name}"
            archive.writestr(member, (ROOT / name).read_bytes())


def _sdist(path: Path, *, correct_paths: bool) -> None:
    top = "technocore_mcp-0.7.0"
    with tarfile.open(path, "w:gz") as archive:
        for name in LEGAL_FILES:
            member = f"{top}/{name}" if correct_paths else f"{top}/unrelated/deep/{name}"
            payload = (ROOT / name).read_bytes()
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_distribution_verifier_accepts_exact_legal_file_locations(tmp_path):
    wheel = tmp_path / "technocore_mcp-0.7.0-py3-none-any.whl"
    sdist = tmp_path / "technocore_mcp-0.7.0.tar.gz"
    _wheel(wheel, correct_paths=True)
    _sdist(sdist, correct_paths=True)

    verify_artifacts([wheel, sdist])


def test_distribution_verifier_rejects_wheel_legal_files_in_package_data(tmp_path):
    wheel = tmp_path / "technocore_mcp-0.7.0-py3-none-any.whl"
    sdist = tmp_path / "technocore_mcp-0.7.0.tar.gz"
    _wheel(wheel, correct_paths=False)
    _sdist(sdist, correct_paths=True)

    with pytest.raises(ValueError, match="wheel legal-file paths"):
        verify_artifacts([wheel, sdist])


def test_distribution_verifier_rejects_nested_sdist_legal_files(tmp_path):
    wheel = tmp_path / "technocore_mcp-0.7.0-py3-none-any.whl"
    sdist = tmp_path / "technocore_mcp-0.7.0.tar.gz"
    _wheel(wheel, correct_paths=True)
    _sdist(sdist, correct_paths=False)

    with pytest.raises(ValueError, match="sdist legal-file paths"):
        verify_artifacts([wheel, sdist])


def test_ci_verifies_the_mcp_artifacts_after_building_them():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    build = workflow.index("uv build --project mcp")
    verify = workflow.index("uv run python tests/verify_mcp_dist.py")
    assert build < verify
