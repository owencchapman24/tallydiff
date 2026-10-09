"""Deterministic synthetic financial exports and independent integer-based ground truth.

Developer fixtures only. This module deliberately does not import TallyDiff.
The legacy workload is retained; the grouped workload adds 30 multi-row keys
per hundred groups, with split detail, credits, offsets, and zero-net groups.
The separate normalization workload reuses those amounts with side-specific raw
composite keys and canonical matching keys known directly from construction.
The comparison workload has its own asymmetric schema, primitive exact-evidence
truth, and source identities constructed before CSV serialization.
"""

import argparse
import csv
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from random import Random

HEADERS_A = (
    "Vendor ID",
    "Invoice Number",
    "Posting Date",
    "Description",
    "Department",
    "Invoice Amount",
)
HEADERS_B = ("Supplier", "Invoice Ref", "Date", "Memo", "Cost Center", "Gross Amount")
KEYS_A = HEADERS_A[:2]
KEYS_B = HEADERS_B[:2]
AMOUNT_A = HEADERS_A[-1]
AMOUNT_B = HEADERS_B[-1]
MODES = ("unique", "grouped_by_key")
WORKLOADS = ("legacy", "grouped", "normalization", "comparison")
NORMALIZATION_RULES = (
    {
        "casefold": True,
        "collapse_whitespace": True,
        "remove_punctuation": True,
        "strip_leading_zeros": False,
    },
    {
        "casefold": False,
        "collapse_whitespace": False,
        "remove_punctuation": False,
        "strip_leading_zeros": True,
    },
)

CATEGORIES = (
    "exact_match",
    "within_tolerance",
    "amount_mismatch",
    "a_only",
    "b_only",
    "duplicate_ambiguous",
)


def decimal_units(units: int) -> Decimal:
    """Construct exact amounts from integer ten-thousandths, without arithmetic context."""

    whole, fraction = divmod(abs(units), 10_000)
    return Decimal(f"{'-' if units < 0 else ''}{whole}.{fraction:04d}")


@dataclass(frozen=True, slots=True)
class Scenario:
    key: tuple[str, str]
    kind: str
    amounts_a: tuple[int, ...]
    amounts_b: tuple[int, ...]
    raw_key_a: tuple[str, str] | None = None
    raw_key_b: tuple[str, str] | None = None
    key_variant: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectedGroup:
    key: tuple[str, str]
    category: str
    rows_a: int
    rows_b: int
    units_a: int
    units_b: int
    raw_key_a: tuple[str, str] | None = None
    raw_key_b: tuple[str, str] | None = None

    @property
    def is_exception(self) -> bool:
        return self.category not in ("exact_match", "within_tolerance")

    @property
    def delta_units(self) -> int:
        return self.units_a - self.units_b

    @property
    def delta(self) -> Decimal:
        return decimal_units(self.units_a - self.units_b)


@dataclass(frozen=True, slots=True)
class GroundTruth:
    groups: tuple[ExpectedGroup, ...]
    record_count_a: int
    record_count_b: int
    category_counts: dict[str, int]
    total_a: Decimal
    total_b: Decimal
    control_difference: Decimal
    tolerated_delta_total: Decimal
    mode: str

    @property
    def exception_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(group.key for group in self.groups if group.is_exception)

    def summary(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "records_a": self.record_count_a,
            "records_b": self.record_count_b,
            "key_groups": len(self.groups),
            "categories": self.category_counts,
            "total_a": str(self.total_a),
            "total_b": str(self.total_b),
            "control_difference": str(self.control_difference),
            "tolerated_delta_total": str(self.tolerated_delta_total),
            "exceptions": len(self.exception_keys),
            "tolerated": self.category_counts["within_tolerance"],
        }


@dataclass(frozen=True, slots=True)
class SyntheticPair:
    csv_a: str
    csv_b: str
    scenarios: tuple[Scenario, ...]

    def expected(
        self, amount_tolerance: Decimal = Decimal("0"), *, mode: str = "unique"
    ) -> GroundTruth:
        """Use construction plans and integer sums, never parsed/reconciled product output."""

        if not isinstance(amount_tolerance, Decimal):
            raise TypeError("amount_tolerance must be a Decimal")
        if not amount_tolerance.is_finite() or amount_tolerance < 0:
            raise ValueError("amount_tolerance must be finite and nonnegative")
        if mode not in MODES:
            raise ValueError("mode must be unique or grouped_by_key")
        groups = []
        count_a = count_b = total_a = total_b = tolerated = 0
        counts = Counter(dict.fromkeys(CATEGORIES, 0))
        for scenario in sorted(self.scenarios, key=lambda scenario: scenario.key):
            units_a = sum(scenario.amounts_a)
            units_b = sum(scenario.amounts_b)
            delta = units_a - units_b
            if mode == "unique" and (len(scenario.amounts_a) > 1 or len(scenario.amounts_b) > 1):
                category = "duplicate_ambiguous"
            elif not scenario.amounts_b:
                category = "a_only"
            elif not scenario.amounts_a:
                category = "b_only"
            elif delta == 0:
                category = "exact_match"
            elif decimal_units(delta).copy_abs() <= amount_tolerance:
                category = "within_tolerance"
                tolerated += delta
            else:
                category = "amount_mismatch"
            counts[category] += 1
            count_a += len(scenario.amounts_a)
            count_b += len(scenario.amounts_b)
            total_a += units_a
            total_b += units_b
            groups.append(
                ExpectedGroup(
                    scenario.key,
                    category,
                    len(scenario.amounts_a),
                    len(scenario.amounts_b),
                    units_a,
                    units_b,
                    scenario.raw_key_a,
                    scenario.raw_key_b,
                )
            )
        return GroundTruth(
            tuple(groups),
            count_a,
            count_b,
            dict(counts),
            decimal_units(total_a),
            decimal_units(total_b),
            decimal_units(total_a - total_b),
            decimal_units(tolerated),
            mode,
        )


def _scenario(index: int, rng: Random) -> Scenario:
    # Invoice numbers recur across vendors, so BOTH ordered key columns are needed.
    key = (f"{index % 137:06d}", f"{index // 137:09d}")
    base = rng.randint(2_500, 25_000_000) * 100
    if index % 17 == 0:
        base = 0
    elif index % 11 == 0:
        base = -base
    if index % 9 == 0 and base:
        base += 17  # A genuine sub-cent value, not a rounded cents fixture.
    slot = index % 100
    if slot < 70:
        return Scenario(key, "exact", (base,), (base,))
    if slot < 80:
        delta = (100, -100, 50, -50, 1, -1, 99, -99, 100, -50)[slot - 70]
        return Scenario(key, "variance", (base,), (base - delta,))
    if slot < 88:
        delta = (200, -200, 1001, -2500, 5000, -5000, 301, -301)[slot - 80]
        return Scenario(key, "variance", (base,), (base - delta,))
    if slot < 92:
        amount = 0 if slot == 88 else (-abs(base) if slot == 90 else base)
        return Scenario(key, "a_only", (amount,), ())
    if slot < 96:
        amount = 0 if slot == 92 else (-abs(base) if slot == 94 else base)
        return Scenario(key, "b_only", (), (amount,))
    if slot == 96:
        return Scenario(key, "duplicate", (base, 10_000), (base + 10_000,))
    if slot == 97:
        return Scenario(key, "duplicate", (base,), (base, 50))
    if slot == 98:
        positive = abs(base) or 1_000_000
        return Scenario(key, "duplicate", (positive, -positive), (positive // 2, -positive // 2))
    return Scenario(
        key,
        "duplicate",
        (0, 0) if index // 100 % 2 == 0 else (),
        () if index // 100 % 2 == 0 else (0, 0),
    )


def _detail(total: int, rng: Random) -> tuple[int, ...]:
    """Split a known total into debits, a credit, zero, and optional offsets."""

    debit = rng.randint(1, 5_000_000)
    credit = rng.randint(1, 1_000_000)
    parts = (total - debit + credit, debit, -credit, 0)
    if rng.randrange(2):
        offset = rng.randint(1, 100_000)
        parts += (offset, -offset)
    return parts


def _grouped_scenario(index: int, rng: Random) -> Scenario:
    key = (f"{index % 137:06d}", f"{index // 137:09d}")
    base = rng.randint(2_500, 25_000_000) * 100
    if index % 17 == 0:
        base = 0
    elif index % 11 == 0:
        base = -base
    if index % 9 == 0 and base:
        base += 17
    slot = index % 100
    if slot < 40:
        return Scenario(key, "one_exact", (base,), (base,))
    if slot < 50:
        delta = (100, -100, 50, -50, 1, -1, 99, -99, 100, -50)[slot - 40]
        return Scenario(key, "one_small_variance", (base,), (base - delta,))
    if slot < 60:
        delta = (200, -200, 1001, -2500, 5000, -5000, 301, -301, 101, -101)[slot - 50]
        return Scenario(key, "one_mismatch", (base,), (base - delta,))
    if slot < 65:
        return Scenario(key, "a_only", (0 if slot == 60 else base,), ())
    if slot < 70:
        return Scenario(key, "b_only", (), (0 if slot == 65 else base,))
    if slot < 75:
        return Scenario(key, "one_many_exact", (base,), _detail(base, rng))
    if slot < 80:
        return Scenario(key, "many_one_exact", _detail(base, rng), (base,))
    if slot < 85:
        return Scenario(key, "many_many_exact", _detail(base, rng), _detail(base, rng))
    if slot < 90:
        delta = (100, -100, 1, -1, 50)[slot - 85]
        return Scenario(
            key, "grouped_small_variance", _detail(base, rng), _detail(base - delta, rng)
        )
    if slot < 95:
        delta = (101, -101, 5000, -5000, 2501)[slot - 90]
        return Scenario(key, "grouped_mismatch", _detail(base, rng), _detail(base - delta, rng))
    positive = abs(base) or 1_000_000
    if slot == 95:
        return Scenario(key, "a_only_zero_net", (positive, -positive, 0), ())
    if slot == 96:
        return Scenario(key, "b_only_zero_net", (), (positive, 0, -positive))
    if slot == 97:
        return Scenario(key, "offsetting_exact", (positive, -positive, 0), (1, -1, 0))
    if slot == 98:
        return Scenario(key, "zero_lines_exact", (0, 0), (0, 0, 0))
    huge = 10**20 + 17
    key = (key[0], f"ADJ-{index:09d}")
    return Scenario(key, "large_subcent_mismatch", (huge, -10_000, 0), (huge - 15_001, 2500, 0))


def _normalization_scenario(index: int, rng: Random) -> Scenario:
    """Use grouped amounts unchanged and define canonical/raw keys directly.

    The vendor number and invoice quotient uniquely identify the index. Each
    source uses exactly one raw full key per plan, including all its detail rows.
    No transformation implementation is used to infer the intended canonical key.
    """

    amounts = _grouped_scenario(index, rng)
    vendor = f"{index % 137:06d}"
    invoice = str(index // 137)
    canonical_vendor = f"vendor{vendor} west"
    variant = index % 12
    variants = (
        ("case_only", (f"Vendor{vendor} West", invoice), (f"VENDOR{vendor} WEST", invoice)),
        (
            "repeated_spaces",
            (f"vendor{vendor}   west", invoice),
            (canonical_vendor, invoice),
        ),
        ("tabs", (f"vendor{vendor}\twest", invoice), (f"vendor{vendor}\t\twest", invoice)),
        (
            "unicode_spaces",
            (f"vendor{vendor}\u00a0west", invoice),
            (f"vendor{vendor}\u2003west", invoice),
        ),
        ("ascii_punctuation", (f"vendor-{vendor} west", invoice), (canonical_vendor, invoice)),
        (
            "unicode_punctuation",
            (f"vendor—{vendor} west", invoice),
            (f"vendor“{vendor}” west", invoice),
        ),
        ("zeros_only", (canonical_vendor, invoice.zfill(9)), (canonical_vendor, invoice)),
        (
            "combined",
            (f"Vendor-{vendor}\tWest", invoice.zfill(9)),
            (f"VENDOR{vendor}   WEST", invoice),
        ),
        (
            "unicode_casefold",
            (f"Straße{vendor} west", invoice),
            (f"STRASSE{vendor} west", invoice),
        ),
        ("canonical", (canonical_vendor, invoice), (canonical_vendor, invoice)),
        (
            "exact_vendor_padded_invoice",
            (canonical_vendor, invoice.zfill(9)),
            (canonical_vendor, invoice.zfill(12)),
        ),
        (
            "unicode_combined",
            (f"VENDOR–{vendor}\u202fWEST", invoice.zfill(9)),
            (f"vendor_{vendor}\twest", invoice),
        ),
    )
    name, raw_a, raw_b = variants[variant]
    if name == "unicode_casefold":
        canonical_vendor = f"strasse{vendor} west"
    return Scenario(
        (canonical_vendor, invoice),
        amounts.kind,
        amounts.amounts_a,
        amounts.amounts_b,
        raw_a if amounts.amounts_a else None,
        raw_b if amounts.amounts_b else None,
        name,
    )


def _validate_normalization_keys(scenarios: tuple[Scenario, ...]) -> None:
    """Guard the construction's one-to-one raw/canonical relationship per source.

    Distinct plans cannot share a canonical key or an original key. All rows in
    one plan reuse its single raw key, so detail rows are exact duplicates rather
    than distinct originals converging. This check needs no normalization code.
    """

    canonical = set()
    originals_a = {}
    originals_b = {}
    for scenario in scenarios:
        if scenario.key in canonical:
            raise AssertionError(f"Duplicate canonical construction key: {scenario.key}")
        canonical.add(scenario.key)
        for amounts, raw_key, originals in (
            (scenario.amounts_a, scenario.raw_key_a, originals_a),
            (scenario.amounts_b, scenario.raw_key_b, originals_b),
        ):
            if not amounts:
                if raw_key is not None:
                    raise AssertionError("Absent source must not have a raw key")
                continue
            if raw_key is None or len(raw_key) != 2 or not all(raw_key):
                raise AssertionError("Every present source must have a raw composite key")
            if raw_key in originals:
                raise AssertionError(f"Original key reused across canonical plans: {raw_key}")
            originals[raw_key] = scenario.key


def _money_text(units: int, variant: int) -> str:
    whole, fraction = divmod(abs(units), 10_000)
    places = 4 if units % 100 or variant % 9 == 0 else 2
    integer = format(whole, ",") if variant % 4 == 0 else str(whole)
    text = f"{integer}.{fraction:04d}" if places == 4 else f"{integer}.{fraction // 100:02d}"
    if variant % 4 == 1:
        text = "$" + text
    if units < 0:
        text = f"({text})" if variant % 4 == 2 else "-" + text
    return f" {text} " if variant % 13 == 0 else text


def _csv(scenarios: tuple[Scenario, ...], side: str, shuffle_seed: int) -> str:
    rows = [
        (index, amount, variant)
        for index, scenario in enumerate(scenarios)
        for variant, amount in enumerate(scenario.amounts_a if side == "A" else scenario.amounts_b)
    ]
    Random(shuffle_seed).shuffle(rows)
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(HEADERS_A if side == "A" else HEADERS_B)
    descriptions = (
        "Synthetic supplies, north office",
        'Synthetic consulting "phase A", review',
        "Café supplies, synthetic batch",
        "Synthetic freight, route\nnorth",
    )
    for index, amount, variant in rows:
        scenario = scenarios[index]
        raw_key = scenario.raw_key_a if side == "A" else scenario.raw_key_b
        vendor, invoice = raw_key if raw_key is not None else scenario.key
        if scenario.key_variant is None and index % 23 == 0:
            vendor = f" {vendor} " if side == "A" else vendor
            invoice = f" {invoice} " if side == "B" else invoice
        writer.writerow(
            (
                vendor,
                invoice,
                (date(2026, 1, 1) + timedelta(days=index % 365)).isoformat(),
                descriptions[index % len(descriptions)],
                f"{'D' if side == 'A' else 'CC-'}{index % 12:02d}",
                _money_text(amount, index + variant),
            )
        )
    return output.getvalue()


# This dedicated workload does not reuse or alter any historical generation recipe.
COMPARISON_HEADERS_A = (
    "Vendor ID",
    "Invoice Number",
    "Department",
    "Currency",
    "Posting Date",
    "Amount",
)
COMPARISON_HEADERS_B = (
    "Supplier",
    "Invoice Ref",
    "Cost Center",
    "Currency Code",
    "Document Date",
    "Gross Amount",
)
COMPARISON_KEYS_A = COMPARISON_HEADERS_A[:2]
COMPARISON_KEYS_B = COMPARISON_HEADERS_B[:2]
COMPARISON_AMOUNT_A = COMPARISON_HEADERS_A[-1]
COMPARISON_AMOUNT_B = COMPARISON_HEADERS_B[-1]
COMPARISON_MAPPINGS = (
    ("Department", "Cost Center"),
    ("Currency", "Currency Code"),
    ("Posting Date", "Document Date"),
)


@dataclass(frozen=True, slots=True)
class ComparisonScenario:
    key: tuple[str, str]
    kind: str
    amounts_a: tuple[int, ...]
    amounts_b: tuple[int, ...]
    values_a: tuple[tuple[str, str, str], ...]
    values_b: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class ExpectedComparison:
    mapping: tuple[str, str]
    values_a: tuple[str, ...]
    values_b: tuple[str, ...]
    status: str


@dataclass(frozen=True, slots=True)
class ExpectedRecord:
    source: str
    source_row: int
    key: tuple[str, str]
    units: int
    raw_fields: dict[str, str]

    @property
    def amount(self) -> Decimal:
        return decimal_units(self.units)


@dataclass(frozen=True, slots=True)
class ExpectedComparisonGroup:
    key: tuple[str, str]
    category: str
    rows_a: int
    rows_b: int
    units_a: int
    units_b: int
    records_a: tuple[ExpectedRecord, ...]
    records_b: tuple[ExpectedRecord, ...]
    field_comparisons: tuple[ExpectedComparison, ...]

    @property
    def has_secondary_mismatch(self) -> bool:
        return any(field.status == "mismatch" for field in self.field_comparisons)

    @property
    def is_exception(self) -> bool:
        return (
            self.category not in ("exact_match", "within_tolerance") or self.has_secondary_mismatch
        )

    @property
    def delta_units(self) -> int:
        return self.units_a - self.units_b

    @property
    def delta(self) -> Decimal:
        return decimal_units(self.delta_units)

    @property
    def amount_a(self) -> Decimal:
        return decimal_units(self.units_a)

    @property
    def amount_b(self) -> Decimal:
        return decimal_units(self.units_b)


@dataclass(frozen=True, slots=True)
class ComparisonGroundTruth:
    groups: tuple[ExpectedComparisonGroup, ...]
    record_count_a: int
    record_count_b: int
    category_counts: dict[str, int]
    total_a: Decimal
    total_b: Decimal
    control_difference: Decimal
    tolerated_delta_total: Decimal
    mode: str
    comparison_fields: tuple[tuple[str, str], ...] = COMPARISON_MAPPINGS

    @property
    def exception_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(group.key for group in self.groups if group.is_exception)

    @property
    def tolerated_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            group.key
            for group in self.groups
            if group.category == "within_tolerance" and not group.is_exception
        )

    def summary(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "records_a": self.record_count_a,
            "records_b": self.record_count_b,
            "key_groups": len(self.groups),
            "categories": self.category_counts,
            "total_a": str(self.total_a),
            "total_b": str(self.total_b),
            "control_difference": str(self.control_difference),
            "tolerated_delta_total": str(self.tolerated_delta_total),
            "exceptions": len(self.exception_keys),
            "tolerated": len(self.tolerated_keys),
            "secondary_mismatches": sum(group.has_secondary_mismatch for group in self.groups),
            "comparison_fields": self.comparison_fields,
        }


@dataclass(frozen=True, slots=True)
class ComparisonPair:
    csv_a: str
    csv_b: str
    scenarios: tuple[ComparisonScenario, ...]
    records_a: tuple[ExpectedRecord, ...]
    records_b: tuple[ExpectedRecord, ...]

    def expected(
        self, amount_tolerance: Decimal = Decimal("0"), *, mode: str = "unique"
    ) -> ComparisonGroundTruth:
        """Compute primitive truth from construction, not CSV parsing or product helpers.

        The grouped oracle compares each field independently. Matching Department
        and Currency sets do not validate associations between those fields, row
        correspondence, occurrence counts, or monetary allocation.
        """
        if not isinstance(amount_tolerance, Decimal):
            raise TypeError("amount_tolerance must be a Decimal")
        if not amount_tolerance.is_finite() or amount_tolerance < 0:
            raise ValueError("amount_tolerance must be finite and nonnegative")
        if mode not in MODES:
            raise ValueError("mode must be unique or grouped_by_key")
        records_a: dict[tuple[str, str], list[ExpectedRecord]] = {}
        records_b: dict[tuple[str, str], list[ExpectedRecord]] = {}
        for records, grouped in ((self.records_a, records_a), (self.records_b, records_b)):
            for record in records:
                grouped.setdefault(record.key, []).append(record)
        groups = []
        total_a = total_b = tolerated = 0
        counts = Counter(dict.fromkeys(CATEGORIES, 0))
        for scenario in sorted(self.scenarios, key=lambda item: item.key):
            amounts_a, amounts_b = scenario.amounts_a, scenario.amounts_b
            units_a, units_b = sum(amounts_a), sum(amounts_b)
            delta = units_a - units_b
            ambiguous = mode == "unique" and (len(amounts_a) > 1 or len(amounts_b) > 1)
            if ambiguous:
                category = "duplicate_ambiguous"
            elif not amounts_b:
                category = "a_only"
            elif not amounts_a:
                category = "b_only"
            elif delta == 0:
                category = "exact_match"
            elif decimal_units(delta).copy_abs() <= amount_tolerance:
                category = "within_tolerance"
            else:
                category = "amount_mismatch"
            comparable = bool(amounts_a and amounts_b) and not ambiguous
            comparisons = []
            for index, mapping in enumerate(COMPARISON_MAPPINGS):
                values_a = tuple(sorted({values[index] for values in scenario.values_a}))
                values_b = tuple(sorted({values[index] for values in scenario.values_b}))
                status = (
                    "not_comparable"
                    if not comparable
                    else "match"
                    if values_a == values_b
                    else "mismatch"
                )
                comparisons.append(ExpectedComparison(mapping, values_a, values_b, status))
            group = ExpectedComparisonGroup(
                scenario.key,
                category,
                len(amounts_a),
                len(amounts_b),
                units_a,
                units_b,
                tuple(records_a.get(scenario.key, ())),
                tuple(records_b.get(scenario.key, ())),
                tuple(comparisons),
            )
            if category == "within_tolerance" and not group.is_exception:
                tolerated += delta
            counts[category] += 1
            total_a += units_a
            total_b += units_b
            groups.append(group)
        # Integer invariants remain exact even under a tiny Decimal arithmetic context.
        if sum(group.delta_units for group in groups) != total_a - total_b:
            raise AssertionError("Construction financial deltas do not balance")
        if (
            sum(group.delta_units for group in groups if group.is_exception) + tolerated
            != total_a - total_b
        ):
            raise AssertionError(
                "Review and accepted tolerance do not partition control difference"
            )
        if sum(group.rows_a for group in groups) != len(self.records_a) or sum(
            group.rows_b for group in groups
        ) != len(self.records_b):
            raise AssertionError("Construction lost a source row")
        return ComparisonGroundTruth(
            tuple(groups),
            len(self.records_a),
            len(self.records_b),
            dict(counts),
            decimal_units(total_a),
            decimal_units(total_b),
            decimal_units(total_a - total_b),
            decimal_units(tolerated),
            mode,
        )


def _comparison_detail(total: int, *, longer: bool = False) -> tuple[int, ...]:
    parts = (total - 90_000, 100_000, -10_000, 0)
    return parts + (17, -17) if longer else parts


def _comparison_scenario(index: int, rng: Random) -> ComparisonScenario:
    key = (f"V{index % 137:06d}", f"INV-{index // 137:09d}")
    base = rng.randint(2_500, 25_000_000) * 100
    if index % 17 == 0:
        base = 0
    elif index % 11 == 0:
        base = -base
    if index % 9 == 0 and base:
        base += 17
    day = (date(2026, 1, 1) + timedelta(days=index % 365)).isoformat()
    slot = index % 100
    default = ("Sales", "USD", day)
    special_matches = (
        ("", "", ""),
        (" Sales ", "USD", day),
        ("Café", "EUR", day),
        ("東京", "JPY", day),
        ("Sales\nNorth", "USD", day),
        ("=SUM(1,2)", "USD", day),
        ("100.00", "USD", "01/02/2026"),
    )
    a, b = (base,), (base,)
    values_a = values_b = (default,)
    kind = "one_exact_match"
    if slot < 20:
        values_a = values_b = (special_matches[slot % len(special_matches)],)
    elif slot < 30:
        edge_a, edge_b = (
            ("", "Sales"),
            (" Sales ", "Sales"),
            ("SALES", "sales"),
            ("ACME-01", "ACME01"),
            ("Café", "Cafe\u0301"),
            ("東京", "大阪"),
            ("Sales\nNorth", "Sales North"),
            ("=1+1", "2"),
            ("100", "100.00"),
            ("2026-01-01", "01/01/2026"),
        )[slot - 20]
        count = 1 + (slot - 20) % 3
        values_a = ((edge_a, "USD", day),)
        values_b = ((edge_b, "usd" if count >= 2 else "USD", "01/01/2026" if count == 3 else day),)
        kind = f"one_exact_{count}_secondary_mismatches"
    elif slot < 40:
        delta = (100, -100, 1, -1, 50)[slot % 5]
        b = (base - delta,)
        if slot >= 35:
            values_b = (("Marketing", "USD", day),)
        kind = "one_tolerance_mismatch" if slot >= 35 else "one_tolerance_match"
    elif slot < 50:
        delta = (101, -101, 2501, -5000, 301)[slot % 5]
        b = (base - delta,)
        if slot >= 45:
            values_b = (("Marketing", "EUR", "01/01/2026"),)
        kind = (
            "one_amount_mismatch_secondary_mismatch"
            if slot >= 45
            else "one_amount_mismatch_secondary_match"
        )
    elif slot < 55:
        a, b = (0 if slot == 50 else base,), ()
        values_b = ()
        kind = "a_only"
    elif slot < 60:
        a, b = (), (0 if slot == 55 else base,)
        values_a = ()
        kind = "b_only"
    elif slot < 64:
        a, b = (base,), (base // 2, base - base // 2)
        values_b = (default, default)
        kind = "grouped_same_set_different_counts"
    elif slot < 68:
        a = b = (base // 2, base - base // 2)
        values_a = (default, default)
        values_b = (default, ("Marketing", "USD", day))
        kind = "grouped_repeated_changed_set"
    elif slot < 72:
        a = b = (base // 2, base - base // 2)
        values_a = (default, ("Marketing", "EUR", day))
        values_b = tuple(reversed(values_a))
        kind = "grouped_reordered_sets"
    elif slot < 76:
        a = b = (base // 2, base - base // 2)
        values_a = (default, ("Marketing", "EUR", day))
        values_b = (("Sales", "EUR", day), ("Marketing", "USD", day))
        kind = "grouped_cross_field_associations_not_validated"
    elif slot < 96:
        delta = (
            0
            if slot < 80
            else (100, -100, 1, -1)[slot % 4]
            if slot < 88
            else (101, -101, 2501, -5000)[slot % 4]
        )
        a, b = _comparison_detail(base), _comparison_detail(base - delta, longer=True)
        pattern = (default, ("Marketing", "EUR", day), ("", "", ""))
        values_a = tuple(pattern[row % 3] for row in range(len(a)))
        values_b = tuple(pattern[row % 3] for row in range(len(b)))
        secondary_mismatch = 84 <= slot < 88 or slot >= 92
        if secondary_mismatch:
            values_b = (*values_b[:-1], ("Zero or offset difference", "JPY", "01/01/2026"))
        kind = (
            "grouped_exact_all_row_evidence"
            if slot < 80
            else "grouped_tolerance_secondary_mismatch"
            if 84 <= slot < 88
            else "grouped_tolerance_secondary_match"
            if slot < 88
            else "grouped_amount_mismatch_secondary_mismatch"
            if secondary_mismatch
            else "grouped_amount_mismatch_secondary_match"
        )
    elif slot == 96:
        positive = abs(base) or 1_000_000
        a = b = (positive, -positive, 0, 17, -17)
        values_a = (
            ("Debit", "USD", day),
            ("Credit", "EUR", day),
            ("Zero", "", ""),
            ("Offset A", "JPY", day),
            ("", "USD", day),
        )
        values_b = (*values_a[:2], ("Only B zero", "", ""), ("Offset B", "JPY", day), values_a[-1])
        kind = "grouped_zero_credit_offset_evidence"
    elif slot == 97:
        a, b = (0, 0), (0, 0, 0)
        values_a, values_b = (("", "", ""),) * 2, (("", "", ""),) * 3
        kind = "grouped_zero_lines_blank_match"
    else:
        positive = abs(base) or 1_000_000
        parts = (positive, -positive, 0)
        evidence = (default, ("Credit", "EUR", day), ("", "", ""))
        a, b = (parts, ()) if slot == 98 else ((), parts)
        values_a, values_b = (evidence, ()) if slot == 98 else ((), evidence)
        kind = "a_only_multirow_zero_net" if slot == 98 else "b_only_multirow_zero_net"
    return ComparisonScenario(key, kind, a, b, values_a, values_b)


def _comparison_csv(
    scenarios: tuple[ComparisonScenario, ...], side: str, shuffle_seed: int
) -> tuple[str, tuple[ExpectedRecord, ...]]:
    rows = [
        (index, scenario, amount, values, variant)
        for index, scenario in enumerate(scenarios)
        for variant, (amount, values) in enumerate(
            zip(
                scenario.amounts_a if side == "A" else scenario.amounts_b,
                scenario.values_a if side == "A" else scenario.values_b,
                strict=True,
            )
        )
    ]
    Random(shuffle_seed).shuffle(rows)
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    headers = COMPARISON_HEADERS_A if side == "A" else COMPARISON_HEADERS_B
    writer.writerow(headers)
    records = []
    for source_row, (index, scenario, amount, values, variant) in enumerate(rows, start=2):
        raw_values = (*scenario.key, *values, _money_text(amount, index + variant))
        # Evidence and row identity are constructed before the CSV writer or any parser.
        records.append(
            ExpectedRecord(
                side, source_row, scenario.key, amount, dict(zip(headers, raw_values, strict=True))
            )
        )
        writer.writerow(raw_values)
    return output.getvalue(), tuple(records)


def _generate_comparison_pair(groups: int, seed: int, shuffle_seed: int) -> ComparisonPair:
    rng = Random(seed)
    scenarios = tuple(_comparison_scenario(index, rng) for index in range(groups))
    csv_a, records_a = _comparison_csv(scenarios, "A", shuffle_seed)
    csv_b, records_b = _comparison_csv(scenarios, "B", shuffle_seed + 1)
    return ComparisonPair(csv_a, csv_b, scenarios, records_a, records_b)


def generate_pair(
    groups: int = 1_000,
    *,
    seed: int = 42,
    shuffle_seed: int | None = None,
    workload: str = "legacy",
) -> SyntheticPair | ComparisonPair:
    """Generate in memory from integer construction plans, shuffled separately per file."""

    if isinstance(groups, bool) or not isinstance(groups, int) or groups < 100:
        raise ValueError("groups must be an integer of at least 100 to include every scenario")
    if workload not in WORKLOADS:
        raise ValueError(f"workload must be one of {WORKLOADS}")
    if workload == "comparison":
        return _generate_comparison_pair(
            groups, seed, seed if shuffle_seed is None else shuffle_seed
        )
    rng = Random(seed)
    recipe = {
        "legacy": _scenario,
        "grouped": _grouped_scenario,
        "normalization": _normalization_scenario,
    }[workload]
    scenarios = tuple(recipe(index, rng) for index in range(groups))
    if workload == "normalization":
        _validate_normalization_keys(scenarios)
    order_seed = seed if shuffle_seed is None else shuffle_seed
    return SyntheticPair(
        _csv(scenarios, "A", order_seed), _csv(scenarios, "B", order_seed + 1), scenarios
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", type=int, default=1_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tolerance", default="0.01")
    parser.add_argument("--workload", choices=WORKLOADS, default="legacy")
    parser.add_argument("--mode", choices=MODES, default="unique")
    parser.add_argument(
        "--write", action="store_true", help="Write only under ignored benchmark_output/"
    )
    args = parser.parse_args()
    pair = generate_pair(args.groups, seed=args.seed, workload=args.workload)
    summary = json.dumps(
        {
            "seed": args.seed,
            "workload": args.workload,
            "amount_tolerance": args.tolerance,
            "key_normalization": NORMALIZATION_RULES if args.workload == "normalization" else None,
            **(
                {
                    "key_columns": {"file_a": COMPARISON_KEYS_A, "file_b": COMPARISON_KEYS_B},
                    "amount_columns": {
                        "file_a": COMPARISON_AMOUNT_A,
                        "file_b": COMPARISON_AMOUNT_B,
                    },
                }
                if args.workload == "comparison"
                else {}
            ),
            **pair.expected(Decimal(args.tolerance), mode=args.mode).summary(),
        },
        indent=2,
    )
    print(summary)
    if args.write:
        directory = (
            Path(__file__).resolve().parents[1]
            / "benchmark_output"
            / (f"{args.workload}-groups-{args.groups}-seed-{args.seed}-{args.mode}")
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "file_a.csv").write_text(pair.csv_a, encoding="utf-8", newline="")
        (directory / "file_b.csv").write_text(pair.csv_b, encoding="utf-8", newline="")
        (directory / "expected.json").write_text(summary + "\n", encoding="utf-8")
        print(f"Synthetic fixtures written to {directory}")


if __name__ == "__main__":
    main()
