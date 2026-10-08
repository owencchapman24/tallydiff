"""Deterministic synthetic financial exports and independent integer-based ground truth.

Developer fixtures only. This module deliberately does not import TallyDiff.
The legacy workload is retained; the grouped workload adds 30 multi-row keys
per hundred groups, with split detail, credits, offsets, and zero-net groups.
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
WORKLOADS = ("legacy", "grouped")

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


@dataclass(frozen=True, slots=True)
class ExpectedGroup:
    key: tuple[str, str]
    category: str
    rows_a: int
    rows_b: int
    units_a: int
    units_b: int

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
        return tuple(
            group.key
            for group in self.groups
            if group.category not in ("exact_match", "within_tolerance")
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
        vendor, invoice = scenario.key
        if index % 23 == 0:
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


def generate_pair(
    groups: int = 1_000,
    *,
    seed: int = 42,
    shuffle_seed: int | None = None,
    workload: str = "legacy",
) -> SyntheticPair:
    """Generate in memory from integer construction plans, shuffled separately per file."""

    if isinstance(groups, bool) or not isinstance(groups, int) or groups < 100:
        raise ValueError("groups must be an integer of at least 100 to include every scenario")
    if workload not in WORKLOADS:
        raise ValueError("workload must be legacy or grouped")
    rng = Random(seed)
    recipe = _scenario if workload == "legacy" else _grouped_scenario
    scenarios = tuple(recipe(index, rng) for index in range(groups))
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
