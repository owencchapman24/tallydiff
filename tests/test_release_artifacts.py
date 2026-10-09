"""Focused release checks using small stdlib-built archives, without a build backend."""

import io
import tarfile
from zipfile import ZipFile

import pytest

from scripts.check_release_artifacts import (
    PACKAGE_MODULES,
    PROJECT_FILES,
    ArtifactContentError,
    check_release_artifacts,
)

VERSION = "0.5.0"
PACKAGE = f"tallydiff-{VERSION}"
INFO = f"{PACKAGE}.dist-info"
METADATA = (
    f"Metadata-Version: 2.4\nName: tallydiff\nVersion: {VERSION}\nRequires-Python: >=3.12\n\n"
).encode()
WHEEL = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"


@pytest.fixture
def release(tmp_path):
    project = tmp_path / "project"
    files = {name: b"project content\n" for name in PROJECT_FILES}
    files["pyproject.toml"] = f'[project]\nname = "tallydiff"\nversion = "{VERSION}"\n'.encode()
    files.update({f"src/{name}": b"# runtime source\n" for name in PACKAGE_MODULES})
    files.update({"scripts/guard.py": b"# script\n", "tests/test_example.py": b"# test\n"})
    for name, data in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    wheel = {name: files[f"src/{name}"] for name in PACKAGE_MODULES}
    wheel.update({f"{INFO}/METADATA": METADATA, f"{INFO}/WHEEL": WHEEL, f"{INFO}/RECORD": b""})
    source = {f"{PACKAGE}/{name}": data for name, data in files.items()}
    source[f"{PACKAGE}/PKG-INFO"] = METADATA
    return project, tmp_path / "dist", wheel, source


def write_archives(release):
    _, dist, wheel, source = release
    dist.mkdir(exist_ok=True)
    with ZipFile(dist / f"{PACKAGE}-py3-none-any.whl", "w") as archive:
        for name, data in wheel.items():
            archive.writestr(name, data)
    with tarfile.open(dist / f"{PACKAGE}.tar.gz", "w:gz") as archive:
        for name, data in source.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def check(release):
    project, dist, _, _ = release
    write_archives(release)
    return check_release_artifacts(dist, project_root=project)


def test_valid_artifacts_preserve_runtime_and_repository_files(release):
    _, _, wheel, source = release
    assert check(release) == (len(wheel), len(source))


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "name",
    [
        "%SystemDrive%/cache/data.bin",
        "benchmark_output/generated.csv",
        ".pytest_cache/data",
        ".ruff_cache/data",
        ".uv-cache/data",
        ".venv/pyvenv.cfg",
        "build/temp.py",
        "dist/old.whl",
        ".vscode/settings.json",
        ".idea/workspace.xml",
        "src/tallydiff/__pycache__/app.pyc",
        "review.patch",
        "review.diff",
    ],
)
def test_local_artifacts_are_rejected_on_either_side(release, artifact, name):
    _, _, wheel, source = release
    if artifact == "wheel":
        wheel[name] = b"local junk"
    else:
        source[f"{PACKAGE}/{name}"] = b"local junk"
    with pytest.raises(ArtifactContentError, match="Local artifact"):
        check(release)


@pytest.mark.parametrize(
    "name",
    [
        "tests/test_local.py",
        "scripts/local.py",
        "local-note.txt",
        "tallydiff/untracked_helper.py",
        "tallydiff/local-note.txt",
    ],
)
def test_wheel_rejects_nonruntime_files(release, name):
    release[2][name] = b"unintended"
    with pytest.raises(ArtifactContentError, match="unexpected"):
        check(release)


def test_sdist_rejects_arbitrary_untracked_root_files(release):
    release[3][f"{PACKAGE}/local-note.txt"] = b"untracked"
    with pytest.raises(ArtifactContentError, match="unexpected"):
        check(release)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize("module", ["engine", "normalization", "normalization_config"])
def test_required_runtime_module_cannot_be_omitted(release, artifact, module):
    _, _, wheel, source = release
    del (wheel if artifact == "wheel" else source)[
        f"tallydiff/{module}.py" if artifact == "wheel" else f"{PACKAGE}/src/tallydiff/{module}.py"
    ]
    with pytest.raises(ArtifactContentError, match="missing"):
        check(release)


@pytest.mark.parametrize("name", ["README.md", "pyproject.toml", "sample_data/file_a.csv"])
def test_required_project_content_cannot_be_omitted(release, name):
    del release[3][f"{PACKAGE}/{name}"]
    with pytest.raises(ArtifactContentError, match="missing"):
        check(release)


@pytest.mark.parametrize("name", ["untracked_helper.py", "local-note.txt"])
def test_sdist_rejects_arbitrary_package_local_files(release, name):
    release[3][f"{PACKAGE}/src/tallydiff/{name}"] = b"unintended"
    with pytest.raises(ArtifactContentError, match="unexpected"):
        check(release)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "old,new",
    [
        (b"Version: 0.5.0", b"Version: 0.3.0"),
        (b"Name: tallydiff", b"Name: another-project"),
        (b"Requires-Python: >=3.12", b"Requires-Python: >=3.10"),
    ],
)
def test_invalid_package_metadata_is_rejected(release, artifact, old, new):
    _, _, wheel, source = release
    if artifact == "wheel":
        wheel[f"{INFO}/METADATA"] = METADATA.replace(old, new)
    else:
        source[f"{PACKAGE}/PKG-INFO"] = METADATA.replace(old, new)
    with pytest.raises(ArtifactContentError, match="metadata"):
        check(release)


def test_invalid_wheel_tag_is_rejected(release):
    release[2][f"{INFO}/WHEEL"] = WHEEL.replace(b"py3-none-any", b"cp312-none-any")
    with pytest.raises(ArtifactContentError, match="Wheel metadata"):
        check(release)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_archived_source_must_match_current_checkout(release, artifact):
    _, _, wheel, source = release
    if artifact == "wheel":
        wheel["tallydiff/engine.py"] = b"# stale runtime"
    else:
        source[f"{PACKAGE}/README.md"] = b"stale documentation"
    with pytest.raises(ArtifactContentError, match="differs from current project"):
        check(release)


@pytest.mark.parametrize("name", ["../outside.txt", "/absolute.txt", "dir\\file.txt"])
@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_unsafe_archive_names_are_rejected_without_extraction(release, name, artifact):
    release[2 if artifact == "wheel" else 3][name] = b"unsafe"
    with pytest.raises(ArtifactContentError, match="Invalid archive path|unexpected"):
        check(release)


def test_sdist_rejects_symbolic_links(release):
    project, dist, _, _ = release
    write_archives(release)
    with tarfile.open(dist / f"{PACKAGE}.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo(f"{PACKAGE}/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        archive.addfile(member)
    with pytest.raises(ArtifactContentError, match="Invalid source-distribution member"):
        check_release_artifacts(dist, project_root=project)
