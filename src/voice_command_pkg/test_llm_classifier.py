#!/usr/bin/env python3
"""Text-only regression and generalization tests for the local classifier."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from llm_test import classify_command


@dataclass(frozen=True)
class Case:
    suite: str
    expected: str
    text: str


BASE_CASES = {
    "coke": [
        "콜라 가져다줘",
        "목마른데 마실 거 가져다줘",
        "목이 너무 말라",
        "음료 하나 가져와",
        "마실 게 필요한데",
        "뭐 좀 마시고 싶어",
        "갈증 나는데 뭐 마실 거 없어?",
        "입이 마르는데 음료 좀 가져와",
        "시원한 거 마시고 싶은데",
        "마실 것 좀 부탁해",
        "목이 타는데 뭐 좀 가져다줘",
        "목마름대 마실거 가져다줘",
    ],
    "tissue": [
        "휴지 가져다줘",
        "뭐 흘렸는데 닦을 거 가져다줘",
        "바닥에 물 흘렸어",
        "닦을 게 필요해",
        "휴지 좀 갖다줘",
        "커피 쏟았어",
        "책상 좀 닦아야겠다",
        "닦을 만한 거 가져와",
        "바닥에 뭐 흘렸는데",
        "이거 닦을 게 필요해",
        "물을 흘렸으니까 닦을 거 가져와",
        "콜라를 흘렸는데 닦을 거 가져다줘",
    ],
    "snack": [
        "간식 가져다줘",
        "과자 가져다줘",
        "배고픈데 먹을 거 가져다줘",
        "배고파",
        "먹을 게 필요해",
        "뭐 좀 먹고 싶어",
        "출출한데 뭐 먹을 거 없어?",
        "허기지는데 간식 좀 줘",
        "간단하게 먹을 거 하나 가져와",
        "배고프니까 간식 좀 가져다줘",
        "먹을 만한 거 가져와",
        "배고픈데 콜라 말고 먹을 거 가져다줘",
    ],
    "unknown": [
        "창문 열어줘",
        "오늘 날씨 어때",
        "불 꺼줘",
        "로봇 멈춰",
        "앞으로 가",
        "노래 틀어줘",
        "안녕하세요",
        "좀 찝찝한데 정리할 만한 거 없어?",
        "배고프고 목도 마른데",
        "그거 가져다줘",
        "뭔가 필요해",
        "심심해",
    ],
}

GENERALIZATION_CASES = {
    "coke": [
        "갈증 나 죽겠네",
        "목 축일 음료가 있었으면 좋겠어",
        "입안이 바짝 말랐어",
        "시원한 음료 한 잔 부탁할게",
        "물 대신 콜라로 목 좀 축이고 싶다",
        "마실 만한 걸 챙겨 줄래?",
        "갈증을 해소할 게 필요해",
        "목이 칼칼해서 음료가 당겨",
        "한 모금 마실 것이 있었으면 해",
        "음료수 하나만 부탁할 수 있을까?",
    ],
    "tissue": [
        "바닥에 음료가 엎질러졌네",
        "책상에 물이 번졌어",
        "손에 묻은 걸 닦고 싶어",
        "주스를 쏟아서 닦아야 해",
        "테이블이 젖었으니 닦을 것을 줘",
        "흘린 국물을 닦을 게 필요하다",
        "여기 얼룩을 훔칠 휴지 좀 줘",
        "물기가 생겼는데 닦을 도구가 없어",
        "컵을 엎어서 바닥이 젖었어",
        "코를 풀 휴지가 필요해",
    ],
    "snack": [
        "출출해서 뭐라도 먹고 싶네",
        "허기를 달랠 것이 필요해",
        "간단히 요기할 걸 챙겨줘",
        "배에서 꼬르륵 소리가 나",
        "입이 심심하니 과자 좀 줘",
        "끼니 전까지 먹을 간단한 걸 부탁해",
        "에너지가 떨어져서 간식이 당겨",
        "한입거리 좀 가져올래?",
        "허기를 채울 음식이 있었으면 좋겠어",
        "가볍게 집어먹을 것을 줘",
    ],
    "unknown": [
        "방 온도가 몇 도야?",
        "문을 닫아 줄래?",
        "지금 몇 시인지 알려줘",
        "내일 일정이 뭐였지?",
        "조명을 더 밝게 해줘",
        "저쪽으로 돌아서 가",
        "재미있는 이야기 해줘",
        "이 물건을 어디에 둘까?",
        "잠깐 기다려 줘",
        "뭔가 가져오면 좋겠는데",
    ],
}

EDGE_CASES = {
    "coke": ["콜라 마시고 싶어", "휴지는 필요 없고 목말라"],
    "tissue": ["콜라 쏟았어", "뭐 흘린 건 아닌데 휴지 가져다줘"],
    "snack": ["콜라는 필요 없고 배고파"],
    "unknown": ["과자 흘렸어", "배고픈데 물 마시고 싶어"],
}

REPEAT_CASES = {
    "coke": "목이 너무 말라",
    "tissue": "커피 쏟았어",
    "snack": "배고파",
    "unknown": "배고프고 목도 마른데",
}


def make_cases() -> list[Case]:
    cases: list[Case] = []
    for suite, groups in (
        ("base", BASE_CASES),
        ("generalization", GENERALIZATION_CASES),
        ("edge", EDGE_CASES),
    ):
        for expected, texts in groups.items():
            cases.extend(Case(suite, expected, text) for text in texts)
    for expected, text in REPEAT_CASES.items():
        cases.extend(Case("repeat", expected, text) for _ in range(3))
    return cases


def expected_result(obj: str) -> dict[str, str]:
    if obj == "unknown":
        return {"action": "unknown", "object": ""}
    return {"action": "fetch", "object": obj}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("all", "base", "generalization", "edge", "repeat"),
        default="all",
    )
    args = parser.parse_args()
    cases = make_cases()
    if args.suite != "all":
        cases = [case for case in cases if case.suite == args.suite]

    timings: list[float] = []
    suite_totals: Counter[str] = Counter()
    suite_passes: Counter[str] = Counter()
    class_totals: Counter[str] = Counter()
    class_passes: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    failures = 0

    for index, case in enumerate(cases, 1):
        expected = expected_result(case.expected)
        started = time.perf_counter()
        error = ""
        try:
            actual = classify_command(case.text)
        except Exception as exc:
            actual = None
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        passed = actual == expected
        timings.append(elapsed)
        suite_totals[case.suite] += 1
        class_totals[case.expected] += 1
        if passed:
            suite_passes[case.suite] += 1
            class_passes[case.expected] += 1
        else:
            failures += 1
        if actual is None:
            actual_class = "error"
        elif actual["object"] == "":
            actual_class = "unknown"
        else:
            actual_class = actual["object"]
        confusion[case.expected][actual_class] += 1
        print(
            json.dumps(
                {
                    "index": index,
                    "suite": case.suite,
                    "input": case.text,
                    "expected": expected,
                    "actual": actual,
                    "pass": passed,
                    "elapsed_seconds": round(elapsed, 3),
                    "error": error or None,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )

    print("\n[SUMMARY]")
    for suite in ("base", "generalization", "edge", "repeat"):
        total = suite_totals[suite]
        if total:
            print(f"suite.{suite}={suite_passes[suite]}/{total}")
    for expected in ("coke", "tissue", "snack", "unknown"):
        total = class_totals[expected]
        print(f"class.{expected}={class_passes[expected]}/{total}")
    print(f"overall={len(cases)-failures}/{len(cases)}")
    print(f"cold_seconds={timings[0]:.3f}")
    warm = timings[1:] or timings
    print(f"warm_mean_seconds={statistics.mean(warm):.3f}")
    print(f"warm_min_seconds={min(warm):.3f}")
    print(f"warm_max_seconds={max(warm):.3f}")
    print("confusion=" + json.dumps(confusion, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
