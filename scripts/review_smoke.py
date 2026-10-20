"""Smoke the exact installed release wheel, from an isolated environment outside checkout.

Copy this file into a temporary directory, install locked runtime requirements and
the built wheel into a fresh virtual environment, then run its Python with ``-I``.
Only the standard library and TallyDiff's root public API are used.
"""

import argparse
import csv
import hashlib
import json
import sys
from decimal import Decimal
from importlib.metadata import distribution
from io import StringIO
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
from zipfile import ZipFile

import tallydiff
from tallydiff import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    ColumnMapping,
    CorrespondenceStatus,
    FindingCategory,
    FindingReview,
    ReconciliationMode,
    ReviewDisposition,
    ReviewSession,
    Source,
    bind_review_result,
    export_exceptions_csv,
    export_mapping_profile,
    export_review_session,
    export_reviewed_exceptions_csv,
    ingest_csv,
    load_mapping_profile,
    load_review_session,
    reconcile,
)

BASE_HEADERS = [
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
]
REVIEW_HEADERS = ["Review disposition", "Review note"]
MAPPING = ColumnMapping((("id", "id"),), "amount", "amount")
FOLLOW_UP_KEY = ("=1+2",)
NOTE = '=1+2\n\u78ba\u8a8d caf\u00e9 \U0001f600\r\nFollow-up with "quotes" and a comma, intact.'


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _provenance(wheel: Path, checkout: Path, expected_version: str) -> dict[str, str | int]:
    wheel = wheel.resolve(strict=True)
    checkout = checkout.resolve(strict=True)
    _check(wheel.suffix == ".whl", "Expected an actual built wheel artifact")
    _check(sys.flags.isolated == 1, "Run the smoke with Python -I")
    _check(sys.prefix != sys.base_prefix, "Run the smoke in a fresh virtual environment")
    package_path = Path(tallydiff.__file__).resolve(strict=True)
    for path in (Path.cwd(), Path(__file__), Path(sys.prefix), package_path):
        _check(not path.resolve().is_relative_to(checkout), f"Path is inside checkout: {path}")
    for entry in sys.path:
        _check(
            not Path(entry).resolve().is_relative_to(checkout),
            f"Import search path is inside checkout: {entry}",
        )
    installed = distribution("tallydiff")
    _check(installed.version == expected_version, "Installed distribution version differs")
    _check(
        package_path == Path(installed.locate_file("tallydiff/__init__.py")).resolve(strict=True),
        "Imported package does not belong to the installed distribution",
    )
    _check(
        package_path.is_relative_to(Path(sys.prefix).resolve()),
        "Imported package is outside the isolated virtual environment",
    )
    direct_url_text = installed.read_text("direct_url.json")
    _check(direct_url_text is not None, "Installed wheel provenance is missing")
    direct_url = json.loads(direct_url_text)
    _check("dir_info" not in direct_url, "Installation came from a directory or editable project")
    url = urlsplit(direct_url["url"])
    _check(url.scheme == "file" and url.netloc in ("", "localhost"), "Expected local wheel URL")
    installed_from = Path(url2pathname(url.path)).resolve(strict=True)
    _check(installed_from == wheel, "Installed distribution came from a different artifact")
    wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    recorded_hash = direct_url.get("archive_info", {}).get("hashes", {}).get("sha256")
    if recorded_hash is not None:
        _check(recorded_hash == wheel_hash, "Installed wheel provenance SHA256 differs")
    # uv can leave archive_info empty even after a hash-required wheel install.
    # Prove the installed runtime bytes belong to the exact artifact independently.
    with ZipFile(wheel) as archive:
        package_members = [
            member
            for member in archive.infolist()
            if member.filename.startswith("tallydiff/") and not member.is_dir()
        ]
        names = {member.filename for member in package_members}
        _check(len(names) == len(package_members), "Wheel has duplicate runtime members")
        _check("tallydiff/__init__.py" in names, "Wheel is missing the package entry point")
        installed_modules = {
            str(member)
            for member in installed.files or ()
            if str(member).startswith("tallydiff/") and str(member).endswith(".py")
        }
        _check(
            installed_modules == {name for name in names if name.endswith(".py")},
            "Installed runtime module set differs from the wheel",
        )
        for member in package_members:
            installed_path = Path(installed.locate_file(member.filename)).resolve(strict=True)
            _check(
                installed_path.is_relative_to(Path(sys.prefix).resolve()),
                f"Installed runtime member is outside the virtual environment: {member.filename}",
            )
            _check(
                installed_path.read_bytes() == archive.read(member),
                f"Installed runtime bytes differ from the exact wheel: {member.filename}",
            )
    return {
        "version": installed.version,
        "python": sys.version.split()[0],
        "package_path": str(package_path),
        "wheel": str(wheel),
        "wheel_sha256": wheel_hash,
        "installed_runtime_members_verified": len(package_members),
    }


def _records(data: bytes, source: Source):
    return ingest_csv(
        data.decode("utf-8"), source=source, key_columns=("id",), amount_column="amount"
    )


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(StringIO(data.decode("utf-8"), newline="")))


def _unique_review() -> dict[str, int]:
    data_a = b"id,amount\n=1+2,12.00\ncomplete,5.00\nclean,2.00\n"
    data_b = b"id,amount\nclean,2.00\ncomplete,4.00\n=1+2,10.00\n"
    result = reconcile(_records(data_a, Source.A), _records(data_b, Source.B))
    _check(result.mode is ReconciliationMode.UNIQUE, "Default reconciliation is not UNIQUE")
    _check(len(result.exceptions) == 2, "Expected exactly two UNIQUE exceptions")
    original = export_exceptions_csv(result)
    original_rows = _csv_rows(original)
    _check(original_rows[0] == BASE_HEADERS, "Original exception evidence headers changed")
    _check(
        original_rows[1:]
        == [
            ["Amount mismatch", "=1+2", "12.00", "10.00", "+2.00", "2", "4"],
            ["Amount mismatch", "complete", "5.00", "4.00", "+1.00", "3", "3"],
        ],
        "Original exception CSV evidence cells differ from the fixture",
    )
    binding = bind_review_result(
        result, data_a=data_a, data_b=data_b, mapping=MAPPING, format_a="csv", format_b="csv"
    )
    _check(binding.result is result, "Binding changed the reconciliation result")
    session = ReviewSession.unreviewed(binding)
    _check(session.total == session.unreviewed_count == 2, "Default annotations are incomplete")
    session = session.with_review(
        FindingReview(FOLLOW_UP_KEY, ReviewDisposition.NEEDS_FOLLOW_UP, NOTE)
    ).with_review(FindingReview(("complete",), ReviewDisposition.REVIEW_COMPLETE))
    _check(
        (session.unreviewed_count, session.needs_follow_up_count, session.complete_count)
        == (0, 1, 1),
        "Replacement annotations or workflow counts differ",
    )
    session_json = export_review_session(session)
    document = json.loads(session_json)
    _check(
        document["format"] == "tallydiff-review-session" and document["version"] == 1,
        "Review-session schema identity changed",
    )
    _check(document["reviews"][0]["note"] == NOTE, "JSON changed the exact analyst note")
    restored = load_review_session(session_json, binding=binding)
    _check(restored.binding is binding and restored == session, "Session restore changed state")
    _check(export_review_session(restored) == session_json, "JSON round-trip is not byte exact")
    reviewed_csv = export_reviewed_exceptions_csv(binding, restored)
    reviewed_rows = _csv_rows(reviewed_csv)
    _check(reviewed_rows[0] == BASE_HEADERS + REVIEW_HEADERS, "Reviewed CSV headers changed")
    _check(
        [row[:-2] for row in reviewed_rows[1:]] == original_rows[1:],
        "Reviewed CSV changed original evidence cells",
    )
    _check(
        [row[-2:] for row in reviewed_rows[1:]]
        == [["Needs follow-up", "Note: " + NOTE], ["Review complete", ""]],
        "Reviewed CSV labels, exact note, or literal Note: prefix differ",
    )
    _check(export_exceptions_csv(result) == original, "Analyst review changed original CSV bytes")
    return {
        "unique_exceptions": session.total,
        "review_json_bytes": len(session_json),
        "reviewed_csv_bytes": len(reviewed_csv),
    }


def _bounded_and_profile() -> dict[str, int]:
    data_a = b"id,amount\ninferred,3\n"
    data_b = b"id,amount\ninferred,1\ninferred,2\n"
    result = reconcile(
        _records(data_a, Source.A),
        _records(data_b, Source.B),
        mode=ReconciliationMode.BOUNDED_ONE_TO_MANY,
    )
    _check(len(result.exceptions) == 1, "Bounded inferred match ceased to be an exception")
    finding = result.exceptions[0]
    _check(finding.category is FindingCategory.EXACT_MATCH, "Expected inferred Exact match")
    _check(finding.is_exception, "Inferred correspondence no longer requires review")
    analysis = finding.correspondence_analysis
    _check(analysis is not None, "Stored bounded correspondence analysis is absent")
    _check(analysis.status is CorrespondenceStatus.UNIQUE_EXACT, "Inference was not unique exact")
    _check(analysis.accepted_solution is not None, "Accepted bounded subset is absent")
    _check(
        analysis.policy is EXACT_UNIQUE_ONE_TO_MANY_POLICY,
        "Bounded inference policy changed",
    )
    original_rows = _csv_rows(export_exceptions_csv(result))
    _check(
        original_rows[0] == BASE_HEADERS + ["Correspondence status", "Correspondence evidence"],
        "Bounded exception evidence columns changed",
    )
    _check(original_rows[1][-2] == "unique_exact", "Bounded CSV lost inferred correspondence")
    binding = bind_review_result(
        result, data_a=data_a, data_b=data_b, mapping=MAPPING, format_a="csv", format_b="csv"
    )
    _check(ReviewSession.unreviewed(binding).total == 1, "Inferred exception is not a review item")
    profile_json = export_mapping_profile(
        MAPPING,
        amount_tolerance=Decimal("0.0100"),
        reconciliation_mode=ReconciliationMode.BOUNDED_ONE_TO_MANY,
    )
    document = json.loads(profile_json)
    _check(
        document["format"] == "tallydiff-mapping-profile" and document["version"] == 5,
        "Mapping profile v5 schema identity changed",
    )
    _check(document["one_to_many_policy"] == "exact_unique_v1", "Profile policy changed")
    profile = load_mapping_profile(profile_json)
    _check(profile.mapping == MAPPING, "Profile changed the directional column mapping")
    _check(
        profile.reconciliation_mode is ReconciliationMode.BOUNDED_ONE_TO_MANY
        and profile.one_to_many_policy is EXACT_UNIQUE_ONE_TO_MANY_POLICY,
        "Profile restore changed bounded mode or policy",
    )
    _check(
        profile.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple(),
        "Profile restore lost exact tolerance digits",
    )
    _check(
        export_mapping_profile(
            profile.mapping,
            amount_tolerance=profile.amount_tolerance,
            reconciliation_mode=profile.reconciliation_mode,
            key_normalization=profile.key_normalization,
        )
        == profile_json,
        "Mapping profile v5 round-trip is not byte exact",
    )
    return {"bounded_inferred_exceptions": 1, "mapping_profile_version": 5}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True, help="Exact built wheel installed")
    parser.add_argument(
        "--checkout-root", type=Path, required=True, help="Excluded source checkout"
    )
    parser.add_argument("--expected-version", default="0.8.0")
    args = parser.parse_args()
    report = _provenance(args.wheel, args.checkout_root, args.expected_version)
    report.update(_unique_review())
    report.update(_bounded_and_profile())
    print(json.dumps({"installed_wheel_smoke": "PASS", **report}, indent=2))


if __name__ == "__main__":
    main()
