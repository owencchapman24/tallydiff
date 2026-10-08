"""Inspect release archives without extracting them; run after a normal isolated uv build."""

import argparse
import tarfile
import tomllib
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
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
        "engine",
        "export",
        "ingest",
        "models",
        "normalization",
        "normalization_config",
        "presentation",
        "profiles",
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
        or "\\" in value
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ArtifactContentError(f"Invalid archive path: {name!r}")
    for part in path.parts:
        lower = part.casefold()
        if (
            lower in LOCAL_PARTS
            or lower == ".env"
            or lower.startswith(".env.")
            or lower.startswith(".coverage")
            or lower.endswith(".egg-info")
            or PurePosixPath(lower).suffix in LOCAL_SUFFIXES
        ):
            raise ArtifactContentError(f"Local artifact in release archive: {name}")
    return value


def _metadata(data: bytes, version: str) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(data)
    if metadata.get("Name") != "tallydiff" or metadata.get("Version") != version:
        raise ArtifactContentError(f"Package metadata must identify tallydiff {version}")
    if not metadata.get("Metadata-Version") or metadata.get("Requires-Python") != ">=3.12":
        raise ArtifactContentError("Package metadata lacks valid metadata/Python requirements")


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
    _metadata(wheel_files[f"{info}/METADATA"], version)
    wheel_metadata = BytesParser(policy=policy.default).parsebytes(wheel_files[f"{info}/WHEEL"])
    if wheel_metadata.get("Wheel-Version") != "1.0" or wheel_metadata.get("Tag") != "py3-none-any":
        raise ArtifactContentError("Wheel metadata must describe the pure-Python wheel")
    for name in PACKAGE_MODULES:
        if wheel_files[name] != (project_root / "src" / name).read_bytes():
            raise ArtifactContentError(f"Wheel source differs from current project: {name}")

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
    _metadata(source_files["PKG-INFO"], version)
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
