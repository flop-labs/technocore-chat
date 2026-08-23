from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
LEGAL_FILES = ("LICENSE", "NOTICE")


def _expected_bytes() -> dict[str, bytes]:
    return {name: (ROOT / name).read_bytes() for name in LEGAL_FILES}


def _verify_wheel(artifact: Path, expected: dict[str, bytes]) -> None:
    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError(f"wheel METADATA entries: {metadata}")

        metadata_path = PurePosixPath(metadata[0])
        dist_info = metadata_path.parent
        fields = archive.read(metadata[0]).decode().splitlines()
        expression = [
            line.partition(":")[2].strip()
            for line in fields
            if line.startswith("License-Expression:")
        ]
        if expression != ["Apache-2.0"]:
            raise ValueError(f"wheel License-Expression values: {expression}")

        declared = [
            line.partition(":")[2].strip() for line in fields if line.startswith("License-File:")
        ]
        if sorted(declared) != sorted(LEGAL_FILES):
            raise ValueError(f"wheel License-File values: {declared}")

        expected_paths = [str(dist_info / "licenses" / name) for name in LEGAL_FILES]
        legal_paths = [name for name in names if PurePosixPath(name).name in LEGAL_FILES]
        if sorted(legal_paths) != sorted(expected_paths):
            raise ValueError(f"wheel legal-file paths: {legal_paths}")
        for name, path in zip(LEGAL_FILES, expected_paths, strict=True):
            if archive.read(path) != expected[name]:
                raise ValueError(f"wheel {name} bytes differ from repository root")


def _verify_sdist(artifact: Path, expected: dict[str, bytes]) -> None:
    with tarfile.open(artifact, "r:gz") as archive:
        members = archive.getmembers()
        top_levels = {PurePosixPath(member.name).parts[0] for member in members if member.name}
        if len(top_levels) != 1:
            raise ValueError(f"sdist top-level directories: {sorted(top_levels)}")
        top = next(iter(top_levels))
        expected_paths = [f"{top}/{name}" for name in LEGAL_FILES]
        legal_members = [
            member for member in members if PurePosixPath(member.name).name in LEGAL_FILES
        ]
        legal_paths = [member.name for member in legal_members]
        if sorted(legal_paths) != sorted(expected_paths):
            raise ValueError(f"sdist legal-file paths: {legal_paths}")
        by_path = {member.name: member for member in legal_members}
        for name, path in zip(LEGAL_FILES, expected_paths, strict=True):
            extracted = archive.extractfile(by_path[path])
            if extracted is None or extracted.read() != expected[name]:
                raise ValueError(f"sdist {name} bytes differ from repository root")


def verify_artifacts(artifacts: list[Path]) -> None:
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(artifacts) != 2:
        raise ValueError(f"expected one wheel and one sdist, got wheels={wheels}, sdists={sdists}")
    expected = _expected_bytes()
    _verify_wheel(wheels[0], expected)
    _verify_sdist(sdists[0], expected)


def main(argv: list[str]) -> int:
    artifacts = [Path(value) for value in argv]
    try:
        verify_artifacts(artifacts)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"MCP distribution verification failed: {exc}", file=sys.stderr)
        return 1
    print("MCP distributions contain exact LICENSE and NOTICE files with matching metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
