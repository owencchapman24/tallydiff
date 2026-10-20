"""Deterministic synthetic signal calibration, separate from runtime decisions.

Intended labels belong only to this developer corpus. Each signal is examined
independently, with exact ratios and all ties retained; no combined ranking is
defined. Run ``python -m scripts.discovery_calibration`` for JSON on stdout.
"""

import csv
import json
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal
from fractions import Fraction
from io import BytesIO, StringIO
from itertools import combinations, permutations
from xml.sax.saxutils import escape
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from tallydiff.discovery import (
    compare_amount_columns,
    compare_headers,
    compare_key_mapping,
    discover_csv,
    discover_xlsx,
)
from tallydiff.models import Source


@dataclass(frozen=True, slots=True)
class CalibrationCase:
    name: str
    categories: tuple[str, ...]
    format_a: str
    format_b: str
    data_a: str | bytes
    data_b: str | bytes
    worksheet_a: str | None
    worksheet_b: str | None
    intended_key_mappings: tuple[tuple[tuple[str, str], ...], ...]
    intended_amount_pairs: tuple[tuple[str, str], ...]
    composite_columns_a: tuple[str, ...] = ()
    composite_columns_b: tuple[str, ...] = ()


def _csv_data(header, rows) -> str:
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def _xlsx_data(header, rows) -> bytes:
    """Make a minimal standard workbook with fixed XML and archive metadata.

    Inline strings avoid shared-string indirection. Numeric cells remain typed
    numeric cells, and booleans/formulas/errors keep their XLSX candidate types.
    No timestamps or creator properties are generated.
    """

    xml_rows = []
    for row_index, values in enumerate((header, *rows), start=1):
        cells = []
        for column_index, value in enumerate(values, start=1):
            if value is None:
                continue
            coordinate = f"{chr(64 + column_index)}{row_index}"
            if isinstance(value, bool):
                cell = f'<c r="{coordinate}" t="b"><v>{int(value)}</v></c>'
            elif isinstance(value, int | float | Decimal):
                cell = f'<c r="{coordinate}" t="n"><v>{value}</v></c>'
            elif value.startswith("="):
                cell = f'<c r="{coordinate}"><f>{escape(value[1:])}</f></c>'
            elif value in ("#N/A", "#DIV/0!"):
                cell = f'<c r="{coordinate}" t="e"><v>{escape(value)}</v></c>'
            else:
                cell = (
                    f'<c r="{coordinate}" t="inlineStr"><is><t xml:space="preserve">'
                    f"{escape(value)}</t></is></c>"
                )
            cells.append(cell)
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    files = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>"
        ),
    }
    with BytesIO() as stream:
        with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
            for name, text in files.items():
                member = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                member.compress_type = ZIP_STORED
                member.create_system = 0
                archive.writestr(member, text.encode("utf-8"))
        return stream.getvalue()


def _case(
    name,
    header_a,
    rows_a,
    header_b,
    rows_b,
    key_mapping,
    amount_pair,
    *,
    formats=("csv", "csv"),
    composite_columns=((), ()),
):
    builders = {"csv": _csv_data, "xlsx": _xlsx_data}
    return CalibrationCase(
        name=name,
        categories=(name,),
        format_a=formats[0],
        format_b=formats[1],
        data_a=builders[formats[0]](header_a, rows_a),
        data_b=builders[formats[1]](header_b, rows_b),
        worksheet_a="Data" if formats[0] == "xlsx" else None,
        worksheet_b="Data" if formats[1] == "xlsx" else None,
        intended_key_mappings=(tuple(key_mapping),),
        intended_amount_pairs=(amount_pair,),
        composite_columns_a=composite_columns[0],
        composite_columns_b=composite_columns[1],
    )


def build_corpus() -> tuple[CalibrationCase, ...]:
    """Return fourteen small cases with non-first intended fields and decoys."""

    return (
        _case(
            "canonical",
            ("Notes", "Vendor ID", "Invoice Number", "Invoice Amount", "Posting Date"),
            [
                ["note A", "V1", "100", "1250", "2026-01-01"],
                ["note A", "V1", "101", "800", "2026-01-01"],
                ["note A", "V2", "200", "500", "2026-01-02"],
                ["note A", "V3", "300", "125", "2026-01-02"],
            ],
            ("Memo", "Supplier", "Invoice Ref", "Gross Amount", "Document Date"),
            [
                ["note B", "V1", "100", "1205", "2026-02-01"],
                ["note B", "V1", "101", "800", "2026-02-01"],
                ["note B", "V2", "200", "500", "2026-02-02"],
                ["note B", "V4", "400", "75", "2026-02-02"],
            ],
            (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")),
            ("Invoice Amount", "Gross Amount"),
            composite_columns=(
                ("Notes", "Vendor ID", "Invoice Number"),
                ("Memo", "Supplier", "Invoice Ref"),
            ),
        ),
        _case(
            "identical_headers",
            ("Notes", "Transaction ID", "Amount", "Date"),
            [["left", "T1", "10", "2026-01-01"], ["left", "T2", "20", "2026-01-01"]],
            ("Notes", "Transaction ID", "Amount", "Date"),
            [["right", "T1", "11", "2026-02-01"], ["right", "T2", "20", "2026-02-01"]],
            (("Transaction ID", "Transaction ID"),),
            ("Amount", "Amount"),
        ),
        _case(
            "unrelated_key_headers",
            ("Notes", "Locator", "Debit", "Serial"),
            [["left", "Q1", "10", "A1"], ["left", "Q2", "20", "A2"]],
            ("Memo", "Receipt", "Credit", "Batch"),
            [["right", "Q2", "21", "B1"], ["right", "Q1", "10", "B2"]],
            (("Locator", "Receipt"),),
            ("Debit", "Credit"),
        ),
        _case(
            "lexical_decoy",
            ("Notes", "Invoice Number", "Locator", "Charge"),
            [["left", "X1", "K1", "10"], ["left", "X2", "K2", "20"]],
            ("Memo", "Invoice Number", "Receipt", "Posted"),
            [["right", "Y1", "K1", "11"], ["right", "Y2", "K2", "21"]],
            (("Locator", "Receipt"),),
            ("Charge", "Posted"),
        ),
        _case(
            "numeric_identifier_decoy",
            ("Notes", "Identifier", "Invoice Amount", "Posting Date"),
            [
                ["left", "1001", "10", "2026-01-01"],
                ["left", "1002", "20", "2026-01-01"],
                ["left", "1003", "30", "2026-01-02"],
            ],
            ("Memo", "Reference", "Gross Amount", "Document Date"),
            [
                ["right", "1001", "10", "2026-02-01"],
                ["right", "1002", "25", "2026-02-01"],
                ["right", "1003", "30", "2026-02-02"],
            ],
            (("Identifier", "Reference"),),
            ("Invoice Amount", "Gross Amount"),
        ),
        _case(
            "monetary_decoy",
            ("Notes", "Record Key", "Invoice Amount", "Tax Amount"),
            [["left", "K1", "100", "5"], ["left", "K2", "200", "10"]],
            ("Memo", "Reference", "Gross Amount", "Tax Amount"),
            [["right", "K1", "99", "5"], ["right", "K2", "200", "10"]],
            (("Record Key", "Reference"),),
            ("Invoice Amount", "Gross Amount"),
        ),
        _case(
            "control_total_trap",
            ("Notes", "Record Key", "Invoice Amount", "Batch Number"),
            [["left", "K1", "50", "10"], ["left", "K2", "60", "20"], ["left", "K3", "70", "30"]],
            ("Memo", "Reference", "Gross Amount", "Sequence Number"),
            [["right", "K1", "40", "20"], ["right", "K2", "50", "10"], ["right", "K3", "60", "30"]],
            (("Record Key", "Reference"),),
            ("Invoice Amount", "Gross Amount"),
        ),
        _case(
            "partial_overlap",
            ("Notes", "Record Key", "Amount", "Status"),
            [
                ["left", "K1", "10", "open"],
                ["left", "K2", "20", "open"],
                ["left", "K3", "30", "closed"],
            ],
            ("Memo", "Reference", "Gross", "State"),
            [
                ["right", "K2", "20", "paid"],
                ["right", "K3", "31", "paid"],
                ["right", "K4", "40", "new"],
            ],
            (("Record Key", "Reference"),),
            ("Amount", "Gross"),
        ),
        _case(
            "composite_duplicates",
            ("Notes", "Vendor", "Invoice", "Amount"),
            [["left", "V1", "100", "10"], ["left", "V1", "101", "20"], ["left", "V2", "100", "30"]],
            ("Memo", "Supplier", "Ref", "Gross"),
            [
                ["right", "V1", "100", "11"],
                ["right", "V1", "101", "20"],
                ["right", "V2", "100", "30"],
            ],
            (("Vendor", "Supplier"), ("Invoice", "Ref")),
            ("Amount", "Gross"),
            composite_columns=(("Notes", "Vendor", "Invoice"), ("Memo", "Supplier", "Ref")),
        ),
        _case(
            "invalid_candidates",
            ("Notes", "Identifier", "Invoice Amount", "Optional Key"),
            [["left", "K1", "10", "A"], ["left", "", "word", "A"], ["left", "K3", "30", ""]],
            ("Memo", "Reference", "Gross Amount", "Optional Ref"),
            [["right", "K1", 10, True], ["right", "K2", None, "=1+1"], ["right", None, 30, "#N/A"]],
            (("Identifier", "Reference"),),
            ("Invoice Amount", "Gross Amount"),
            formats=("csv", "xlsx"),
        ),
        _case(
            "reordered_columns",
            ("Posting Date", "Notes", "Invoice Amount", "Serial", "Transaction ID"),
            [["2026-01-01", "left", "10", "A1", "T1"], ["2026-01-01", "left", "20", "A2", "T2"]],
            ("Memo", "Transaction ID", "Batch", "Gross Amount", "Document Date"),
            [["right", "T2", "B2", "21", "2026-02-01"], ["right", "T1", "B1", "10", "2026-02-01"]],
            (("Transaction ID", "Transaction ID"),),
            ("Invoice Amount", "Gross Amount"),
        ),
        _case(
            "cross_format",
            ("Notes", "Identifier", "Invoice Amount", "Optional"),
            [
                ["left", "1 ", "10.10", "A"],
                ["left", "001", "20.20", "B"],
                ["left", "2", "30.30", "C"],
            ],
            ("Memo", "Reference", "Gross Amount", "Optional"),
            [["right", 1, 10.1, True], ["right", "1", 20.2, "=1+1"], ["right", 2, 30.3, "#N/A"]],
            (("Identifier", "Reference"),),
            ("Invoice Amount", "Gross Amount"),
            formats=("csv", "xlsx"),
        ),
        _case(
            "empty_tables",
            ("Notes", "Identifier", "Amount"),
            [],
            ("Memo", "Reference", "Gross"),
            [],
            (("Identifier", "Reference"),),
            ("Amount", "Gross"),
        ),
        _case(
            "punctuation_only_headers",
            ("$", "Identifier", "Amount"),
            [["left", "K1", "10"]],
            ("€", "Reference", "Gross"),
            [["right", "K1", "11"]],
            (("Identifier", "Reference"),),
            ("Amount", "Gross"),
        ),
    )


def _json_value(value):
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    return value


def _ratio(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator}


def _ratio_value(ratio):
    return Fraction(ratio["numerator"], ratio["denominator"]) if ratio["denominator"] else None


def _leaders(candidates, indices, value_for, *, minimum=False):
    observations = [(index, value_for(candidates[index])) for index in indices]
    observations = [(index, value) for index, value in observations if value is not None]
    value = (min if minimum else max)(item[1] for item in observations) if observations else None
    tied = [index for index, observed in observations if observed == value]
    return {
        "value": _json_value(value),
        "candidate_indices": tied,
        "intended_indices": [index for index in tied if candidates[index]["is_intended"]],
        "intended_component_indices": [
            index for index in tied if candidates[index].get("is_intended_component", False)
        ],
    }


def _signal_leaders(keys, amounts):
    result = {}
    for cardinality in (1, 2):
        indices = [
            index for index, candidate in enumerate(keys) if candidate["cardinality"] == cardinality
        ]
        signals = {}
        for name in (
            "eligible_coverage_a",
            "eligible_coverage_b",
            "distinct_overlap_a",
            "distinct_overlap_b",
            "row_overlap_a",
            "row_overlap_b",
            "uniqueness_a",
            "uniqueness_b",
        ):
            signals[name] = _leaders(
                keys, indices, lambda item, name=name: _ratio_value(item["ratios"][name])
            )
        for name in (
            "overlap_distinct_count",
            "overlap_row_count_a",
            "overlap_row_count_b",
            "duplicate_excess_count_a",
            "duplicate_excess_count_b",
        ):
            signals[name] = _leaders(
                keys,
                indices,
                lambda item, name=name: item["facts"][name],
                minimum=name.startswith("duplicate"),
            )
        if cardinality == 1:
            signals["exact_normalized_match"] = _leaders(
                keys,
                indices,
                lambda item: True if item["headers"][0]["exact_normalized_match"] else None,
            )
            signals["shared_token_count"] = _leaders(
                keys, indices, lambda item: len(item["headers"][0]["shared_tokens"])
            )
        result[f"key_cardinality_{cardinality}"] = signals
    indices = list(range(len(amounts)))
    result["amount"] = {
        name: _leaders(amounts, indices, lambda item, name=name: _ratio_value(item["ratios"][name]))
        for name in ("parseability_a", "parseability_b")
    }
    result["amount"]["exact_normalized_match"] = _leaders(
        amounts, indices, lambda item: True if item["header"]["exact_normalized_match"] else None
    )
    result["amount"]["shared_token_count"] = _leaders(
        amounts, indices, lambda item: len(item["header"]["shared_tokens"])
    )
    result["amount"]["absolute_control_difference"] = _leaders(
        amounts,
        indices,
        lambda item: (
            Decimal(item["facts"]["control_difference"]).copy_abs()
            if item["facts"]["control_difference"] is not None
            else None
        ),
        minimum=True,
    )
    return result


def evaluate_case(case: CalibrationCase) -> dict:
    """Enumerate all singles and bounded, explicitly scoped two-component keys."""

    def snapshot(data, file_format, worksheet, source):
        return (
            discover_csv(data, source=source)
            if file_format == "csv"
            else discover_xlsx(data, source=source, worksheet=worksheet)
        )

    snapshot_a = snapshot(case.data_a, case.format_a, case.worksheet_a, Source.A)
    snapshot_b = snapshot(case.data_b, case.format_b, case.worksheet_b, Source.B)
    headers_a = tuple(column.name for column in snapshot_a.profile.columns)
    headers_b = tuple(column.name for column in snapshot_b.profile.columns)
    singles = [((name_a, name_b),) for name_a in headers_a for name_b in headers_b]
    mappings = list(singles)
    scoped_a = tuple(name for name in headers_a if name in case.composite_columns_a)
    scoped_b = tuple(name for name in headers_b if name in case.composite_columns_b)
    if len(scoped_a) > 3 or len(scoped_b) > 3:
        raise ValueError("calibration composite scope is bounded to three columns per side")
    mappings.extend(
        tuple(zip(left, right, strict=True))
        for left in combinations(scoped_a, 2)
        for right in permutations(scoped_b, 2)
    )
    intended_components = {pair for mapping in case.intended_key_mappings for pair in mapping}
    keys = []
    amounts = []
    for mapping in mappings:
        facts = compare_key_mapping(snapshot_a, snapshot_b, key_pairs=mapping)
        keys.append(
            {
                "key_pairs": _json_value(mapping),
                "cardinality": len(mapping),
                "is_intended": mapping in case.intended_key_mappings,
                "is_intended_component": len(mapping) == 1 and mapping[0] in intended_components,
                "headers": [_json_value(compare_headers(*pair)) for pair in mapping],
                "facts": _json_value(facts),
                "ratios": {
                    "eligible_coverage_a": _ratio(facts.eligible_row_count_a, facts.row_count_a),
                    "eligible_coverage_b": _ratio(facts.eligible_row_count_b, facts.row_count_b),
                    "distinct_overlap_a": _ratio(
                        facts.overlap_distinct_count, facts.distinct_key_count_a
                    ),
                    "distinct_overlap_b": _ratio(
                        facts.overlap_distinct_count, facts.distinct_key_count_b
                    ),
                    "row_overlap_a": _ratio(facts.overlap_row_count_a, facts.row_count_a),
                    "row_overlap_b": _ratio(facts.overlap_row_count_b, facts.row_count_b),
                    "uniqueness_a": _ratio(facts.distinct_key_count_a, facts.eligible_row_count_a),
                    "uniqueness_b": _ratio(facts.distinct_key_count_b, facts.eligible_row_count_b),
                },
            }
        )
    for (pair,) in singles:
        facts = compare_amount_columns(snapshot_a, snapshot_b, column_a=pair[0], column_b=pair[1])
        amounts.append(
            {
                "column_a": pair[0],
                "column_b": pair[1],
                "is_intended": pair in case.intended_amount_pairs,
                "header": _json_value(compare_headers(*pair)),
                "facts": _json_value(facts),
                "ratios": {
                    "parseability_a": _ratio(facts.parseable_count_a, facts.row_count_a),
                    "parseability_b": _ratio(facts.parseable_count_b, facts.row_count_b),
                },
            }
        )
    leaders = _signal_leaders(keys, amounts)
    singles_leader = leaders["key_cardinality_1"]["overlap_distinct_count"]
    header_leader = leaders["key_cardinality_1"]["shared_token_count"]
    fully_parseable = [
        index
        for index, candidate in enumerate(amounts)
        if all(
            candidate["ratios"][f"parseability_{side}"]
            == _ratio(
                candidate["facts"][f"row_count_{side}"], candidate["facts"][f"row_count_{side}"]
            )
            and candidate["facts"][f"row_count_{side}"] > 0
            for side in ("a", "b")
        )
    ]
    numeric_id_ties = [
        index
        for index in fully_parseable
        if (amounts[index]["column_a"], amounts[index]["column_b"]) in intended_components
        and not amounts[index]["is_intended"]
    ]
    composite_observations = []
    for index, candidate in enumerate(keys):
        if candidate["is_intended"] and candidate["cardinality"] == 2:
            first_pair = candidate["key_pairs"][0]
            first_index = next(
                i for i, single in enumerate(keys) if single["key_pairs"] == [first_pair]
            )
            composite_observations.append(
                {
                    "candidate_index": index,
                    "first_component_index": first_index,
                    "duplicate_excess_reduced_a": candidate["facts"]["duplicate_excess_count_a"]
                    < keys[first_index]["facts"]["duplicate_excess_count_a"],
                    "duplicate_excess_reduced_b": candidate["facts"]["duplicate_excess_count_b"]
                    < keys[first_index]["facts"]["duplicate_excess_count_b"],
                    "overlap_rows_preserved_a": candidate["facts"]["overlap_row_count_a"]
                    == keys[first_index]["facts"]["overlap_row_count_a"],
                    "overlap_rows_preserved_b": candidate["facts"]["overlap_row_count_b"]
                    == keys[first_index]["facts"]["overlap_row_count_b"],
                }
            )
    return {
        "name": case.name,
        "categories": list(case.categories),
        "formats": {"a": case.format_a, "b": case.format_b},
        "headers_a": list(headers_a),
        "headers_b": list(headers_b),
        "intended_key_mappings": _json_value(case.intended_key_mappings),
        "intended_amount_pairs": _json_value(case.intended_amount_pairs),
        "key_candidates": keys,
        "amount_candidates": amounts,
        "signal_leaders": leaders,
        "analysis": {
            "distinct_overlap_signal_present": singles_leader["value"] > 0,
            "intended_component_in_distinct_overlap_leaders": singles_leader["value"] > 0
            and bool(singles_leader["intended_component_indices"]),
            "intended_component_is_unique_distinct_overlap_leader": singles_leader["value"] > 0
            and len(singles_leader["candidate_indices"]) == 1
            and bool(singles_leader["intended_component_indices"]),
            "decoy_in_distinct_overlap_leaders": singles_leader["value"] > 0
            and any(
                not keys[index]["is_intended_component"]
                for index in singles_leader["candidate_indices"]
            ),
            "shared_token_signal_present": header_leader["value"] > 0,
            "intended_component_in_shared_token_leaders": header_leader["value"] > 0
            and bool(header_leader["intended_component_indices"]),
            "decoy_in_shared_token_leaders": header_leader["value"] > 0
            and any(
                not keys[index]["is_intended_component"]
                for index in header_leader["candidate_indices"]
            ),
            "exact_header_match_with_empty_normalization": any(
                item["headers"][0]["exact_normalized_match"]
                and not item["headers"][0]["normalized_a"]
                for item in keys
                if item["cardinality"] == 1
            ),
            "full_parseability_decoy_indices": [
                index for index in fully_parseable if not amounts[index]["is_intended"]
            ],
            "numeric_identifier_full_parseability_indices": numeric_id_ties,
            "decoy_in_control_proximity_leaders": any(
                not amounts[index]["is_intended"]
                for index in leaders["amount"]["absolute_control_difference"]["candidate_indices"]
            ),
            "composite_observations": composite_observations,
        },
    }


def build_report() -> dict:
    cases = [evaluate_case(case) for case in build_corpus()]
    summary = {
        "case_count": len(cases),
        "single_key_candidate_count": sum(
            sum(item["cardinality"] == 1 for item in case["key_candidates"]) for case in cases
        ),
        "composite_key_candidate_count": sum(
            sum(item["cardinality"] == 2 for item in case["key_candidates"]) for case in cases
        ),
        "amount_candidate_count": sum(len(case["amount_candidates"]) for case in cases),
        "cases_intended_component_in_distinct_overlap_leaders": sum(
            case["analysis"]["intended_component_in_distinct_overlap_leaders"] for case in cases
        ),
        "cases_intended_component_unique_distinct_overlap_leader": sum(
            case["analysis"]["intended_component_is_unique_distinct_overlap_leader"]
            for case in cases
        ),
        "cases_decoy_in_distinct_overlap_leaders": sum(
            case["analysis"]["decoy_in_distinct_overlap_leaders"] for case in cases
        ),
        "cases_shared_header_tokens_only_intended_components": sum(
            case["analysis"]["shared_token_signal_present"]
            and case["analysis"]["intended_component_in_shared_token_leaders"]
            and not case["analysis"]["decoy_in_shared_token_leaders"]
            for case in cases
        ),
        "cases_shared_header_tokens_include_decoy_leaders": sum(
            case["analysis"]["shared_token_signal_present"]
            and case["analysis"]["decoy_in_shared_token_leaders"]
            for case in cases
        ),
        "cases_exact_header_match_with_empty_normalization": sum(
            case["analysis"]["exact_header_match_with_empty_normalization"] for case in cases
        ),
        "cases_with_full_parseability_amount_decoys": sum(
            bool(case["analysis"]["full_parseability_decoy_indices"]) for case in cases
        ),
        "cases_with_fully_parseable_numeric_identifier_pairs": sum(
            bool(case["analysis"]["numeric_identifier_full_parseability_indices"]) for case in cases
        ),
        "cases_control_proximity_leaders_include_decoys": sum(
            case["analysis"]["decoy_in_control_proximity_leaders"] for case in cases
        ),
        "composite_mappings_reducing_duplicates_with_row_overlap_preserved": sum(
            all(
                item[name]
                for name in (
                    "duplicate_excess_reduced_a",
                    "duplicate_excess_reduced_b",
                    "overlap_rows_preserved_a",
                    "overlap_rows_preserved_b",
                )
            )
            for case in cases
            for item in case["analysis"]["composite_observations"]
        ),
    }
    return {"schema_version": 1, "case_count": len(cases), "cases": cases, "summary": summary}


def report_json(report: dict | None = None) -> str:
    """Serialize portable ASCII JSON, preserving Unicode through JSON escapes."""

    return json.dumps(
        build_report() if report is None else report,
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def main() -> None:
    print(report_json())


if __name__ == "__main__":
    main()
