#!/usr/bin/env python3
"""Validate or execute the single-use classifier v2 final held-out set."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from llm_test import classify_command


ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "final_heldout_v2.json"
CLASSIFIER_PATH = ROOT / "llm_test.py"
CLASSES = ("coke", "tissue", "snack", "unknown")
EXPECTED_COUNTS = {
    "base": {label: 30 for label in CLASSES},
    "hard": {label: 10 for label in CLASSES},
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_and_validate() -> dict[str, list[dict[str, str]]]:
    with DATA_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if set(data) != {"base", "hard"}:
        raise ValueError("dataset must contain exactly base and hard suites")

    ids: list[str] = []
    texts: list[str] = []
    for suite, expected_counts in EXPECTED_COUNTS.items():
        cases = data[suite]
        counts = Counter(case.get("expected") for case in cases)
        if counts != Counter(expected_counts):
            raise ValueError(f"{suite} class counts are invalid: {dict(counts)}")
        for case in cases:
            required = {"id", "expected", "type", "text"}
            if set(case) != required:
                raise ValueError(f"invalid fields in {case.get('id', '<unknown>')}")
            if not case["text"].strip():
                raise ValueError(f"empty text in {case['id']}")
            ids.append(case["id"])
            texts.append(case["text"].strip())
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case id detected")
    if len(texts) != len(set(texts)):
        raise ValueError("duplicate case text detected")
    return data


def expected_result(label: str) -> dict[str, str]:
    if label == "unknown":
        return {"action": "unknown", "object": ""}
    return {"action": "fetch", "object": label}


def result_class(result: dict[str, str] | None) -> str:
    if result is None:
        return "error"
    return "unknown" if result.get("object") == "" else result.get("object", "error")


def print_summary(
    name: str,
    records: list[dict[str, Any]],
) -> None:
    totals = Counter(record["expected_class"] for record in records)
    passes = Counter(
        record["expected_class"] for record in records if record["pass"]
    )
    timings = [record["elapsed_seconds"] for record in records]
    print(f"\n[SUMMARY:{name}]")
    for label in CLASSES:
        total = totals[label]
        print(f"class.{label}={passes[label]}/{total} ({passes[label] / total:.1%})")
    passed = sum(record["pass"] for record in records)
    print(f"overall={passed}/{len(records)} ({passed / len(records):.1%})")
    print(f"latency_mean_seconds={statistics.mean(timings):.3f}")


def execute(data: dict[str, list[dict[str, str]]]) -> int:
    records: list[dict[str, Any]] = []
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for suite in ("base", "hard"):
        for case in data[suite]:
            expected = expected_result(case["expected"])
            started = time.perf_counter()
            error = None
            try:
                actual: dict[str, str] | None = classify_command(case["text"])
            except Exception as exc:
                actual = None
                error = f"{type(exc).__name__}: {exc}"
            elapsed = time.perf_counter() - started
            record: dict[str, Any] = {
                "suite": suite,
                "id": case["id"],
                "type": case["type"],
                "input": case["text"],
                "expected_class": case["expected"],
                "expected": expected,
                "actual": actual,
                "pass": actual == expected,
                "elapsed_seconds": round(elapsed, 3),
                "error": error,
            }
            records.append(record)
            confusion[case["expected"]][result_class(actual)] += 1
            print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)

    base_records = [record for record in records if record["suite"] == "base"]
    hard_records = [record for record in records if record["suite"] == "hard"]
    print_summary("base", base_records)
    print_summary("hard", hard_records)
    print_summary("all", records)
    timings = [record["elapsed_seconds"] for record in records]
    warm = timings[1:]
    print(f"first_request_seconds={timings[0]:.3f}")
    print(f"warm_mean_seconds={statistics.mean(warm):.3f}")
    print(f"warm_min_seconds={min(warm):.3f}")
    print(f"warm_max_seconds={max(warm):.3f}")
    print("confusion=" + json.dumps(confusion, ensure_ascii=False))
    failures = [record for record in records if not record["pass"]]
    print(f"failures={len(failures)}")
    print(f"classifier_sha256_after={file_sha256(CLASSIFIER_PATH)}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="execute the final Local LLM evaluation; omit to validate only",
    )
    args = parser.parse_args()
    data = load_and_validate()
    print(
        f"dataset_valid: base={len(data['base'])}, hard={len(data['hard'])}, "
        f"total={len(data['base']) + len(data['hard'])}"
    )
    print(f"classifier_sha256_before={file_sha256(CLASSIFIER_PATH)}")
    if not args.run_once:
        print("Final LLM evaluation skipped; pass --run-once to execute it.")
        return 0
    return execute(data)


if __name__ == "__main__":
    raise SystemExit(main())
