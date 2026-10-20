"""Focused release checks using small stdlib-built archives, without a build backend."""

import base64
import csv
import hashlib
import io
import tarfile
import tomllib
from importlib.metadata import metadata, version
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.check_release_artifacts import (
    PACKAGE_MODULES,
    PROJECT_FILES,
    ArtifactContentError,
    check_release_artifacts,
)

VERSION = "0.8.0"
PACKAGE = f"tallydiff-{VERSION}"
INFO = f"{PACKAGE}.dist-info"
METADATA = (
    f"Metadata-Version: 2.4\nName: tallydiff\nVersion: {VERSION}\nRequires-Python: >=3.12\n"
    "Requires-Dist: openpyxl>=3.1.5\nRequires-Dist: streamlit>=1.65.0\n\n"
).encode()
WHEEL = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"


@pytest.fixture
def release(tmp_path):
    project = tmp_path / "project"
    files = {name: b"project content\n" for name in PROJECT_FILES}
    files["pyproject.toml"] = (
        f'[project]\nname = "tallydiff"\nversion = "{VERSION}"\n'
        'dependencies = ["openpyxl>=3.1.5", "streamlit>=1.65.0"]\n'
    ).encode()
    files.update({f"src/{name}": b"# runtime source\n" for name in PACKAGE_MODULES})
    files.update({"scripts/guard.py": b"# script\n", "tests/test_example.py": b"# test\n"})
    files.update({name: b"# subset developer source\n" for name in SUBSET_DEVELOPER_FILES})
    files.update({name: b"# review developer source\n" for name in REVIEW_DEVELOPER_FILES})
    for name, data in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    wheel = {name: files[f"src/{name}"] for name in PACKAGE_MODULES}
    wheel.update({f"{INFO}/METADATA": METADATA, f"{INFO}/WHEEL": WHEEL, f"{INFO}/RECORD": b""})
    source = {f"{PACKAGE}/{name}": data for name, data in files.items()}
    source[f"{PACKAGE}/PKG-INFO"] = METADATA
    return project, tmp_path / "dist", wheel, source


SUBSET_DEVELOPER_FILES = (
    "scripts/subset_oracle.py",
    "scripts/subset_benchmark.py",
    "tests/test_subset_oracle.py",
    "tests/test_subset_benchmark.py",
    "tests/test_realistic_one_to_many.py",
)
REVIEW_DEVELOPER_FILES = (
    "scripts/review_oracle.py",
    "scripts/review_benchmark.py",
    "tests/test_review_scale.py",
)
EXPECTED_RUNTIME_MODULES = frozenset(
    {
        "tallydiff/__init__.py",
        "tallydiff/_decimal.py",
        "tallydiff/amounts.py",
        "tallydiff/app.py",
        "tallydiff/configuration.py",
        "tallydiff/discovery.py",
        "tallydiff/engine.py",
        "tallydiff/export.py",
        "tallydiff/ingest.py",
        "tallydiff/models.py",
        "tallydiff/normalization.py",
        "tallydiff/normalization_config.py",
        "tallydiff/presentation.py",
        "tallydiff/profiles.py",
        "tallydiff/review.py",
        "tallydiff/subset_matching.py",
        "tallydiff/xlsx.py",
    }
)


def record_bytes(wheel):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, data in wheel.items():
        if name == f"{INFO}/RECORD":
            writer.writerow((name, "", ""))
        else:
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            writer.writerow((name, "sha256=" + digest, str(len(data))))
    return output.getvalue().encode()


def write_archives(release):
    _, dist, wheel, source = release
    dist.mkdir(exist_ok=True)
    if not wheel[f"{INFO}/RECORD"]:
        wheel[f"{INFO}/RECORD"] = record_bytes(wheel)
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


def test_installed_release_metadata_matches_unchanged_runtime_contract():
    assert version("tallydiff") == VERSION
    installed = metadata("tallydiff")
    assert installed["Requires-Python"] == ">=3.12"
    assert installed.get_all("Requires-Dist") == ["openpyxl>=3.1.5", "streamlit>=1.65.0"]
    assert not any(name.casefold().startswith("license") for name in installed.keys())


def test_subset_developer_sources_follow_existing_sdist_only_policy(release):
    project = Path(__file__).resolve().parents[1]
    targets = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "hatch"
    ]["build"]["targets"]
    assert "/scripts/*.py" in targets["sdist"]["include"]
    assert "/tests/*.py" in targets["sdist"]["include"]
    assert all((project / name).is_file() for name in SUBSET_DEVELOPER_FILES)
    _, dist, _, _ = release
    check(release)
    with ZipFile(dist / f"{PACKAGE}-py3-none-any.whl") as archive:
        assert not set(SUBSET_DEVELOPER_FILES) & set(archive.namelist())
    with tarfile.open(dist / f"{PACKAGE}.tar.gz", "r:gz") as archive:
        assert {f"{PACKAGE}/{name}" for name in SUBSET_DEVELOPER_FILES} <= set(archive.getnames())


def test_subset_matching_is_in_each_runtime_allowlist_exactly_once():
    project = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    targets = config["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["only-include"].count("src/tallydiff/subset_matching.py") == 1
    assert targets["sdist"]["include"].count("/src/tallydiff/subset_matching.py") == 1
    assert "tallydiff/subset_matching.py" in PACKAGE_MODULES


def test_release_freezes_exact_complete_runtime_module_allowlists():
    project = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    targets = config["tool"]["hatch"]["build"]["targets"]
    wheel_modules = targets["wheel"]["only-include"]
    source_modules = [name for name in targets["sdist"]["include"] if name.startswith("/src/")]
    assert PACKAGE_MODULES == EXPECTED_RUNTIME_MODULES
    assert set(wheel_modules) == {f"src/{name}" for name in EXPECTED_RUNTIME_MODULES}
    assert set(source_modules) == {f"/src/{name}" for name in EXPECTED_RUNTIME_MODULES}
    assert len(wheel_modules) == len(source_modules) == len(EXPECTED_RUNTIME_MODULES) == 17


def test_discovery_ships_exactly_once_in_both_archives(release):
    _, dist, _, _ = release
    check(release)
    with ZipFile(dist / f"{PACKAGE}-py3-none-any.whl") as archive:
        assert archive.namelist().count("tallydiff/discovery.py") == 1
    with tarfile.open(dist / f"{PACKAGE}.tar.gz", "r:gz") as archive:
        assert archive.getnames().count(f"{PACKAGE}/src/tallydiff/discovery.py") == 1


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_discovery_source_bytes_must_match_checkout(release, artifact):
    _, _, wheel, source = release
    if artifact == "wheel":
        wheel["tallydiff/discovery.py"] = b"# stale discovery profile"
    else:
        source[f"{PACKAGE}/src/tallydiff/discovery.py"] = b"# stale discovery profile"
    with pytest.raises(ArtifactContentError, match="differs from current project"):
        check(release)


def test_review_scale_developer_sources_follow_existing_sdist_only_policy(release):
    project = Path(__file__).resolve().parents[1]
    targets = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "hatch"
    ]["build"]["targets"]
    assert "/scripts/*.py" in targets["sdist"]["include"]
    assert "/tests/*.py" in targets["sdist"]["include"]
    assert all((project / name).is_file() for name in REVIEW_DEVELOPER_FILES)
    _, dist, _, _ = release
    check(release)
    with ZipFile(dist / f"{PACKAGE}-py3-none-any.whl") as archive:
        assert not set(REVIEW_DEVELOPER_FILES) & set(archive.namelist())
    with tarfile.open(dist / f"{PACKAGE}.tar.gz", "r:gz") as archive:
        assert {f"{PACKAGE}/{name}" for name in REVIEW_DEVELOPER_FILES} <= set(archive.getnames())


def test_subset_matching_ships_exactly_once_in_both_archives(release):
    _, dist, _, _ = release
    check(release)
    with ZipFile(dist / f"{PACKAGE}-py3-none-any.whl") as archive:
        assert archive.namelist().count("tallydiff/subset_matching.py") == 1
    with tarfile.open(dist / f"{PACKAGE}.tar.gz", "r:gz") as archive:
        assert archive.getnames().count(f"{PACKAGE}/src/tallydiff/subset_matching.py") == 1


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_subset_matching_source_bytes_must_match_checkout(release, artifact):
    _, _, wheel, source = release
    if artifact == "wheel":
        wheel["tallydiff/subset_matching.py"] = b"# stale subset search"
    else:
        source[f"{PACKAGE}/src/tallydiff/subset_matching.py"] = b"# stale subset search"
    with pytest.raises(ArtifactContentError, match="differs from current project"):
        check(release)


def test_review_is_in_each_runtime_allowlist_exactly_once():
    project = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    targets = config["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["only-include"].count("src/tallydiff/review.py") == 1
    assert targets["sdist"]["include"].count("/src/tallydiff/review.py") == 1
    assert "tallydiff/review.py" in PACKAGE_MODULES


def test_review_ships_exactly_once_in_both_archives(release):
    project, dist, _, _ = release
    checkout = Path(__file__).resolve().parents[1] / "src/tallydiff/review.py"
    data = checkout.read_bytes()
    (project / "src/tallydiff/review.py").write_bytes(data)
    release[2]["tallydiff/review.py"] = data
    release[3][f"{PACKAGE}/src/tallydiff/review.py"] = data
    check(release)
    with ZipFile(dist / f"{PACKAGE}-py3-none-any.whl") as archive:
        assert archive.namelist().count("tallydiff/review.py") == 1
        assert archive.read("tallydiff/review.py") == data
    with tarfile.open(dist / f"{PACKAGE}.tar.gz", "r:gz") as archive:
        name = f"{PACKAGE}/src/tallydiff/review.py"
        assert archive.getnames().count(name) == 1
        assert archive.extractfile(name).read() == data


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize("change", ["content", "line_endings"])
def test_review_source_bytes_must_match_checkout(release, artifact, change):
    changed = b"# stale review domain\n" if change == "content" else b"# runtime source\r\n"
    if artifact == "wheel":
        release[2]["tallydiff/review.py"] = changed
    else:
        release[3][f"{PACKAGE}/src/tallydiff/review.py"] = changed
    with pytest.raises(ArtifactContentError, match="differs from current project"):
        check(release)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_review_duplicate_archive_members_are_rejected(release, artifact):
    project, dist, _, _ = release
    write_archives(release)
    if artifact == "wheel":
        with ZipFile(dist / f"{PACKAGE}-py3-none-any.whl", "a") as archive:
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("tallydiff/review.py", b"# duplicate review domain\n")
    else:
        with tarfile.open(dist / f"{PACKAGE}.tar.gz", "w:gz") as archive:
            name = f"{PACKAGE}/src/tallydiff/review.py"
            for data in (b"# runtime source\n", b"# duplicate review domain\n"):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
    with pytest.raises(ArtifactContentError, match="Duplicate .* member"):
        check_release_artifacts(dist, project_root=project)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "name",
    [
        "%SystemDrive%/cache/data.bin",
        "benchmark_output/generated.csv",
        "benchmark_output/review_v07.json",
        "review_v07.json",
        "scripts/review_v07.json",
        ".pytest_cache/data",
        ".pytest-tmp/data",
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
        "Users/cheer/temp.tmp",
    ],
)
def test_local_artifacts_are_rejected_on_either_side(release, artifact, name):
    _, _, wheel, source = release
    if artifact == "wheel":
        wheel[name] = b"local junk"
    else:
        source[f"{PACKAGE}/{name}"] = b"local junk"
    with pytest.raises(ArtifactContentError, match="Local artifact|unexpected"):
        check(release)


@pytest.mark.parametrize(
    "name",
    [
        "tests/test_local.py",
        "scripts/local.py",
        "local-note.txt",
        "tallydiff/untracked_helper.py",
        "tallydiff/local-note.txt",
        "LICENSE",
    ],
)
def test_wheel_rejects_nonruntime_files(release, name):
    release[2][name] = b"unintended"
    with pytest.raises(ArtifactContentError, match="Local artifact|unexpected"):
        check(release)


def test_sdist_rejects_arbitrary_untracked_root_files(release):
    release[3][f"{PACKAGE}/local-note.txt"] = b"untracked"
    with pytest.raises(ArtifactContentError, match="unexpected"):
        check(release)


@pytest.mark.parametrize("name", ["LICENSE", "LICENSE.txt", "scripts/LICENSE.py"])
def test_sdist_rejects_unexpected_license(release, name):
    release[3][f"{PACKAGE}/{name}"] = b"unintended license"
    with pytest.raises(ArtifactContentError, match="Local artifact"):
        check(release)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "module",
    sorted(
        name.removeprefix("tallydiff/").removesuffix(".py") for name in EXPECTED_RUNTIME_MODULES
    ),
)
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
        (b"Version: 0.8.0", b"Version: 0.7.0"),
        (b"Name: tallydiff", b"Name: another-project"),
        (b"Requires-Python: >=3.12", b"Requires-Python: >=3.10"),
        (b"streamlit>=1.65.0", b"streamlit>=1.66.0"),
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


def test_wheel_must_remain_pure_python(release):
    release[2][f"{INFO}/WHEEL"] = WHEEL.replace(b"Root-Is-Purelib: true", b"Root-Is-Purelib: false")
    with pytest.raises(ArtifactContentError, match="Wheel metadata"):
        check(release)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "field",
    [
        "License: MIT",
        "License-Expression: MIT",
        "License-File: LICENSE",
        "Classifier: License :: OSI Approved :: MIT License",
    ],
)
def test_license_metadata_is_rejected(release, artifact, field):
    changed = METADATA.replace(b"\n\n", b"\n" + field.encode() + b"\n\n")
    if artifact == "wheel":
        release[2][f"{INFO}/METADATA"] = changed
    else:
        release[3][f"{PACKAGE}/PKG-INFO"] = changed
    with pytest.raises(ArtifactContentError, match="unlicensed"):
        check(release)


@pytest.mark.parametrize(
    "tamper",
    ["hash", "size", "omitted", "duplicate", "unknown", "self_hash", "fields", "encoding", "csv"],
)
def test_record_integrity_is_not_optional(release, tamper):
    wheel = release[2]
    rows = list(csv.reader(io.StringIO(record_bytes(wheel).decode())))
    index = next(index for index, row in enumerate(rows) if row[0] != f"{INFO}/RECORD")
    if tamper == "hash":
        rows[index][1] = "sha256=wrong"
    elif tamper == "size":
        rows[index][2] = str(int(rows[index][2]) + 1)
    elif tamper == "omitted":
        rows.pop(index)
    elif tamper == "duplicate":
        rows.append(rows[index])
    elif tamper == "unknown":
        rows.append(["outside.py", "sha256=wrong", "0"])
    elif tamper == "self_hash":
        next(row for row in rows if row[0] == f"{INFO}/RECORD")[1] = "sha256=wrong"
    elif tamper == "fields":
        rows[index].append("extra")
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    wheel[f"{INFO}/RECORD"] = (
        b"\xff"
        if tamper == "encoding"
        else b'"unterminated'
        if tamper == "csv"
        else output.getvalue().encode()
    )
    with pytest.raises(ArtifactContentError, match="RECORD"):
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


@pytest.mark.parametrize(
    "name",
    [
        "../outside.txt",
        "/absolute.txt",
        "dir\\file.txt",
        "C:/Users/cheer/local.txt",
        "C:relative.txt",
        f"{PACKAGE}/C:/Users/cheer/local.txt",
        f"{PACKAGE}/C:relative.txt",
    ],
)
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
