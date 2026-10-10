import json
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from tallydiff import (
    FindingCategory,
    IngestionError,
    ReconciliationMode,
    Source,
    SourceRecord,
    export_mapping_profile,
    ingest_csv,
    load_mapping_profile,
    reconcile,
)
from tallydiff.presentation import (
    EXCEPTION_CATEGORIES,
    MODE_LABELS,
    ColumnMapping,
    configuration_id,
    decode_upload,
    display_amount,
    evidence_rows,
    finding_rows,
    review_exceptions,
)


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"])
def test_utf8_decoding_preserves_source_text_with_optional_bom(prefix: bytes) -> None:
    text = "id,amount,notes\r\n001,1,caf\u00e9\r\n"
    assert decode_upload(prefix + text.encode("utf-8"), source=Source.A) == text


def test_invalid_encoding_is_a_blocking_contextual_error() -> None:
    with pytest.raises(IngestionError, match="UTF-8") as error:
        decode_upload(b"id,amount\n\xff,1", source=Source.B)
    assert error.value.source is Source.B
    assert error.value.__suppress_context__


def test_ordered_pairs_transform_to_each_files_key_order() -> None:
    mapping = ColumnMapping((("invoice", "reference"), ("vendor", "supplier")), "amount", "gross")
    assert (
        mapping.problem(["vendor", "invoice", "amount"], ["supplier", "reference", "gross"]) is None
    )
    assert mapping.keys_a == ("invoice", "vendor")
    assert mapping.keys_b == ("reference", "supplier")


@pytest.mark.parametrize(
    "mapping",
    [
        ColumnMapping((), "amount", "gross"),
        ColumnMapping(((None, "supplier"),), "amount", "gross"),
        ColumnMapping((("vendor", None),), "amount", "gross"),
        ColumnMapping((("unknown", "supplier"),), "amount", "gross"),
        ColumnMapping((("vendor", "supplier"), ("vendor", "reference")), "amount", "gross"),
        ColumnMapping((("vendor", "supplier"), ("invoice", "supplier")), "amount", "gross"),
        ColumnMapping((("vendor", "supplier"),), None, "gross"),
        ColumnMapping((("vendor", "supplier"),), "amount", None),
        ColumnMapping((("vendor", "supplier"),), "unknown", "gross"),
    ],
)
def test_incomplete_duplicate_or_stale_mappings_cannot_run(mapping: ColumnMapping) -> None:
    assert mapping.problem(["vendor", "invoice", "amount"], ["supplier", "reference", "gross"])


@pytest.mark.parametrize(
    ("value", "signed", "expected"),
    [
        ("1250", False, "1250"),
        ("45", True, "+45"),
        ("-300", True, "-300"),
        ("0.00", True, "0.00"),
        ("-0.00", True, "-0.00"),
        ("0.12345678901234567890", True, "+0.12345678901234567890"),
        ("1E+30", False, "1000000000000000000000000000000"),
    ],
)
def test_decimal_display_is_exact_even_under_low_precision(
    value: str, signed: bool, expected: str
) -> None:
    with localcontext() as context:
        context.prec = 2
        assert display_amount(Decimal(value), signed=signed) == expected


def test_finding_table_uses_exact_strings_and_includes_all_duplicate_source_numbers() -> None:
    a = ingest_csv(
        "id,amount\nINV,0.1234\nINV,0.0001\n",
        source=Source.A,
        key_columns=["id"],
        amount_column="amount",
    )
    b = ingest_csv(
        "id,amount\nINV,0.1200\n", source=Source.B, key_columns=["id"], amount_column="amount"
    )
    result = reconcile(a, b)
    assert finding_rows(result.exceptions) == [
        {
            "Category": "Duplicate / ambiguous",
            "Matching key": "INV",
            "File A amount": "0.1235",
            "File B amount": "0.1200",
            "Delta (A - B)": "+0.0035",
            "File A records": "2, 3",
            "File B records": "2",
        }
    ]


def test_evidence_preserves_fields_including_names_that_overlap_presentation_labels() -> None:
    raw = {"Source record": "003", "id": " 00123 ", "amount": " $1,200.00 ", "memo": "a\nb"}
    record = SourceRecord(Source.A, 2, ("00123",), Decimal("1200.00"), raw)
    assert evidence_rows(record) == [
        {"Field": field, "Original value": value} for field, value in raw.items()
    ]


def test_configuration_identity_changes_for_every_result_relevant_input() -> None:
    mapping = ColumnMapping((("id", "ref"), ("vendor", "supplier")), "amount", "gross")
    original = configuration_id(b"a", b"b", mapping, name_a="a.csv", name_b="b.csv")
    assert original == configuration_id(b"a", b"b", mapping, name_a="a.csv", name_b="b.csv")
    variants = [
        (b"changed", b"b", mapping, "a.csv", "b.csv"),
        (b"a", b"changed", mapping, "a.csv", "b.csv"),
        (b"a", b"b", mapping, "renamed.csv", "b.csv"),
        (b"a", b"b", mapping, "a.csv", "renamed.csv"),
        (
            b"a",
            b"b",
            replace(mapping, key_pairs=tuple(reversed(mapping.key_pairs))),
            "a.csv",
            "b.csv",
        ),
        (b"a", b"b", replace(mapping, key_pairs=(("other", "ref"),)), "a.csv", "b.csv"),
        (b"a", b"b", replace(mapping, amount_a="other"), "a.csv", "b.csv"),
        (b"a", b"b", replace(mapping, amount_b="other"), "a.csv", "b.csv"),
    ]
    for data_a, data_b, changed_mapping, name_a, name_b in variants:
        assert (
            configuration_id(data_a, data_b, changed_mapping, name_a=name_a, name_b=name_b)
            != original
        )


@pytest.mark.parametrize(
    ("amounts_a", "amounts_b", "shown_a", "shown_b", "delta"),
    [
        (("500",), (), "500", "\u2014", "+500"),
        ((), ("300",), "\u2014", "300", "-300"),
        (("0.00",), (), "0.00", "\u2014", "0.00"),
        ((), ("0.00",), "\u2014", "0.00", "0.00"),
        (("0.123400",), (), "0.123400", "\u2014", "+0.123400"),
        ((), ("0.123400",), "\u2014", "0.123400", "-0.123400"),
        (("0.00",), ("0.0001",), "0.00", "0.0001", "-0.0001"),
        (("1.00", "-1.00"), ("0.00",), "0.00", "0.00", "0.00"),
    ],
)
def test_finding_table_distinguishes_missing_records_from_present_zero_amounts(
    amounts_a: tuple[str, ...],
    amounts_b: tuple[str, ...],
    shown_a: str,
    shown_b: str,
    delta: str,
) -> None:
    records_a = [
        SourceRecord(Source.A, row, ("INV",), Decimal(amount))
        for row, amount in enumerate(amounts_a, start=2)
    ]
    records_b = [
        SourceRecord(Source.B, row, ("INV",), Decimal(amount))
        for row, amount in enumerate(amounts_b, start=2)
    ]
    result = reconcile(records_a, records_b)
    finding = result.exceptions[0]

    displayed = finding_rows(result.exceptions)[0]

    assert displayed["File A amount"] == shown_a
    assert displayed["File B amount"] == shown_b
    assert displayed["Delta (A - B)"] == delta
    assert finding.delta == result.control_difference == result.finding_delta_sum == Decimal(delta)
    if not amounts_a:
        assert finding.amount_a == Decimal("0")
    if not amounts_b:
        assert finding.amount_b == Decimal("0")


def test_configuration_identity_includes_exact_decimal_tolerance() -> None:
    mapping = ColumnMapping((("id", "ref"),), "amount", "gross")

    def identity(tolerance: Decimal) -> str:
        return configuration_id(
            b"a", b"b", mapping, name_a="a.csv", name_b="b.csv", amount_tolerance=tolerance
        )

    assert identity(Decimal("0")) == configuration_id(
        b"a", b"b", mapping, name_a="a.csv", name_b="b.csv"
    )
    assert (
        len({identity(Decimal(value)) for value in ("0", "0.01", "0.010000000000000000001")}) == 3
    )


def test_configuration_identity_includes_each_selected_worksheet() -> None:
    mapping = ColumnMapping((("id", "id"),), "amount", "amount")

    def identity(a=None, b=None):
        return configuration_id(
            b"same",
            b"same",
            mapping,
            name_a="a.xlsx",
            name_b="b.xlsx",
            worksheet_a=a,
            worksheet_b=b,
        )

    assert (
        len(
            {
                identity(),
                identity("One"),
                identity("Two"),
                identity(b="One"),
                identity("One", "One"),
                identity("One", "Two"),
            }
        )
        == 6
    )
    assert identity("One", "Two") == identity("One", "Two")


def test_configuration_identity_includes_canonical_mode_and_preserves_default() -> None:
    mapping = ColumnMapping((("id", "ref"),), "amount", "gross")
    kwargs = {"name_a": "a.csv", "name_b": "b.csv", "amount_tolerance": Decimal("0.01")}
    default = configuration_id(b"a", b"b", mapping, **kwargs)
    unique = configuration_id(
        b"a", b"b", mapping, **kwargs, reconciliation_mode=ReconciliationMode.UNIQUE
    )
    grouped = configuration_id(
        b"a", b"b", mapping, **kwargs, reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY
    )

    assert default == unique
    assert grouped != unique
    assert grouped == configuration_id(
        b"a", b"b", mapping, **kwargs, reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY
    )


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_configuration_identity_does_not_depend_on_mode_display_labels(
    monkeypatch: pytest.MonkeyPatch, mode: ReconciliationMode
) -> None:
    mapping = ColumnMapping((("id", "ref"),), "amount", "gross")
    kwargs = {"name_a": "a.csv", "name_b": "b.csv", "reconciliation_mode": mode}
    identity = configuration_id(b"a", b"b", mapping, **kwargs)

    monkeypatch.setitem(MODE_LABELS, mode, "Another display label")

    assert configuration_id(b"a", b"b", mapping, **kwargs) == identity


@pytest.mark.parametrize("mode", ["unique", "grouped_by_key", None, True, 0])
def test_configuration_identity_requires_a_mode_enum(mode: object) -> None:
    mapping = ColumnMapping((("id", "ref"),), "amount", "gross")

    with pytest.raises(TypeError, match="reconciliation_mode.*ReconciliationMode enum member"):
        configuration_id(
            b"a", b"b", mapping, name_a="a.csv", name_b="b.csv", reconciliation_mode=mode
        )


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_profile_and_manual_configuration_have_same_identity_regardless_of_json(
    mode: ReconciliationMode,
) -> None:
    mapping = ColumnMapping((("id", "ref"),), "amount", "gross")
    data = export_mapping_profile(
        mapping, amount_tolerance=Decimal("0.0100"), reconciliation_mode=mode
    )
    compact = json.dumps(json.loads(data), sort_keys=True, separators=(",", ":"))
    manual = configuration_id(
        b"a",
        b"b",
        mapping,
        name_a="a.csv",
        name_b="b.csv",
        amount_tolerance=Decimal("0.01"),
        reconciliation_mode=mode,
    )

    for serialized in (data, compact):
        profile = load_mapping_profile(serialized)
        assert (
            configuration_id(
                b"a",
                b"b",
                profile.mapping,
                name_a="a.csv",
                name_b="b.csv",
                amount_tolerance=profile.amount_tolerance,
                reconciliation_mode=profile.reconciliation_mode,
            )
            == manual
        )


@pytest.fixture
def review_findings() -> tuple:
    a = [
        ((" V001 ", "INV-1042"), "100"),
        (("V001", "INV-1043"), "10"),
        (("V002", "INV-2000"), "2"),
        (("V004", "DUP-4000"), "5"),
        (("V004", "DUP-4000"), "-5"),
        (("V005", "INV-5000"), "0.001"),
    ]
    b = [
        ((" V001 ", "INV-1042"), "99.9999"),
        (("V001", "INV-1043"), "5"),
        (("V003", "CREDIT-3000"), "2"),
        (("V004", "DUP-4000"), "0"),
        (("V005", "INV-5000"), "0"),
        (("V006", "INV-6000"), "10"),
    ]
    records = [
        [
            SourceRecord(source, row, key, Decimal(amount))
            for row, (key, amount) in enumerate(values, 1)
        ]
        for source, values in ((Source.A, a), (Source.B, b))
    ]
    return tuple(reversed(reconcile(*records).exceptions))


@pytest.mark.parametrize(
    ("query", "vendors"),
    [
        ("", [" V001 ", "V001", "V002", "V003", "V004", "V005", "V006"]),
        (" \t\n", [" V001 ", "V001", "V002", "V003", "V004", "V005", "V006"]),
        ("v001", [" V001 ", "V001"]),
        (" v001 ", [" V001 ", "V001"]),
        ("INV", [" V001 ", "V001", "V002", "V005", "V006"]),
        ("1042", [" V001 "]),
        ("104", [" V001 ", "V001"]),
        ("missing", []),
        ("INV.*", []),
        ("V001 / INV", []),
    ],
)
def test_exception_review_searches_components_without_fuzzy_or_regex_matching(
    review_findings, query, vendors
) -> None:
    visible = review_exceptions(review_findings, query=query)

    assert [finding.key[0] for finding in visible] == vendors


def test_exception_review_preserves_keys_findings_and_complete_source_evidence(
    review_findings,
) -> None:
    inputs = list(review_findings)
    snapshot = tuple(inputs)
    keys = [finding.key for finding in inputs]
    selected = review_exceptions(inputs, query="v001")

    assert tuple(inputs) == snapshot
    assert [finding.key for finding in inputs] == keys
    assert selected[0].key == (" V001 ", "INV-1042")
    assert selected[0] is inputs[-1]
    duplicate = review_exceptions(inputs, query="dup")[0]
    original = next(finding for finding in inputs if finding.key[0] == "V004")
    assert duplicate is original
    assert duplicate.rows_a is original.rows_a and duplicate.rows_b is original.rows_b
    assert len(duplicate.rows_a) == 2 and len(duplicate.rows_b) == 1


@pytest.mark.parametrize(
    ("categories", "vendors"),
    [
        (EXCEPTION_CATEGORIES, [" V001 ", "V001", "V002", "V003", "V004", "V005", "V006"]),
        ((FindingCategory.AMOUNT_MISMATCH,), [" V001 ", "V001", "V005"]),
        ((FindingCategory.DUPLICATE_AMBIGUOUS,), ["V004"]),
        ((FindingCategory.A_ONLY, FindingCategory.B_ONLY), ["V002", "V003", "V006"]),
        ((), []),
    ],
)
def test_exception_review_category_selection(review_findings, categories, vendors) -> None:
    visible = review_exceptions(review_findings, categories=categories)

    assert [finding.key[0] for finding in visible] == vendors


@pytest.mark.parametrize(
    ("minimum", "vendors"),
    [
        ("0", [" V001 ", "V001", "V002", "V003", "V004", "V005", "V006"]),
        ("0.0001", [" V001 ", "V001", "V002", "V003", "V005", "V006"]),
        ("0.00010000000000000001", ["V001", "V002", "V003", "V005", "V006"]),
        ("0.001", ["V001", "V002", "V003", "V005", "V006"]),
        ("0.00100000000000001", ["V001", "V002", "V003", "V006"]),
        ("2", ["V001", "V002", "V003", "V006"]),
        ("2.0000000000000001", ["V001", "V006"]),
        ("5", ["V001", "V006"]),
        ("5.0000000000000001", ["V006"]),
        ("11", []),
    ],
)
def test_exception_review_minimum_is_exact_inclusive_and_keeps_zero_by_default(
    review_findings, minimum, vendors
) -> None:
    visible = review_exceptions(review_findings, minimum_abs_delta=Decimal(minimum))

    assert [finding.key[0] for finding in visible] == vendors


@pytest.mark.parametrize("minimum", [0, 0.01, "0", True, None])
def test_exception_review_minimum_requires_decimal(review_findings, minimum: object) -> None:
    with pytest.raises(TypeError, match="minimum_abs_delta must be a Decimal"):
        review_exceptions(review_findings, minimum_abs_delta=minimum)


@pytest.mark.parametrize("minimum", ["-0.0001", "NaN", "sNaN", "Infinity", "-Infinity"])
def test_exception_review_rejects_negative_or_nonfinite_minimum(review_findings, minimum) -> None:
    with pytest.raises(ValueError, match="minimum_abs_delta must be finite and zero or greater"):
        review_exceptions(review_findings, minimum_abs_delta=Decimal(minimum))


@pytest.mark.parametrize(
    ("sort_order", "vendors"),
    [
        ("matching_key", [" V001 ", "V001", "V002", "V003", "V004", "V005", "V006"]),
        ("absolute_delta_desc", ["V006", "V001", "V002", "V003", "V005", " V001 ", "V004"]),
        ("absolute_delta_asc", ["V004", " V001 ", "V005", "V002", "V003", "V001", "V006"]),
    ],
)
def test_exception_review_sorting_is_deterministic_with_key_ties_and_preserves_input(
    review_findings, sort_order, vendors
) -> None:
    inputs = list(review_findings)
    snapshot = tuple(inputs)
    visible = review_exceptions(inputs, sort_order=sort_order)

    assert [finding.key[0] for finding in visible] == vendors
    assert review_exceptions(tuple(reversed(inputs)), sort_order=sort_order) == visible
    assert tuple(inputs) == snapshot


def test_exception_review_search_categories_minimum_and_sort_compose(review_findings) -> None:
    visible = review_exceptions(
        review_findings,
        query="inv",
        categories=(FindingCategory.AMOUNT_MISMATCH, FindingCategory.A_ONLY),
        minimum_abs_delta=Decimal("0.001"),
        sort_order="absolute_delta_desc",
    )

    assert len(visible) == 3
    assert [finding.key[0] for finding in visible] == ["V001", "V002", "V005"]


@pytest.mark.parametrize("sort_order", ["", "unknown"])
def test_exception_review_rejects_unknown_sort_order(review_findings, sort_order) -> None:
    with pytest.raises(ValueError, match="Unsupported exception sort order"):
        review_exceptions(review_findings, sort_order=sort_order)


@pytest.mark.parametrize("category", ["a_only", "exact_match", None, True, 1])
def test_exception_review_requires_category_enum_members(review_findings, category) -> None:
    with pytest.raises(ValueError, match="only FindingCategory enum members"):
        review_exceptions(review_findings, categories=(category,))


def test_exception_review_decimal_filter_and_sort_ignore_low_precision_and_float_limits() -> None:
    huge = Decimal("1" + "0" * 1000 + ".0001")
    a = [
        SourceRecord(Source.A, 1, ("A-large",), huge),
        SourceRecord(Source.A, 2, ("C-small",), Decimal("0.0001")),
    ]
    b = [SourceRecord(Source.B, 1, ("B-large",), huge)]
    findings = reconcile(a, b).exceptions
    with localcontext() as context:
        context.prec = 1
        context.Emax = 2
        context.Emin = -2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()

        visible = review_exceptions(
            findings, minimum_abs_delta=Decimal("0.0001"), sort_order="absolute_delta_desc"
        )

        assert [finding.key for finding in visible] == [("A-large",), ("B-large",), ("C-small",)]
        assert (
            review_exceptions(findings, minimum_abs_delta=Decimal("0.00010000000000000001"))
            == findings[:2]
        )
        assert not any(context.flags.values())


def test_exception_review_handles_ten_thousand_groups() -> None:
    records = [
        SourceRecord(Source.A, index + 1, (f"INV-{index:05d}",), Decimal(index % 10))
        for index in range(10_000)
    ]
    findings = reconcile(records, []).exceptions

    visible = review_exceptions(
        tuple(reversed(findings)),
        query="999",
        minimum_abs_delta=Decimal("9"),
        sort_order="absolute_delta_desc",
    )

    assert len(visible) == 10
    assert [finding.key for finding in visible] == [
        (f"INV-{index:05d}",) for index in range(999, 10_000, 1000)
    ]
