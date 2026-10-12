"""Independent developer truth: signed integer DP and physical-row bitmasks.

All expected evidence, including CSV bytes, comes from construction plans.
Only the standard library is used. The historical synthetic recipes are separate.
"""

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from io import StringIO
from math import comb
from pathlib import Path
from random import Random

HEADERS_A = ("Vendor ID", "Invoice Number", "Department", "Currency", "Posting Date", "Amount")
HEADERS_B = (
    "Supplier",
    "Invoice Ref",
    "Cost Center",
    "Currency Code",
    "Document Date",
    "Gross Amount",
)
MAPPINGS = tuple(zip(HEADERS_A[2:5], HEADERS_B[2:5], strict=True))
MODES = ("unique", "grouped_by_key", "bounded_one_to_many")
CATEGORIES = (
    "exact_match",
    "within_tolerance",
    "amount_mismatch",
    "a_only",
    "b_only",
    "duplicate_ambiguous",
)
STATUSES = (
    "unique_exact",
    "ambiguous",
    "no_exact_subset",
    "singleton_only",
    "bound_exceeded",
    "not_eligible",
)
# Independent literal contract: never read these from the application.
POLICY = {
    "policy_id": "exact_unique_v1",
    "max_candidate_rows": 12,
    "minimum_accepted_subset_rows": 2,
    "max_planned_combinations_per_run": 1_000_000,
    "max_ambiguity_witnesses": 2,
    "exact_amounts_only": True,
    "physical_row_uniqueness": True,
    "singleton_rivals_count": True,
}


def money(units: int, *, signed: bool = False) -> str:
    whole, fraction = divmod(abs(units), 10_000)
    sign = "-" if units < 0 else "+" if signed and units > 0 else ""
    return f"{sign}{whole}.{fraction:04d}"


def amount(units: int) -> Decimal:
    return Decimal(money(units))


@dataclass(frozen=True, slots=True)
class SubsetScenario:
    key: tuple[str, str]
    kind: str
    amounts_a: tuple[int, ...]
    amounts_b: tuple[int, ...]
    values_a: tuple[tuple[str, str, str], ...] = ()
    values_b: tuple[tuple[str, str, str], ...] = ()
    original_key_a: tuple[str, str] | None = None
    original_key_b: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ExpectedSubsetRecord:
    source: str
    source_row: int
    key: tuple[str, str]
    canonical_key: tuple[str, str]
    units: int
    raw_fields: dict[str, str]

    def reference(self) -> dict:
        return {
            "source": self.source,
            "source_row": self.source_row,
            "amount": money(self.units),
            "original_key": list(self.key),
        }


@dataclass(frozen=True, slots=True)
class ExpectedSubsetSolution:
    units: int
    rows_a: tuple[ExpectedSubsetRecord, ...]
    rows_b: tuple[ExpectedSubsetRecord, ...]

    def evidence(self) -> dict:
        return {
            "amount": money(self.units),
            "rows_a": [row.reference() for row in self.rows_a],
            "rows_b": [row.reference() for row in self.rows_b],
        }


@dataclass(frozen=True, slots=True)
class ExpectedCorrespondence:
    status: str
    reason: str | None
    anchor: ExpectedSubsetRecord | None
    accepted_solution: ExpectedSubsetSolution | None
    unassigned_rows_a: tuple[ExpectedSubsetRecord, ...]
    unassigned_rows_b: tuple[ExpectedSubsetRecord, ...]
    ambiguity_witnesses: tuple[ExpectedSubsetSolution, ...]
    planned_combinations: int = 0
    examined_combinations: int = 0
    reserved_combinations: int = 0
    search_complete: bool = False

    def evidence(self, key: tuple[str, str]) -> dict:
        return {
            "schema_version": 1,
            "policy": dict(POLICY),
            "matching_key": list(key),
            "status": self.status,
            "reason": self.reason,
            "anchor": self.anchor.reference() if self.anchor else None,
            "accepted_solution": self.accepted_solution.evidence()
            if self.accepted_solution
            else None,
            "unassigned_rows_a": [row.reference() for row in self.unassigned_rows_a],
            "unassigned_rows_b": [row.reference() for row in self.unassigned_rows_b],
            "ambiguity_witnesses": [solution.evidence() for solution in self.ambiguity_witnesses],
            "planned_combinations": self.planned_combinations,
            "examined_combinations": self.examined_combinations,
            "reserved_combinations": self.reserved_combinations,
            "search_complete": self.search_complete,
        }


def mask_order(mask: int, count: int) -> tuple[int, tuple[int, ...]]:
    indices = tuple(index for index in range(count) if mask & (1 << index))
    return len(indices), indices


def dp_witness_masks(target: int, candidates: tuple[int, ...]) -> tuple[int, ...]:
    """Saturate each sum at two nonempty masks, preserving declared witness order.

    Extension by the next (largest) row index preserves cardinality/lexicographic
    order. Therefore a discarded third mask can never outrank both retained masks
    after any common extension. The empty seed is implicit and never a solution;
    singleton insertion keeps it available even when sum zero has two witnesses.
    """
    states: dict[int, tuple[int, ...]] = {}
    for index, units in enumerate(candidates):
        bit = 1 << index
        added: dict[int, list[int]] = {units: [bit]}
        for subtotal, masks in states.items():
            added.setdefault(subtotal + units, []).extend(mask | bit for mask in masks)
        for subtotal, masks in added.items():
            pool = (*states.get(subtotal, ()), *masks)
            states[subtotal] = tuple(sorted(pool, key=lambda mask: mask_order(mask, index + 1))[:2])
    return states.get(target, ())


def witness_rank(mask: int, count: int) -> int:
    """One-based cardinality then lexicographic rank, independently using math.comb."""
    cardinality, indices = mask_order(mask, count)
    if not cardinality or mask >= 1 << count:
        raise ValueError("rank requires a nonempty mask inside the candidate set")
    rank = 1 + sum(comb(count, smaller) for smaller in range(1, cardinality))
    previous = -1
    for position, selected in enumerate(indices):
        rank += sum(
            comb(count - skipped - 1, cardinality - position - 1)
            for skipped in range(previous + 1, selected)
        )
        previous = selected
    return rank


def correspondence(rows_a, rows_b, remaining: int, exhausted: bool = False):
    """Return independent whole-parent analysis and the new sticky run state."""
    if len(rows_a) <= 1 and len(rows_b) <= 1:
        return None, exhausted
    anchor = (
        rows_a[0]
        if len(rows_a) == 1 and len(rows_b) > 1
        else (rows_b[0] if len(rows_b) == 1 and len(rows_a) > 1 else None)
    )

    def unresolved(
        status, reason=None, planned=0, examined=0, reserved=0, complete=False, witnesses=()
    ):
        return ExpectedCorrespondence(
            status,
            reason,
            anchor,
            None,
            rows_a,
            rows_b,
            witnesses,
            planned,
            examined,
            reserved,
            complete,
        )

    if not rows_a or not rows_b:
        return unresolved("not_eligible", "missing_opposite_side"), exhausted
    if len(rows_a) > 1 and len(rows_b) > 1:
        return unresolved("not_eligible", "both_sides_multiple"), exhausted
    candidates = rows_b if anchor.source == "A" else rows_a
    count = len(candidates)
    if count > 12:
        return unresolved("bound_exceeded", "candidate_row_limit"), exhausted
    planned = (1 << count) - 1
    if exhausted or remaining < planned:
        return unresolved("bound_exceeded", "run_budget_exhausted", planned), True
    masks = dp_witness_masks(anchor.units, tuple(row.units for row in candidates))
    solutions = []
    for mask in masks:
        selected = tuple(row for index, row in enumerate(candidates) if mask & (1 << index))
        solutions.append(
            ExpectedSubsetSolution(
                anchor.units,
                (anchor,) if anchor.source == "A" else selected,
                selected if anchor.source == "A" else (anchor,),
            )
        )
    if len(masks) == 2:
        examined = witness_rank(masks[1], count)
        return unresolved(
            "ambiguous",
            planned=planned,
            examined=examined,
            reserved=planned,
            complete=examined == planned,
            witnesses=tuple(solutions),
        ), exhausted
    if not masks or masks[0].bit_count() == 1:
        return unresolved(
            "singleton_only" if masks else "no_exact_subset",
            planned=planned,
            examined=planned,
            reserved=planned,
            complete=True,
            witnesses=tuple(solutions),
        ), exhausted
    residuals = tuple(row for index, row in enumerate(candidates) if not masks[0] & (1 << index))
    return ExpectedCorrespondence(
        "unique_exact",
        None,
        anchor,
        solutions[0],
        () if anchor.source == "A" else residuals,
        residuals if anchor.source == "A" else (),
        (),
        planned,
        planned,
        planned,
        True,
    ), exhausted


@dataclass(frozen=True, slots=True)
class ExpectedSecondary:
    mapping: tuple[str, str]
    values_a: tuple[str, ...]
    values_b: tuple[str, ...]
    status: str


@dataclass(frozen=True, slots=True)
class ExpectedSubsetGroup:
    key: tuple[str, str]
    category: str
    records_a: tuple[ExpectedSubsetRecord, ...]
    records_b: tuple[ExpectedSubsetRecord, ...]
    units_a: int
    units_b: int
    field_comparisons: tuple[ExpectedSecondary, ...]
    analysis: ExpectedCorrespondence | None

    @property
    def delta_units(self):
        return self.units_a - self.units_b

    @property
    def inferred(self):
        return self.analysis is not None and self.analysis.accepted_solution is not None

    @property
    def has_secondary_mismatch(self):
        return any(field.status == "mismatch" for field in self.field_comparisons)

    @property
    def is_exception(self):
        return (
            self.category not in ("exact_match", "within_tolerance")
            or self.inferred
            or self.has_secondary_mismatch
        )


@dataclass(frozen=True, slots=True)
class SubsetGroundTruth:
    groups: tuple[ExpectedSubsetGroup, ...]
    mode: str
    tolerance: Decimal
    normalized: bool

    @property
    def exception_keys(self):
        return tuple(group.key for group in self.groups if group.is_exception)

    @property
    def tolerated_keys(self):
        return tuple(
            group.key
            for group in self.groups
            if group.category == "within_tolerance" and not group.is_exception
        )

    def summary(self):
        analyses = tuple(group.analysis for group in self.groups if group.analysis is not None)
        total_a = sum(group.units_a for group in self.groups)
        total_b = sum(group.units_b for group in self.groups)
        tolerated_keys = set(self.tolerated_keys)
        tolerated = sum(group.delta_units for group in self.groups if group.key in tolerated_keys)
        # Both accounting identities use integers, regardless of Decimal context.
        assert total_a - total_b == sum(group.delta_units for group in self.groups)
        assert (
            total_a - total_b
            == sum(group.delta_units for group in self.groups if group.is_exception) + tolerated
        )
        return {
            "mode": self.mode,
            "normalized": self.normalized,
            "key_groups": len(self.groups),
            "records_a": sum(len(group.records_a) for group in self.groups),
            "records_b": sum(len(group.records_b) for group in self.groups),
            "categories": {
                category: sum(group.category == category for group in self.groups)
                for category in CATEGORIES
            },
            "statuses": {
                status: sum(analysis.status == status for analysis in analyses)
                for status in STATUSES
            },
            "analyzed": len(analyses),
            "inferred": sum(group.inferred for group in self.groups),
            "exceptions": len(self.exception_keys),
            "tolerated": len(self.tolerated_keys),
            "total_a": money(total_a),
            "total_b": money(total_b),
            "control_difference": money(total_a - total_b),
            "tolerated_delta_total": money(tolerated),
            "planned": sum(analysis.planned_combinations for analysis in analyses),
            "reserved": sum(analysis.reserved_combinations for analysis in analyses),
            "examined": sum(analysis.examined_combinations for analysis in analyses),
        }


@dataclass(frozen=True, slots=True)
class SubsetSyntheticPair:
    csv_a: str
    csv_b: str
    scenarios: tuple[SubsetScenario, ...]
    records_a: tuple[ExpectedSubsetRecord, ...]
    records_b: tuple[ExpectedSubsetRecord, ...]
    normalized: bool = False

    def expected(self, tolerance=Decimal("0"), *, mode="bounded_one_to_many"):
        if not isinstance(tolerance, Decimal):
            raise TypeError("tolerance must be Decimal")
        if not tolerance.is_finite() or tolerance < 0:
            raise ValueError("tolerance must be finite and nonnegative")
        if mode not in MODES:
            raise ValueError("unknown mode")
        by_a, by_b = {}, {}
        for records, grouped in ((self.records_a, by_a), (self.records_b, by_b)):
            for row in records:
                grouped.setdefault(row.canonical_key, []).append(row)
        groups, remaining, exhausted = [], 1_000_000, False
        for scenario in sorted(self.scenarios, key=lambda item: item.key):
            a, b = tuple(by_a.get(scenario.key, ())), tuple(by_b.get(scenario.key, ()))
            ua, ub = sum(row.units for row in a), sum(row.units for row in b)
            analysis = None
            if mode == "bounded_one_to_many":
                analysis, exhausted = correspondence(a, b, remaining, exhausted)
                if analysis:
                    remaining -= analysis.reserved_combinations
            if analysis is not None:
                category = (
                    "duplicate_ambiguous"
                    if analysis.accepted_solution is None
                    else (
                        "amount_mismatch"
                        if analysis.unassigned_rows_a or analysis.unassigned_rows_b
                        else "exact_match"
                    )
                )
            elif mode == "unique" and (len(a) > 1 or len(b) > 1):
                category = "duplicate_ambiguous"
            elif not b:
                category = "a_only"
            elif not a:
                category = "b_only"
            elif ua == ub:
                category = "exact_match"
            elif amount(ua - ub).copy_abs() <= tolerance:
                category = "within_tolerance"
            else:
                category = "amount_mismatch"
            comparable = bool(a and b) and analysis is None and category != "duplicate_ambiguous"
            fields = []
            for names in MAPPINGS:
                va = tuple(sorted({row.raw_fields[names[0]] for row in a}))
                vb = tuple(sorted({row.raw_fields[names[1]] for row in b}))
                status = "not_comparable" if not comparable else "match" if va == vb else "mismatch"
                fields.append(ExpectedSecondary(names, va, vb, status))
            groups.append(
                ExpectedSubsetGroup(scenario.key, category, a, b, ua, ub, tuple(fields), analysis)
            )
        return SubsetGroundTruth(tuple(groups), mode, tolerance, self.normalized)


def pair_from_scenarios(scenarios, *, shuffle_seed=None, normalized=False):
    scenarios = tuple(scenarios)
    outputs = []
    for side, headers in (("A", HEADERS_A), ("B", HEADERS_B)):
        plans = []
        for scenario in scenarios:
            units = scenario.amounts_a if side == "A" else scenario.amounts_b
            values = scenario.values_a if side == "A" else scenario.values_b
            key = (
                scenario.original_key_a if side == "A" else scenario.original_key_b
            ) or scenario.key
            for index, value in enumerate(units):
                secondary = values[index] if values else ("Sales", "USD", "2026-09-01")
                plans.append((scenario.key, key, value, secondary))
        if shuffle_seed is not None:
            Random(shuffle_seed + (side == "B")).shuffle(plans)
        stream = StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(headers)
        records = []
        for ordinal, (canonical, original, units, secondary) in enumerate(plans, start=2):
            cells = (*original, *secondary, money(units))
            writer.writerow(cells)
            records.append(
                ExpectedSubsetRecord(
                    side,
                    ordinal,
                    original,
                    canonical,
                    units,
                    dict(zip(headers, cells, strict=True)),
                )
            )
        outputs.append((stream.getvalue(), tuple(records)))
    return SubsetSyntheticPair(
        outputs[0][0], outputs[1][0], scenarios, outputs[0][1], outputs[1][1], normalized
    )


# Amounts here are integer ten-thousandths. Scaling each plan preserves its math.
RECIPES = (
    ("ordinary_exact", (300,), (300,)),
    ("ordinary_tolerated", (300,), (250,)),
    ("ordinary_mismatch", (300,), (100,)),
    ("a_only", (300,), ()),
    ("b_only", (), (300,)),
    ("a_full", (300,), (100, 200)),
    ("b_full", (100, 200), (300,)),
    ("a_residual", (300,), (100, 200, 50)),
    ("b_residual", (100, 200, 50), (300,)),
    ("credit", (-300,), (-100, -200)),
    ("zero_unique", (0,), (150, -150)),
    ("different_combinations", (300,), (100, 200, 150, 150)),
    ("equal_physical", (300,), (100, 100, 200)),
    ("equal_pair", (200,), (100, 100)),
    ("singleton_rival", (300,), (300, 100, 200)),
    ("zero_ambiguity", (0,), (0, 0)),
    ("offset_ambiguity", (100,), (150, -50, 0)),
    ("singleton_only", (300,), (300, 50)),
    ("no_exact", (300,), (100, 250)),
    ("both_multiple", (300, 50), (100, 200)),
    ("missing_b", (100, 200), ()),
    ("missing_a", (), (100, 200)),
    ("case", (300,), (300,)),
    ("whitespace", (300,), (300,)),
    ("blank", (300,), (300,)),
    ("unicode", (300,), (300,)),
    ("tolerated_secondary", (300,), (250,)),
    ("large", (10**34 + 300,), (10**34 + 100, 200)),
    ("cancellation", (300,), (10**34, -(10**34) + 100, 200)),
    ("subcent", (3,), (1, 2)),
    ("reverse_credit", (-100, -200), (-300,)),
    ("reverse_zero", (150, -150), (0,)),
)


def generate_subset_pair(groups: int, *, seed=42, shuffle_seed=None, normalized=False):
    if type(groups) is not int or groups < 1:
        raise ValueError("groups must be a positive integer")
    rng = Random(seed)
    scenarios = []
    for index in range(groups):
        kind, a, b = RECIPES[index % len(RECIPES)]
        if index % 257 == 98:
            kind, a, b = "candidate_12", (4095,), tuple(1 << i for i in range(12))
        elif index % 257 == 99:
            kind, a, b = "candidate_13", (8191,), tuple(1 << i for i in range(13))
        scale = rng.randint(1, 9) * 10_000
        if kind.startswith("ordinary_tolerated") or kind == "tolerated_secondary":
            a, b = (300 * scale,), (300 * scale - 50,)
        elif kind == "subcent":
            pass
        else:
            a, b = tuple(value * scale for value in a), tuple(value * scale for value in b)
        key = (f"V{index:06d}", f"{index + 1}")
        raw_a = raw_b = None
        if normalized:
            key = (f"vendor {index:06d}", f"{index + 1}")
            raw_a = (f"VENDOR-  {index:06d}", f"000{index + 1}")
            raw_b = (f"vendor {index:06d}", f"{index + 1}")
        va = tuple(("Sales", "USD", "2026-09-01") for _ in a)
        vb = tuple(("Sales", "USD", "2026-09-01") for _ in b)
        special = {
            "case": "sales",
            "whitespace": "Sales ",
            "blank": "",
            "unicode": "Sa\u0301les",
            "tolerated_secondary": "Research",
        }
        if kind in special:
            vb = tuple((special[kind], "USD", "2026-09-01") for _ in b)
        elif index % 2 and len(b) > 1:
            vb = tuple(("財務" if i % 2 else "", "EUR", "2026-09-02") for i in range(len(b)))
        scenarios.append(SubsetScenario(key, kind, a, b, va, vb, raw_a, raw_b))
    return pair_from_scenarios(scenarios, shuffle_seed=shuffle_seed, normalized=normalized)


def calibration_pair(*, early_ambiguity=False):
    """Exactly one million reserved: 244*4095 + 511 + 103*3, then sticky failures."""
    scenarios = []
    for index, count in enumerate((*([12] * 244), 9, *([2] * 103), 2, 2, 13)):
        candidates = (0,) * count if early_ambiguity else tuple(1 << i for i in range(count))
        # Alternate exhaustive unique and no-exact searches, including the full set.
        anchor = 0 if early_ambiguity else sum(candidates) if index % 2 else -1
        scenarios.append(
            SubsetScenario((f"V{index:06d}", "1"), "calibration", (anchor,), candidates)
        )
    scenarios.extend(
        (
            SubsetScenario(("V999997", "1"), "both_after_budget", (1, 2), (1, 2)),
            SubsetScenario(("V999998", "1"), "missing_after_budget", (1, 2), ()),
            SubsetScenario(("V999999", "1"), "ordinary_after_budget", (1,), (1,)),
        )
    )
    return pair_from_scenarios(scenarios)


def expected_subset_export(truth: SubsetGroundTruth) -> bytes:
    """Entire public protocol, independently serialized with stdlib CSV and JSON."""
    labels = (
        "Exact match",
        "Within tolerance",
        "Amount mismatch",
        "File A only",
        "File B only",
        "Duplicate / ambiguous",
    )
    columns = [
        "Category",
        "Matching key",
        "File A amount",
        "File B amount",
        "Delta (A - B)",
        "File A records",
        "File B records",
        "Secondary differences",
    ]
    for index, names in enumerate(MAPPINGS, 1):
        columns.extend(
            (
                f"Comparison {index} File A values — {names[0]}",
                f"Comparison {index} File B values — {names[1]}",
                f"Comparison {index} status",
            )
        )
    analyzed = any(group.analysis is not None and group.is_exception for group in truth.groups)
    if analyzed:
        columns.extend(("Correspondence status", "Correspondence evidence"))
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(columns)
    for group in truth.groups:
        if not group.is_exception:
            continue
        row = [
            labels[CATEGORIES.index(group.category)],
            " / ".join(group.key),
            money(group.units_a) if group.records_a else "",
            money(group.units_b) if group.records_b else "",
            money(group.delta_units, signed=True),
            "; ".join(str(row.source_row) for row in group.records_a),
            "; ".join(str(row.source_row) for row in group.records_b),
            "; ".join(
                f"Comparison {index}: {field.mapping[0]} ↔ {field.mapping[1]}"
                for index, field in enumerate(group.field_comparisons, 1)
                if field.status == "mismatch"
            ),
        ]
        for field in group.field_comparisons:
            row.extend(
                (
                    json.dumps(field.values_a, ensure_ascii=False),
                    json.dumps(field.values_b, ensure_ascii=False),
                    field.status,
                )
            )
        if analyzed:
            row.extend(
                (
                    group.analysis.status,
                    json.dumps(
                        group.analysis.evidence(group.key),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                if group.analysis
                else ("", "")
            )
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def ordered_truth(truth):
    """Primitive complete ordered evidence for new frozen-history hashes."""
    return [asdict(group) for group in truth.groups]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--shuffle-seed", type=int)
    parser.add_argument("--normalized", action="store_true")
    parser.add_argument("--mode", choices=MODES, default="bounded_one_to_many")
    parser.add_argument("--tolerance", default="0.01")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    pair = generate_subset_pair(
        args.groups, seed=args.seed, shuffle_seed=args.shuffle_seed, normalized=args.normalized
    )
    print(
        json.dumps(pair.expected(Decimal(args.tolerance), mode=args.mode).summary(), sort_keys=True)
    )
    if args.write:
        directory = Path("benchmark_output")
        directory.mkdir(exist_ok=True)
        directory.joinpath("subset_file_a.csv").write_bytes(pair.csv_a.encode("utf-8"))
        directory.joinpath("subset_file_b.csv").write_bytes(pair.csv_b.encode("utf-8"))


if __name__ == "__main__":
    main()
