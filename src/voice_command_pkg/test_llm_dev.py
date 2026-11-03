#!/usr/bin/env python3
"""Validate or execute the v1 development/diagnostic text dataset."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from llm_test import classify_command


DATA_PATH = Path(__file__).with_name("dev_cases_v1.json")
CLASSES = ("coke", "tissue", "snack", "unknown")
EXPECTED_COUNTS = {name: 25 for name in CLASSES}
EXPECTED_HARD_COUNT = 20


def load_cases() -> dict[str, list[dict[str, str]]]:
    with DATA_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if set(data) != {"heldout", "hard"}:
        raise ValueError("dataset must contain exactly 'heldout' and 'hard' suites")
    return data


def validate_cases(data: dict[str, list[dict[str, str]]]) -> None:
    basic = data["heldout"]
    hard = data["hard"]
    counts = Counter(case["expected"] for case in basic)
    if counts != Counter(EXPECTED_COUNTS):
        raise ValueError(f"unexpected held-out class counts: {dict(counts)}")
    if len(hard) != EXPECTED_HARD_COUNT:
        raise ValueError(f"hard suite must have {EXPECTED_HARD_COUNT} cases")

    all_cases = basic + hard
    ids = [case["id"] for case in all_cases]
    texts = [case["text"].strip() for case in all_cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case id detected")
    if len(texts) != len(set(texts)):
        raise ValueError("duplicate test text detected")
    for case in all_cases:
        if case["expected"] not in CLASSES:
            raise ValueError(f"unsupported expected class: {case}")
        if not case["text"].strip():
            raise ValueError(f"empty test text: {case['id']}")


def expected_result(label: str) -> dict[str, str]:
    if label == "unknown":
        return {"action": "unknown", "object": ""}
    return {"action": "fetch", "object": label}


def actual_class(result: dict[str, str] | None) -> str:
    if result is None:
        return "error"
    return "unknown" if result.get("object") == "" else result.get("object", "error")


def run_suite(name: str, cases: list[dict[str, str]]) -> int:
    totals: Counter[str] = Counter()
    passes: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    timings: list[float] = []
    failures = 0

    for index, case in enumerate(cases, 1):
        expected = expected_result(case["expected"])
        started = time.perf_counter()
        error = None
        try:
            actual: dict[str, str] | None = classify_command(case["text"])
        except Exception as exc:
            actual = None
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        passed = actual == expected
        timings.append(elapsed)
        totals[case["expected"]] += 1
        passes[case["expected"]] += int(passed)
        confusion[case["expected"]][actual_class(actual)] += 1
        failures += int(not passed)
        record: dict[str, Any] = {
            "suite": name,
            "index": index,
            "id": case["id"],
            "input": case["text"],
            "expected": expected,
            "actual": actual,
            "pass": passed,
            "elapsed_seconds": round(elapsed, 3),
            "error": error,
        }
        if "type" in case:
            record["type"] = case["type"]
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)

    print(f"\n[SUMMARY:{name}]")
    for label in CLASSES:
        total = totals[label]
        accuracy = passes[label] / total if total else 0.0
        print(f"class.{label}={passes[label]}/{total} ({accuracy:.1%})")
    accuracy = (len(cases) - failures) / len(cases) if cases else 0.0
    print(f"overall={len(cases) - failures}/{len(cases)} ({accuracy:.1%})")
    print(f"latency_mean_seconds={statistics.mean(timings):.3f}")
    print(f"latency_min_seconds={min(timings):.3f}")
    print(f"latency_max_seconds={max(timings):.3f}")
    print("confusion=" + json.dumps(confusion, ensure_ascii=False))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="call the Local LLM; without this flag only validate the dev dataset",
    )
    parser.add_argument(
        "--suite",
        choices=("all", "heldout", "hard"),
        default="all",
    )
    args = parser.parse_args()

    data = load_cases()
    validate_cases(data)
    print(
        "dataset_valid: "
        f"dev={len(data['heldout'])}, hard={len(data['hard'])}, "
        f"total={len(data['heldout']) + len(data['hard'])}"
    )
    if not args.run:
        print("LLM execution skipped; pass --run to execute development tests.")
        return 0

    selected = ("heldout", "hard") if args.suite == "all" else (args.suite,)
    failures = sum(run_suite(name, data[name]) for name in selected)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
