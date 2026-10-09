import ast
import csv
import hashlib
import inspect
import json
import sys
from collections import Counter
from decimal import Decimal, localcontext
from io import StringIO

import pytest

import scripts.synthetic_data as oracle
from scripts.synthetic_data import (
    CATEGORIES,
    COMPARISON_AMOUNT_A,
    COMPARISON_AMOUNT_B,
    COMPARISON_HEADERS_A,
    COMPARISON_HEADERS_B,
    COMPARISON_KEYS_A,
    COMPARISON_KEYS_B,
    COMPARISON_MAPPINGS,
    ComparisonPair,
    generate_pair,
)

# Captured from HEAD:scripts/synthetic_data.py before Task 6 changes. These
# anchors cover complete CSV bytes and every historical expected group, plus
# explicit financial/category summaries, in both modes at three seeds/scales.
FROZEN_HISTORICAL = json.loads(r"""[
  {
    "workload": "legacy",
    "groups": 100,
    "seed": 0,
    "shuffle_seed": 17,
    "sha_a": "7b915c90d893a5b68c65e8d0d38202ecd86d81ef91078d118d28bb2b32b4e9d5",
    "sha_b": "bee658ada0883b9e3aeb6c0c3e0bfc20196df5aaf3c372453350da48678c0719",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 99,
          "records_b": 97,
          "key_groups": 100,
          "categories": {
            "exact_match": 70,
            "within_tolerance": 10,
            "amount_mismatch": 8,
            "a_only": 4,
            "b_only": 4,
            "duplicate_ambiguous": 4
          },
          "total_a": "8867261.0536",
          "total_b": "8735464.0452",
          "control_difference": "131797.0084",
          "tolerated_delta_total": "0.0050",
          "exceptions": 20,
          "tolerated": 10
        },
        "groups_sha": "aa97c957912026f8565e36d73324d56973ee02568faf2c89c3069ca6aebf40db"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 99,
          "records_b": 97,
          "key_groups": 100,
          "categories": {
            "exact_match": 72,
            "within_tolerance": 11,
            "amount_mismatch": 8,
            "a_only": 5,
            "b_only": 4,
            "duplicate_ambiguous": 0
          },
          "total_a": "8867261.0536",
          "total_b": "8735464.0452",
          "control_difference": "131797.0084",
          "tolerated_delta_total": "0.0000",
          "exceptions": 17,
          "tolerated": 11
        },
        "groups_sha": "d32d876a92a1ece3d48e4c1007e3f535f3aec8c5c2da3ad1660adf8c89524576"
      }
    }
  },
  {
    "workload": "legacy",
    "groups": 257,
    "seed": 42,
    "shuffle_seed": null,
    "sha_a": "b04108f882484ffaaa228b6a830202e7b2006388de1f904e04b5b642ee034fc5",
    "sha_b": "3e41d8a9df7e2f31ac4b31e1985cf62b3e4a7f5ae3816a165c1b3090dafaccb3",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 253,
          "records_b": 253,
          "key_groups": 257,
          "categories": {
            "exact_match": 197,
            "within_tolerance": 20,
            "amount_mismatch": 16,
            "a_only": 8,
            "b_only": 8,
            "duplicate_ambiguous": 8
          },
          "total_a": "21668485.4291",
          "total_b": "21835378.9888",
          "control_difference": "-166893.5597",
          "tolerated_delta_total": "0.0100",
          "exceptions": 40,
          "tolerated": 20
        },
        "groups_sha": "3111ed2ba36ca98f288c42f1df9136c82ddd289d84356991b6e762405ad27992"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 253,
          "records_b": 253,
          "key_groups": 257,
          "categories": {
            "exact_match": 200,
            "within_tolerance": 23,
            "amount_mismatch": 16,
            "a_only": 9,
            "b_only": 9,
            "duplicate_ambiguous": 0
          },
          "total_a": "21668485.4291",
          "total_b": "21835378.9888",
          "control_difference": "-166893.5597",
          "tolerated_delta_total": "0.0001",
          "exceptions": 34,
          "tolerated": 23
        },
        "groups_sha": "daf809ca71d1be89697ee8a55222d6d3344997c100b88b0acc141710981ff5e2"
      }
    }
  },
  {
    "workload": "legacy",
    "groups": 1000,
    "seed": 2026,
    "shuffle_seed": 9,
    "sha_a": "54870fb6886abf16782689c3361c5d4175a99a2156cb0f62424b6f433438fc67",
    "sha_b": "1a87708154495a6d8f30c986d3b637647de0a306125bac3feaea57786d19822c",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 980,
          "records_b": 980,
          "key_groups": 1000,
          "categories": {
            "exact_match": 700,
            "within_tolerance": 100,
            "amount_mismatch": 80,
            "a_only": 40,
            "b_only": 40,
            "duplicate_ambiguous": 40
          },
          "total_a": "91493717.1115",
          "total_b": "90497981.4621",
          "control_difference": "995735.6494",
          "tolerated_delta_total": "0.0500",
          "exceptions": 200,
          "tolerated": 100
        },
        "groups_sha": "cba0a941cef444b78fb8bfe43ea97eb6796564367dae5458442ea61214da3a0e"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 980,
          "records_b": 980,
          "key_groups": 1000,
          "categories": {
            "exact_match": 719,
            "within_tolerance": 111,
            "amount_mismatch": 80,
            "a_only": 45,
            "b_only": 45,
            "duplicate_ambiguous": 0
          },
          "total_a": "91493717.1115",
          "total_b": "90497981.4621",
          "control_difference": "995735.6494",
          "tolerated_delta_total": "0.0001",
          "exceptions": 170,
          "tolerated": 111
        },
        "groups_sha": "c991952ad61d9038c57a03d837b342d072d3a5f5445bd2fe7fe439a91193cab8"
      }
    }
  },
  {
    "workload": "grouped",
    "groups": 100,
    "seed": 0,
    "shuffle_seed": 17,
    "sha_a": "c02a2c54b9e19ee9256c684d4f40bb74e7f97e30084e581fe0e7e747a1b23dcb",
    "sha_b": "ce2784c97928535249957cdda1cd3a73d8bf5a58f6ff73f85c948aaaa7e55bfd",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 175,
          "records_b": 178,
          "key_groups": 100,
          "categories": {
            "exact_match": 40,
            "within_tolerance": 10,
            "amount_mismatch": 10,
            "a_only": 5,
            "b_only": 5,
            "duplicate_ambiguous": 30
          },
          "total_a": "10000000009083896.0487",
          "total_b": "10000000008669936.1067",
          "control_difference": "413959.9420",
          "tolerated_delta_total": "0.0050",
          "exceptions": 50,
          "tolerated": 10
        },
        "groups_sha": "3859fea120e68738f57ba8eb31655fe739162706e8eb4d7add30f4b7c8b1ed43"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 175,
          "records_b": 178,
          "key_groups": 100,
          "categories": {
            "exact_match": 57,
            "within_tolerance": 15,
            "amount_mismatch": 16,
            "a_only": 6,
            "b_only": 6,
            "duplicate_ambiguous": 0
          },
          "total_a": "10000000009083896.0487",
          "total_b": "10000000008669936.1067",
          "control_difference": "413959.9420",
          "tolerated_delta_total": "0.0100",
          "exceptions": 28,
          "tolerated": 15
        },
        "groups_sha": "51eeb85114d2daf1c7c5f4c5d28882080188091e450c159506b3127e0a4e21a9"
      }
    }
  },
  {
    "workload": "grouped",
    "groups": 257,
    "seed": 42,
    "shuffle_seed": null,
    "sha_a": "486149a7d8b70206882e79d789b944713549191b6d4f5f904f94f9f82de5df94",
    "sha_b": "56678585e6d66c7db145dc41127b1cd040822b37ba2b4ceece03c9a8e4f57e14",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 425,
          "records_b": 419,
          "key_groups": 257,
          "categories": {
            "exact_match": 120,
            "within_tolerance": 30,
            "amount_mismatch": 27,
            "a_only": 10,
            "b_only": 10,
            "duplicate_ambiguous": 60
          },
          "total_a": "20000000023018584.3159",
          "total_b": "20000000022042176.5567",
          "control_difference": "976407.7592",
          "tolerated_delta_total": "0.0150",
          "exceptions": 107,
          "tolerated": 30
        },
        "groups_sha": "32a9923aaf9ee099f5ecefa6892995cf1d80e57b8b3fa92394d09553a5b90c5b"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 425,
          "records_b": 419,
          "key_groups": 257,
          "categories": {
            "exact_match": 154,
            "within_tolerance": 40,
            "amount_mismatch": 39,
            "a_only": 12,
            "b_only": 12,
            "duplicate_ambiguous": 0
          },
          "total_a": "20000000023018584.3159",
          "total_b": "20000000022042176.5567",
          "control_difference": "976407.7592",
          "tolerated_delta_total": "0.0250",
          "exceptions": 63,
          "tolerated": 40
        },
        "groups_sha": "f7ee1b5ef20cf89fb8720e60271707ce063a202fac2d3d335617a230bf92fedf"
      }
    }
  },
  {
    "workload": "grouped",
    "groups": 1000,
    "seed": 2026,
    "shuffle_seed": 9,
    "sha_a": "0e977c283420c986836fa5d3dfe5d9d59bfd905bcf7add464bfb6617b1847808",
    "sha_b": "8455124325536e909995d3fea1e5d4a257a96f913c2fb02b049b487b81db72b0",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 1814,
          "records_b": 1820,
          "key_groups": 1000,
          "categories": {
            "exact_match": 400,
            "within_tolerance": 100,
            "amount_mismatch": 100,
            "a_only": 50,
            "b_only": 50,
            "duplicate_ambiguous": 300
          },
          "total_a": "100000000087939655.5468",
          "total_b": "100000000087567676.6121",
          "control_difference": "371978.9347",
          "tolerated_delta_total": "0.0500",
          "exceptions": 500,
          "tolerated": 100
        },
        "groups_sha": "d948856d4462f37db370a6d12e30013596082a6a37937f9f0441030deb9891b5"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 1814,
          "records_b": 1820,
          "key_groups": 1000,
          "categories": {
            "exact_match": 570,
            "within_tolerance": 150,
            "amount_mismatch": 160,
            "a_only": 60,
            "b_only": 60,
            "duplicate_ambiguous": 0
          },
          "total_a": "100000000087939655.5468",
          "total_b": "100000000087567676.6121",
          "control_difference": "371978.9347",
          "tolerated_delta_total": "0.1000",
          "exceptions": 280,
          "tolerated": 150
        },
        "groups_sha": "af2cc3d469eed67e2702ad322ab7d8f62fbd2001ac90db7812910a455f73c063"
      }
    }
  },
  {
    "workload": "normalization",
    "groups": 100,
    "seed": 0,
    "shuffle_seed": 17,
    "sha_a": "607bed31edfe651f80c814175478e81bdf995ae15c7a012045834ab907b95211",
    "sha_b": "75cb8984eca1cdfc57fb0e83e37b527467e2854cf22c3e2c2ef3f5f4b38cdd4b",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 175,
          "records_b": 178,
          "key_groups": 100,
          "categories": {
            "exact_match": 40,
            "within_tolerance": 10,
            "amount_mismatch": 10,
            "a_only": 5,
            "b_only": 5,
            "duplicate_ambiguous": 30
          },
          "total_a": "10000000009083896.0487",
          "total_b": "10000000008669936.1067",
          "control_difference": "413959.9420",
          "tolerated_delta_total": "0.0050",
          "exceptions": 50,
          "tolerated": 10
        },
        "groups_sha": "a028b40c77ef06ef8c3bcfa2bac981bed2936b6ea7d84914f421b0f9ada8086e"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 175,
          "records_b": 178,
          "key_groups": 100,
          "categories": {
            "exact_match": 57,
            "within_tolerance": 15,
            "amount_mismatch": 16,
            "a_only": 6,
            "b_only": 6,
            "duplicate_ambiguous": 0
          },
          "total_a": "10000000009083896.0487",
          "total_b": "10000000008669936.1067",
          "control_difference": "413959.9420",
          "tolerated_delta_total": "0.0100",
          "exceptions": 28,
          "tolerated": 15
        },
        "groups_sha": "e635a37a02aafa4899f44ba4489c6c77317c50100db740db51779456bc3c789a"
      }
    }
  },
  {
    "workload": "normalization",
    "groups": 257,
    "seed": 42,
    "shuffle_seed": null,
    "sha_a": "e7947e872b08c52078df10f6cff1843ab000d204156ca4693eaa2aa574811e7c",
    "sha_b": "eb9b4b1cd173d099770cb9b7f3a12f15fcc5665228a1042c49cb70436647fb55",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 425,
          "records_b": 419,
          "key_groups": 257,
          "categories": {
            "exact_match": 120,
            "within_tolerance": 30,
            "amount_mismatch": 27,
            "a_only": 10,
            "b_only": 10,
            "duplicate_ambiguous": 60
          },
          "total_a": "20000000023018584.3159",
          "total_b": "20000000022042176.5567",
          "control_difference": "976407.7592",
          "tolerated_delta_total": "0.0150",
          "exceptions": 107,
          "tolerated": 30
        },
        "groups_sha": "4dcbe7d4d41eea7fdfe8fd370f03c33d3e17763f68907b58c791ed643a58951c"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 425,
          "records_b": 419,
          "key_groups": 257,
          "categories": {
            "exact_match": 154,
            "within_tolerance": 40,
            "amount_mismatch": 39,
            "a_only": 12,
            "b_only": 12,
            "duplicate_ambiguous": 0
          },
          "total_a": "20000000023018584.3159",
          "total_b": "20000000022042176.5567",
          "control_difference": "976407.7592",
          "tolerated_delta_total": "0.0250",
          "exceptions": 63,
          "tolerated": 40
        },
        "groups_sha": "f250bcf40b92431083399311eea18bf4f606122b2604cf301b5741ab222cf2d0"
      }
    }
  },
  {
    "workload": "normalization",
    "groups": 1000,
    "seed": 2026,
    "shuffle_seed": 9,
    "sha_a": "a04b4bb560fd8e41135f918d32606443bea7e686f9dea83a1a019534a1d21c8e",
    "sha_b": "2e0b19d7ec2f9aaab9dfed3b7ebab43dffdd8a492336d01406662a41ee833874",
    "truth": {
      "unique": {
        "summary": {
          "mode": "unique",
          "records_a": 1814,
          "records_b": 1820,
          "key_groups": 1000,
          "categories": {
            "exact_match": 400,
            "within_tolerance": 100,
            "amount_mismatch": 100,
            "a_only": 50,
            "b_only": 50,
            "duplicate_ambiguous": 300
          },
          "total_a": "100000000087939655.5468",
          "total_b": "100000000087567676.6121",
          "control_difference": "371978.9347",
          "tolerated_delta_total": "0.0500",
          "exceptions": 500,
          "tolerated": 100
        },
        "groups_sha": "edc105ac003da8e3e7f7e1e8f981780ae0368606822ca56f5c70982eaec29973"
      },
      "grouped_by_key": {
        "summary": {
          "mode": "grouped_by_key",
          "records_a": 1814,
          "records_b": 1820,
          "key_groups": 1000,
          "categories": {
            "exact_match": 570,
            "within_tolerance": 150,
            "amount_mismatch": 160,
            "a_only": 60,
            "b_only": 60,
            "duplicate_ambiguous": 0
          },
          "total_a": "100000000087939655.5468",
          "total_b": "100000000087567676.6121",
          "control_difference": "371978.9347",
          "tolerated_delta_total": "0.1000",
          "exceptions": 280,
          "tolerated": 150
        },
        "groups_sha": "33d0815381ed35bd4b0544525040aca3f8a3e436bc967213790cb1533442983d"
      }
    }
  }
]""")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    "frozen",
    FROZEN_HISTORICAL,
    ids=lambda item: f"{item['workload']}-{item['groups']}-{item['seed']}",
)
def test_historical_workloads_keep_committed_bytes_and_complete_truth(frozen):
    pair = generate_pair(
        frozen["groups"],
        seed=frozen["seed"],
        shuffle_seed=frozen["shuffle_seed"],
        workload=frozen["workload"],
    )
    assert hashlib.sha256(pair.csv_a.encode()).hexdigest() == frozen["sha_a"]
    assert hashlib.sha256(pair.csv_b.encode()).hexdigest() == frozen["sha_b"]
    for mode, expected in frozen["truth"].items():
        truth = pair.expected(Decimal("0.01"), mode=mode)
        assert truth.summary() == expected["summary"]
        assert (
            _digest(
                [
                    (
                        group.key,
                        group.category,
                        group.rows_a,
                        group.rows_b,
                        group.units_a,
                        group.units_b,
                        group.raw_key_a,
                        group.raw_key_b,
                        group.is_exception,
                    )
                    for group in truth.groups
                ]
            )
            == expected["groups_sha"]
        )


def test_comparison_oracle_has_only_stdlib_imports_and_no_product_dependencies(monkeypatch):
    tree = ast.parse(inspect.getsource(oracle))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imports.append(node.module.split(".")[0])
    assert set(imports) <= sys.stdlib_module_names
    forbidden_names = {
        "tallydiff",
        "reconcile",
        "FieldComparison",
        "FieldComparisonStatus",
        "ComparisonFieldMapping",
        "normalize_key",
        "review_exceptions",
        "export_exceptions_csv",
        "export_mapping_profile",
    }
    assert not any(
        isinstance(node, ast.Name)
        and node.id in forbidden_names
        or isinstance(node, ast.Attribute)
        and node.attr in forbidden_names
        for node in ast.walk(tree)
    )
    # No floating-point numeric literals enter any construction recipe.
    assert not any(
        isinstance(node, ast.Constant) and isinstance(node.value, float) for node in ast.walk(tree)
    )
    original_import = __import__

    def guard_import(name, *args, **kwargs):
        if name.startswith("tallydiff"):
            pytest.fail("The independent oracle attempted a product import")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guard_import)
    pair = generate_pair(100, workload="comparison")
    with localcontext() as context:
        context.prec = 2
        for mode in oracle.MODES:
            truth = pair.expected(Decimal("0.01"), mode=mode)
            assert len(truth.groups) == 100
            assert sum(group.delta_units for group in truth.groups) == sum(
                group.units_a for group in truth.groups
            ) - sum(group.units_b for group in truth.groups)


def test_comparison_schema_is_asymmetric_ordered_and_separate_from_historical_schema():
    assert COMPARISON_HEADERS_A == (
        "Vendor ID",
        "Invoice Number",
        "Department",
        "Currency",
        "Posting Date",
        "Amount",
    )
    assert COMPARISON_HEADERS_B == (
        "Supplier",
        "Invoice Ref",
        "Cost Center",
        "Currency Code",
        "Document Date",
        "Gross Amount",
    )
    assert COMPARISON_KEYS_A == COMPARISON_HEADERS_A[:2]
    assert COMPARISON_KEYS_B == COMPARISON_HEADERS_B[:2]
    assert COMPARISON_AMOUNT_A == "Amount" and COMPARISON_AMOUNT_B == "Gross Amount"
    assert COMPARISON_MAPPINGS == (
        ("Department", "Cost Center"),
        ("Currency", "Currency Code"),
        ("Posting Date", "Document Date"),
    )
    assert COMPARISON_AMOUNT_A not in {mapping[0] for mapping in COMPARISON_MAPPINGS}
    assert COMPARISON_AMOUNT_B not in {mapping[1] for mapping in COMPARISON_MAPPINGS}


@pytest.mark.parametrize("mode", oracle.MODES)
@pytest.mark.parametrize("tolerance", [Decimal("0"), Decimal("0.01")])
def test_comparison_recipe_has_hand_auditable_primary_and_review_partitions(mode, tolerance):
    pair = generate_pair(100, seed=42, workload="comparison")
    assert isinstance(pair, ComparisonPair)
    assert pair == generate_pair(100, seed=42, workload="comparison")
    assert pair != generate_pair(100, seed=43, workload="comparison")
    truth = pair.expected(tolerance, mode=mode)
    accepting = bool(tolerance)
    if mode == "unique":
        counts = (30, 10 if accepting else 0, 10 if accepting else 20, 5, 5, 40)
        exceptions, accepted = (75, 5) if accepting else (80, 0)
    else:
        counts = (52, 18 if accepting else 0, 18 if accepting else 36, 6, 6, 0)
        exceptions, accepted = (54, 9) if accepting else (63, 0)
    assert tuple(truth.category_counts) == CATEGORIES
    assert tuple(truth.category_counts.values()) == counts
    assert (truth.record_count_a, truth.record_count_b) == (173, 218)
    assert len(truth.exception_keys) == exceptions
    assert len(truth.tolerated_keys) == accepted
    assert truth.tolerated_delta_total == Decimal("0.0050" if accepting else "0")
    assert truth.comparison_fields == COMPARISON_MAPPINGS
    assert tuple(group.key for group in truth.groups) == tuple(
        sorted(scenario.key for scenario in pair.scenarios)
    )
    assert truth.exception_keys == tuple(group.key for group in truth.groups if group.is_exception)
    assert truth.tolerated_keys == tuple(
        group.key
        for group in truth.groups
        if group.category == "within_tolerance" and not group.has_secondary_mismatch
    )
    by_key = {group.key: group for group in truth.groups}
    exact_mismatch = by_key[pair.scenarios[20].key]
    assert exact_mismatch.category == "exact_match" and exact_mismatch.delta_units == 0
    assert exact_mismatch.has_secondary_mismatch and exact_mismatch.is_exception
    if accepting:
        accepted_group = by_key[pair.scenarios[30].key]
        review_group = by_key[pair.scenarios[35].key]
        assert accepted_group.category == review_group.category == "within_tolerance"
        assert not accepted_group.is_exception and review_group.is_exception
        assert (
            accepted_group.key in truth.tolerated_keys
            and review_group.key not in truth.tolerated_keys
        )
    for index in (40, 45):
        assert by_key[pair.scenarios[index].key].is_exception
    assert not by_key[pair.scenarios[40].key].has_secondary_mismatch
    assert by_key[pair.scenarios[45].key].has_secondary_mismatch


@pytest.mark.parametrize(
    "index, statuses",
    [
        (60, ("match", "match", "match")),
        (64, ("mismatch", "match", "match")),
        (68, ("match", "match", "match")),
        (72, ("match", "match", "match")),
        (76, ("match", "match", "match")),
        (80, ("match", "match", "match")),
        (84, ("mismatch", "mismatch", "mismatch")),
        (88, ("match", "match", "match")),
        (92, ("mismatch", "mismatch", "mismatch")),
        (96, ("mismatch", "match", "match")),
        (97, ("match", "match", "match")),
        (98, ("not_comparable", "not_comparable", "not_comparable")),
        (99, ("not_comparable", "not_comparable", "not_comparable")),
    ],
)
def test_grouped_distinct_set_recipe_anchors_counts_order_and_every_row(index, statuses):
    pair = generate_pair(100, workload="comparison")
    scenario = pair.scenarios[index]
    truth = pair.expected(Decimal("0.01"), mode="grouped_by_key")
    group = next(group for group in truth.groups if group.key == scenario.key)
    assert tuple(field.status for field in group.field_comparisons) == statuses
    for field_index, field in enumerate(group.field_comparisons):
        assert field.mapping == COMPARISON_MAPPINGS[field_index]
        assert field.values_a == tuple(sorted({row[field_index] for row in scenario.values_a}))
        assert field.values_b == tuple(sorted({row[field_index] for row in scenario.values_b}))
    if index == 60:
        assert (group.rows_a, group.rows_b) == (1, 2)
        assert (
            group.field_comparisons[0].values_a == group.field_comparisons[0].values_b == ("Sales",)
        )
    if index == 64:
        assert group.field_comparisons[0].values_a == ("Sales",)
        assert group.field_comparisons[0].values_b == ("Marketing", "Sales")
    if index == 72:
        # Equal independent sets intentionally say nothing about Department/Currency associations.
        assert set((values[0], values[1]) for values in scenario.values_a) != set(
            (values[0], values[1]) for values in scenario.values_b
        )
        assert not group.has_secondary_mismatch
    if index == 96:
        assert any(record.units < 0 for record in group.records_a)
        assert any(record.units == 0 for record in group.records_a)
        assert group.units_a == group.units_b == 0
        assert group.field_comparisons[0].values_a == ("", "Credit", "Debit", "Offset A", "Zero")
        assert group.field_comparisons[0].values_b == (
            "",
            "Credit",
            "Debit",
            "Offset B",
            "Only B zero",
        )
    if index == 97:
        assert group.field_comparisons[0].values_a == group.field_comparisons[0].values_b == ("",)
        assert (group.rows_a, group.rows_b) == (2, 3)
    if index in (98, 99):
        assert group.delta_units == 0 and group.is_exception
        assert (
            group.field_comparisons[0].values_a == ()
            if index == 99
            else group.field_comparisons[0].values_b == ()
        )
        assert (
            group.field_comparisons[0].values_b
            if index == 99
            else group.field_comparisons[0].values_a
        )


@pytest.mark.parametrize("index", range(20, 30))
def test_exact_evidence_edge_cases_are_never_smart_equivalent(index):
    pair = generate_pair(100, workload="comparison")
    scenario = pair.scenarios[index]
    group = next(group for group in pair.expected().groups if group.key == scenario.key)
    assert group.field_comparisons[0].status == "mismatch"
    assert group.field_comparisons[0].values_a == (scenario.values_a[0][0],)
    assert group.field_comparisons[0].values_b == (scenario.values_b[0][0],)
    assert (
        sum(field.status == "mismatch" for field in group.field_comparisons) == 1 + (index - 20) % 3
    )
    assert group.category == "exact_match" and group.delta_units == 0 and group.is_exception


@pytest.mark.parametrize("mode", oracle.MODES)
def test_comparison_source_evidence_and_financial_invariants_are_constructed_before_parsing(mode):
    pair = generate_pair(257, seed=2026, workload="comparison")
    truth = pair.expected(Decimal("0.01"), mode=mode)
    for source, headers, text, expected_records, attribute in (
        ("A", COMPARISON_HEADERS_A, pair.csv_a, pair.records_a, "records_a"),
        ("B", COMPARISON_HEADERS_B, pair.csv_b, pair.records_b, "records_b"),
    ):
        parsed = list(csv.DictReader(StringIO(text, newline="")))
        assert [record.source_row for record in expected_records] == list(range(2, len(parsed) + 2))
        assert [record.raw_fields for record in expected_records] == parsed
        assert all(
            record.source == source and tuple(record.raw_fields) == headers
            for record in expected_records
        )
        assert all(
            record.key == tuple(record.raw_fields[field] for field in headers[:2])
            for record in expected_records
        )
        grouped_records = [record for group in truth.groups for record in getattr(group, attribute)]
        assert Counter(id(record) for record in grouped_records) == Counter(
            id(record) for record in expected_records
        )
        assert Counter((record.source, record.source_row) for record in grouped_records) == Counter(
            (record.source, record.source_row) for record in expected_records
        )
        assert all(
            tuple(record.source_row for record in getattr(group, attribute))
            == tuple(sorted(record.source_row for record in getattr(group, attribute)))
            for group in truth.groups
        )
    delta_units = sum(group.delta_units for group in truth.groups)
    assert (
        truth.total_a - truth.total_b
        == truth.control_difference
        == oracle.decimal_units(delta_units)
    )
    accepted_units = sum(
        group.delta_units for group in truth.groups if group.key in truth.tolerated_keys
    )
    assert truth.tolerated_delta_total == oracle.decimal_units(accepted_units)
    assert (
        sum(group.delta_units for group in truth.groups if group.is_exception) + accepted_units
        == delta_units
    )
    assert any(record.units < 0 for record in pair.records_a)
    assert any(record.units == 0 for record in pair.records_a)
    assert any("\n" in value for record in pair.records_a for value in record.raw_fields.values())
    assert any(
        value.startswith("=") for record in pair.records_a for value in record.raw_fields.values()
    )


@pytest.mark.parametrize("mode", oracle.MODES)
def test_physical_shuffle_changes_positions_but_not_comparison_or_money_truth(mode):
    first = generate_pair(300, seed=5, shuffle_seed=1, workload="comparison")
    second = generate_pair(300, seed=5, shuffle_seed=2, workload="comparison")
    assert first.scenarios == second.scenarios
    assert first.csv_a != second.csv_a and first.csv_b != second.csv_b
    for records_first, records_second in (
        (first.records_a, second.records_a),
        (first.records_b, second.records_b),
    ):
        assert Counter(tuple(record.raw_fields.items()) for record in records_first) == Counter(
            tuple(record.raw_fields.items()) for record in records_second
        )
        assert any(
            record_a.key != record_b.key
            for record_a, record_b in zip(records_first, records_second, strict=True)
        )
    truth_first = first.expected(Decimal("0.01"), mode=mode)
    truth_second = second.expected(Decimal("0.01"), mode=mode)
    assert truth_first.summary() == truth_second.summary()
    assert truth_first.exception_keys == truth_second.exception_keys
    assert truth_first.tolerated_keys == truth_second.tolerated_keys
    assert [
        (
            group.key,
            group.category,
            group.units_a,
            group.units_b,
            group.field_comparisons,
            group.is_exception,
        )
        for group in truth_first.groups
    ] == [
        (
            group.key,
            group.category,
            group.units_a,
            group.units_b,
            group.field_comparisons,
            group.is_exception,
        )
        for group in truth_second.groups
    ]


@pytest.mark.parametrize("index", [50, 55, 60, 64, 96, 98, 99])
def test_unique_missing_or_duplicate_groups_keep_observed_distinct_values_not_comparable(index):
    pair = generate_pair(100, workload="comparison")
    scenario = pair.scenarios[index]
    group = next(
        group for group in pair.expected(mode="unique").groups if group.key == scenario.key
    )
    assert all(field.status == "not_comparable" for field in group.field_comparisons)
    assert not group.has_secondary_mismatch and group.is_exception
    assert group.category == (
        "a_only" if index == 50 else "b_only" if index == 55 else "duplicate_ambiguous"
    )
    assert group.field_comparisons[0].values_a == tuple(
        sorted({values[0] for values in scenario.values_a})
    )
    assert group.field_comparisons[0].values_b == tuple(
        sorted({values[0] for values in scenario.values_b})
    )


@pytest.mark.parametrize(
    "tolerance, error",
    [
        (0, TypeError),
        ("0.01", TypeError),
        (Decimal("-0.01"), ValueError),
        (Decimal("NaN"), ValueError),
        (Decimal("Infinity"), ValueError),
    ],
)
def test_comparison_oracle_rejects_invalid_tolerance(tolerance, error):
    with pytest.raises(error):
        generate_pair(100, workload="comparison").expected(tolerance)


def test_comparison_oracle_rejects_unknown_mode():
    with pytest.raises(ValueError, match="mode"):
        generate_pair(100, workload="comparison").expected(mode="unknown")
