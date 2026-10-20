"""Inspect release archives without extracting them; run after a normal isolated uv build."""

import argparse
import base64
import csv
import hashlib
import io
import tarfile
import tomllib
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_MODULES = frozenset(
    f"tallydiff/{name}.py"
    for name in (
        "__init__",
        "_decimal",
        "amounts",
        "app",
        "configuration",
        "discovery",
        "engine",
        "export",
        "ingest",
        "models",
        "normalization",
        "normalization_config",
        "presentation",
        "profiles",
        "review",
        "subset_matching",
        "xlsx",
    )
)
PROJECT_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "README.md",
        ".gitignore",
        ".github/workflows/ci.yml",
        ".streamlit/config.toml",
        "docs/tallydiff-demo.png",
        "sample_data/file_a.csv",
        "sample_data/file_b.csv",
    }
)
LOCAL_PARTS = frozenset(
    {
        "%systemdrive%",
        "benchmark_output",
        "__pycache__",
        ".pytest_cache",
        ".pytest-tmp",
        ".ruff_cache",
        ".uv-cache",
        ".venv",
        "venv",
        "build",
        "dist",
        ".vscode",
        ".idea",
        ".codex",
        ".agents",
        ".git",
        ".aws",
        ".ds_store",
        "htmlcov",
    }
)
LOCAL_SUFFIXES = frozenset({".pyc", ".pyo", ".patch", ".diff", ".orig", ".rej"})


class ArtifactContentError(ValueError):
    """A release artifact is missing intended content or includes unintended files."""


def _safe_name(name: str) -> str:
    value = name.removesuffix("/")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or PureWindowsPath(value).drive
        or "\\" in value
        or ".." in path.parts
        or any(PureWindowsPath(part).drive for part in path.parts)
        or path.as_posix() != value
    ):
        raise ArtifactContentError(f"Invalid archive path: {name!r}")
    for part in path.parts:
        lower = part.casefold()
        if (
            lower in LOCAL_PARTS
            or lower == "review_v07.json"
            or lower == "license"
            or lower.startswith("license.")
            or lower == ".env"
            or lower.startswith(".env.")
            or lower.startswith(".coverage")
            or lower.endswith(".egg-info")
            or PurePosixPath(lower).suffix in LOCAL_SUFFIXES
        ):
            raise ArtifactContentError(f"Local artifact in release archive: {name}")
    return value


def _metadata(data: bytes, version: str, dependencies: list[str]) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(data)
    if metadata.get("Name") != "tallydiff" or metadata.get("Version") != version:
        raise ArtifactContentError(f"Package metadata must identify tallydiff {version}")
    if not metadata.get("Metadata-Version") or metadata.get("Requires-Python") != ">=3.12":
        raise ArtifactContentError("Package metadata lacks valid metadata/Python requirements")
    if sorted(metadata.get_all("Requires-Dist", [])) != sorted(dependencies):
        raise ArtifactContentError("Package metadata runtime dependencies differ from the project")
    if any(name.casefold().startswith("license") for name in metadata.keys()) or any(
        classifier.startswith("License ::") for classifier in metadata.get_all("Classifier", [])
    ):
        raise ArtifactContentError(
            "Package metadata must preserve the unlicensed repository policy"
        )


def _record(files: dict[str, bytes], record_name: str) -> None:
    """Require one RECORD entry per member, with SHA-256 and byte-size integrity."""
    try:
        rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8")), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise ArtifactContentError("Wheel RECORD is not valid UTF-8 CSV") from exc
    seen = set()
    for row in rows:
        if len(row) != 3:
            raise ArtifactContentError("Wheel RECORD entries must have three fields")
        name, digest, size = row
        if name not in files or name in seen:
            raise ArtifactContentError("Wheel RECORD contains an unknown or duplicate member")
        seen.add(name)
        if name == record_name:
            if digest or size:
                raise ArtifactContentError("Wheel RECORD must leave its own hash and size blank")
        else:
            expected = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=")
            if digest != "sha256=" + expected.decode("ascii") or size != str(len(files[name])):
                raise ArtifactContentError(f"Wheel RECORD hash/size mismatch: {name}")
    if seen != files.keys():
        raise ArtifactContentError("Wheel RECORD omits archive members")


def _check_files(files: dict[str, bytes], expected: set[str], label: str) -> None:
    missing, extra = expected - files.keys(), files.keys() - expected
    if missing or extra:
        raise ArtifactContentError(
            f"{label}: missing={sorted(missing)}, unexpected={sorted(extra)}"
        )


def check_release_artifacts(dist_dir: Path, *, project_root: Path = ROOT) -> tuple[int, int]:
    """Check exact file sets, package versions, and source bytes for both artifacts."""
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    dependencies = project["project"].get("dependencies", [])
    package_id = f"tallydiff-{version}"
    info = f"{package_id}.dist-info"
    wheel_files = {}
    with ZipFile(dist_dir / f"{package_id}-py3-none-any.whl") as wheel:
        for member in wheel.infolist():
            name = _safe_name(member.filename)
            if member.is_dir():
                raise ArtifactContentError(f"Unexpected wheel directory entry: {name}")
            if name in wheel_files:
                raise ArtifactContentError(f"Duplicate wheel member: {name}")
            wheel_files[name] = wheel.read(member)
    expected_wheel = set(PACKAGE_MODULES) | {
        f"{info}/{name}" for name in ("METADATA", "WHEEL", "RECORD")
    }
    _check_files(wheel_files, expected_wheel, "Wheel")
    _metadata(wheel_files[f"{info}/METADATA"], version, dependencies)
    wheel_metadata = BytesParser(policy=policy.default).parsebytes(wheel_files[f"{info}/WHEEL"])
    if (
        wheel_metadata.get("Wheel-Version") != "1.0"
        or wheel_metadata.get_all("Tag") != ["py3-none-any"]
        or wheel_metadata.get("Root-Is-Purelib") != "true"
    ):
        raise ArtifactContentError("Wheel metadata must describe the pure-Python wheel")
    for name in PACKAGE_MODULES:
        if wheel_files[name] != (project_root / "src" / name).read_bytes():
            raise ArtifactContentError(f"Wheel source differs from current project: {name}")
    _record(wheel_files, f"{info}/RECORD")

    source_files = {}
    with tarfile.open(dist_dir / f"{package_id}.tar.gz", mode="r:gz") as sdist:
        for member in sdist.getmembers():
            name = _safe_name(member.name)
            prefix = f"{package_id}/"
            if not name.startswith(prefix) or not member.isfile():
                raise ArtifactContentError(f"Invalid source-distribution member: {name}")
            relative = name[len(prefix) :]
            if relative in source_files:
                raise ArtifactContentError(f"Duplicate source-distribution member: {name}")
            source_files[relative] = sdist.extractfile(member).read()
    expected_source = set(PROJECT_FILES) | {f"src/{name}" for name in PACKAGE_MODULES}
    for directory in ("scripts", "tests"):
        expected_source.update(
            file.relative_to(project_root).as_posix()
            for file in (project_root / directory).glob("*.py")
        )
    _check_files(source_files, expected_source | {"PKG-INFO"}, "Source distribution")
    _metadata(source_files["PKG-INFO"], version, dependencies)
    for name in expected_source:
        if source_files[name] != (project_root / name).read_bytes():
            raise ArtifactContentError(f"Source distribution differs from current project: {name}")
    return len(wheel_files), len(source_files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        wheel_count, source_count = check_release_artifacts(args.dist_dir)
    except (OSError, ValueError, tarfile.TarError, BadZipFile) as exc:
        parser.exit(1, f"Release artifact check failed: {exc}\n")
    print(f"Release artifacts passed: wheel {wheel_count} files; sdist {source_count} files.")


if __name__ == "__main__":
    main()
