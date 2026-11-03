#!/usr/bin/env python3
"""Text-only LLM tests for fetch objects, stop, and unknown."""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict

from llm_test import classify_command


CASES = {
    "coke": [
        "콜라 가져다줘",
        "콜라 한 캔만 가져와 줘",
        "갈증 나니까 마실 음료가 필요해",
        "입안이 메말라서 한 모금 마시고 싶어",
        "시원한 탄산음료 하나 부탁해",
        "목을 축일 만한 걸 갖다줄래",
        "오래 말했더니 목이 바싹 말랐어",
        "지금 마실 게 있었으면 좋겠다",
        "목이 타는데 콜라 좀 줘",
    ],
    "vaseline": [
        "바세린 가져다줘",
        "바세린 통을 가져다줘",
        "입술이 갈라져서 바를 게 필요해",
        "손등 피부가 거칠어서 보습하고 싶어",
        "피부가 너무 건조하니 보습제 좀 줘",
        "튼 입술에 바를 것을 부탁해",
        "팔꿈치가 건조해서 바세린이 필요하다",
        "손가락 사이 피부가 건조하게 갈라졌는데 보습할 게 없어",
        "메마른 피부에 쓸 연고 같은 보습제를 가져와",
    ],
    "tissue": [
        "휴지 가져다줘",
        "휴지 몇 장 가져다줘",
        "커피를 엎질러서 닦아야 해",
        "바닥에 물이 흥건하니 닦을 걸 줘",
        "손에 소스가 묻어서 닦고 싶어",
        "책상 얼룩을 지울 휴지가 필요해",
        "코를 풀 수 있게 휴지 좀 부탁해",
        "쏟은 물기를 흡수할 것을 가져와",
        "테이블이 끈적하니 닦을 걸 갖다줘",
    ],
    "airpod": [
        "에어팟 가져다줘",
        "에어팟을 가져다줘",
        "무선 이어폰이 필요해",
        "노래를 들을 수 있게 이어폰 좀 줘",
        "통화할 때 쓸 에어팟을 갖다줘",
        "귀에 꽂을 청취 장치를 가져와",
        "내 블루투스 이어폰을 찾아서 줘",
        "출근길에 음악 들을 이어폰이 필요하다",
        "소리 들을 때 쓸 에어팟 하나 부탁해",
    ],
    "stop": [
        "로봇 멈춰",
        "멈춰",
        "정지해",
        "로봇 정지",
        "지금 당장 멈춰",
        "움직이지 마",
        "스톱",
        "stop",
        "작동 멈춰",
        "로봇 그만 움직여",
        "플랫폼을 지금 정지해",
        "당장 이동을 중단해 줘",
        "거기 그대로 서",
        "더 이상 움직이지 마",
        "주행을 멈춰줘",
        "비상 정지해",
        "앞으로 가지 말고 멈춰",
    ],
    "unknown": [
        "오늘 날씨 어때",
        "안녕",
        "노래 틀어줘",
        "배고프니까 과자 가져다줘",
        "오늘 오후 날씨가 어때",
        "텔레비전 리모컨을 가져와",
        "신나는 노래를 틀어줘",
        "아까 봤던 그거 가져다줘",
        "콜라와 바세린을 둘 다 가져와",
        "입술도 텄고 목도 말라서 둘 다 해결해줘",
        "에어팟도 휴지도 필요 없으니 가져오지 마",
    ],
}


def expected_result(label: str) -> dict[str, str]:
    if label == "stop":
        return {"action": "stop", "object": ""}
    if label == "unknown":
        return {"action": "unknown", "object": ""}
    return {"action": "fetch", "object": label}


def main() -> int:
    totals: Counter[str] = Counter()
    passes: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    timings: list[float] = []
    failures = 0

    for expected_class, texts in CASES.items():
        for text in texts:
            started = time.perf_counter()
            error = None
            try:
                actual = classify_command(text)
            except Exception as exc:
                actual = None
                error = f"{type(exc).__name__}: {exc}"
            elapsed = time.perf_counter() - started
            expected = expected_result(expected_class)
            passed = actual == expected
            actual_class = (
                "error"
                if actual is None
                else actual["action"]
                if actual["object"] == ""
                else actual["object"]
            )
            totals[expected_class] += 1
            passes[expected_class] += int(passed)
            confusion[expected_class][actual_class] += 1
            failures += int(not passed)
            timings.append(elapsed)
            print(
                json.dumps(
                    {
                        "input": text,
                        "expected": expected,
                        "actual": actual,
                        "pass": passed,
                        "elapsed_seconds": round(elapsed, 3),
                        "error": error,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )

    print("\n[SUMMARY]")
    for label in ("coke", "vaseline", "tissue", "airpod", "stop", "unknown"):
        print(f"class.{label}={passes[label]}/{totals[label]}")
    print(f"overall={sum(passes.values())}/{sum(totals.values())}")
    print(f"first_seconds={timings[0]:.3f}")
    print(f"warm_mean_seconds={statistics.mean(timings[1:]):.3f}")
    print(f"warm_min_seconds={min(timings[1:]):.3f}")
    print(f"warm_max_seconds={max(timings[1:]):.3f}")
    print("confusion=" + json.dumps(confusion, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
