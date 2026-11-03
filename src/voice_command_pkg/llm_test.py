#!/usr/bin/env python3
"""Classify Korean service-robot commands with a local Ollama model."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from typing import Any


OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3:4b-instruct"

SYSTEM_PROMPT = """당신은 서비스 로봇의 자연어 명령을 제한된 행동으로 변환하는 명령 해석기다.
로봇이 가져올 수 있는 물체는 coke, vaseline, tissue, airpod뿐이다.

분류 규칙:
1. 로봇·플랫폼의 이동이나 주행을 멈추라는 명확한 명령은 가장 높은 우선순위의 `{"action":"stop","object":""}`이다. "로봇 멈춰", "멈춰", "정지해", "로봇 정지", "지금 멈춰", "당장 멈춰", "움직이지 마", "스톱", "stop", "작동 멈춰", "로봇 그만 움직여"가 이에 해당한다. 음악·재생·알람 같은 다른 기능을 멈추라는 말은 stop이 아니다.
2. 현재 문장 안에서 긍정적으로 필요한 지원 물체를 모두 식별한다. 두 개 이상이면 하나를 임의로 고르지 말고 unknown이다.
3. "그거", "그 물건", "아까 말한 것"처럼 현재 문장만으로 대상을 식별할 수 없는 참조는 반드시 unknown이다.
4. "말고", "필요 없다", "사양하다", "가져오지 마"처럼 부정되거나 제외된 물체는 긍정 의도로 세지 않는다. 남은 명확한 의도가 하나면 그것을 선택한다. 반면 "바를 거 없어?"처럼 필요한 물건이 있는지 묻는 표현은 거절이 아니라 요청이다.
5. coke: 콜라·음료를 요청하거나, 목·입안의 갈증이나 건조함 때문에 마실 것이 필요한 상황이다. "목을 축이다"는 마실 것을 필요로 한다는 뜻이므로 coke다. 입술이나 피부가 건조한 것은 coke가 아니다.
6. vaseline: 한국어 "바세린"은 출력 object `vaseline`에 정확히 대응한다. "바세린 통"이나 "바세린 튜브"도 vaseline이다. 바세린·보습제를 요청하거나, 입술·손·피부가 트고 갈라지거나 건조해서 바를 것이 필요한 상황이다. 목이나 입안이 마른 것은 vaseline이 아니다.
7. tissue: 휴지나 닦을 것을 요청하거나, 책상·바닥·손·물건에 액체·소스·얼룩이 묻어 닦아야 하는 상황이다. 음료 이름은 마시려는 의도가 아니라 흘린 대상일 수 있다.
8. airpod: 에어팟·무선 이어폰·귀에 꽂을 청취 도구를 요청하거나 통화·음악 청취를 위해 이어폰이 필요한 상황이다. 단순히 음악을 재생하거나 볼륨을 조절하라는 기기 제어는 airpod가 아니다.
9. 명시적으로 콜라, 바세린, 휴지, 에어팟·블루투스 이어폰 중 하나를 요청하면 부정이나 다중 의도가 없는 한 반드시 대응하는 fetch로 판정한다. 직접 "가져와"라고 하지 않은 상태 서술이나 공손한 질문도 필요 물체가 명확하면 fetch다. 코를 풀려는 상황은 tissue이고, 갈라진 피부에 바를 것이 필요한 상황은 vaseline이다.
10. 음식, 리모컨, 생수, 약처럼 지원하지 않는 물체, 정보 질문, 인사, 로봇 정지 이외의 기기 제어, 불명확한 요청은 unknown이다. 지원 물체를 다른 물체로 대체하지 않는다.

사용자: 목마른데 마실 거 가져다줘
결과: {"action":"fetch","object":"coke"}
사용자: 뭐 흘렸는데 닦을 거 가져다줘
결과: {"action":"fetch","object":"tissue"}
사용자: 입술이 터서 바를 게 필요해
결과: {"action":"fetch","object":"vaseline"}
사용자: 바세린 가져다줘
결과: {"action":"fetch","object":"vaseline"}
사용자: 바세린 통 좀 줘
결과: {"action":"fetch","object":"vaseline"}
사용자: 손이 너무 건조한데 바세린 가져다줘
결과: {"action":"fetch","object":"vaseline"}
사용자: 손가락 피부가 갈라져서 바를 게 필요해
결과: {"action":"fetch","object":"vaseline"}
사용자: 코 풀 휴지가 필요해
결과: {"action":"fetch","object":"tissue"}
사용자: 음악 들을 때 쓸 무선 이어폰 가져다줘
결과: {"action":"fetch","object":"airpod"}
사용자: 에어팟이 필요해
결과: {"action":"fetch","object":"airpod"}
사용자: 내 블루투스 이어폰을 찾아줘
결과: {"action":"fetch","object":"airpod"}
사용자: 로봇 당장 멈춰
결과: {"action":"stop","object":""}
사용자: 더 이상 움직이지 마
결과: {"action":"stop","object":""}
사용자: 정지해
결과: {"action":"stop","object":""}
사용자: 스톱
결과: {"action":"stop","object":""}
사용자: 오늘 날씨 어때
결과: {"action":"unknown","object":""}
사용자: 콜라랑 휴지 둘 다 가져다줘
결과: {"action":"unknown","object":""}
사용자: 노래 틀어줘
결과: {"action":"unknown","object":""}
사용자: 음악 재생을 멈춰
결과: {"action":"unknown","object":""}
사용자: 전에 말한 물건을 가져와
결과: {"action":"unknown","object":""}
사용자: 입술도 텄고 목도 말라
결과: {"action":"unknown","object":""}

설명, markdown, 인사말, reasoning 또는 추가 문장 없이 JSON 객체만 출력하라."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["fetch", "stop", "unknown"]},
        "object": {
            "type": "string",
            "enum": ["coke", "vaseline", "tissue", "airpod", ""],
        },
    },
    "required": ["action", "object"],
    "additionalProperties": False,
    "oneOf": [
        {
            "properties": {
                "action": {"const": "fetch"},
                "object": {"const": "coke"},
            }
        },
        {
            "properties": {
                "action": {"const": "fetch"},
                "object": {"const": "vaseline"},
            }
        },
        {
            "properties": {
                "action": {"const": "fetch"},
                "object": {"const": "tissue"},
            }
        },
        {
            "properties": {
                "action": {"const": "fetch"},
                "object": {"const": "airpod"},
            }
        },
        {
            "properties": {
                "action": {"const": "stop"},
                "object": {"const": ""},
            }
        },
        {
            "properties": {
                "action": {"const": "unknown"},
                "object": {"const": ""},
            }
        },
    ],
}

ALLOWED_RESULTS = {
    ("fetch", "coke"),
    ("fetch", "vaseline"),
    ("fetch", "tissue"),
    ("fetch", "airpod"),
    ("stop", ""),
    ("unknown", ""),
}


def _validate_result(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"action", "object"}:
        raise ValueError(f"Invalid result structure: {value!r}")

    action = value.get("action")
    obj = value.get("object")
    if not isinstance(action, str) or not isinstance(obj, str):
        raise ValueError(f"Result values must be strings: {value!r}")
    if (action, obj) not in ALLOWED_RESULTS:
        raise ValueError(f"Invalid action/object combination: {value!r}")
    return {"action": action, "object": obj}


def classify_command_with_metrics(text: str) -> tuple[dict[str, str], dict[str, float]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text.strip()},
        ],
        "stream": False,
        "think": False,
        "format": OUTPUT_SCHEMA,
        "keep_alive": "10m",
        "options": {
            "temperature": 0,
            "seed": 0,
            "num_ctx": 2048,
            "num_predict": 32,
        },
    }
    request = urllib.request.Request(
        OLLAMA_CHAT_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=120) as response:
        api_result = json.load(response)
    wall_seconds = time.perf_counter() - started

    content = api_result["message"]["content"]
    result = _validate_result(json.loads(content))
    metrics = {
        "wall_seconds": wall_seconds,
        "total_seconds": api_result.get("total_duration", 0) / 1_000_000_000,
        "load_seconds": api_result.get("load_duration", 0) / 1_000_000_000,
        "prompt_eval_seconds": api_result.get("prompt_eval_duration", 0)
        / 1_000_000_000,
        "eval_seconds": api_result.get("eval_duration", 0) / 1_000_000_000,
    }
    return result, metrics


def classify_command(text: str) -> dict[str, str]:
    result, _ = classify_command_with_metrics(text)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help="Natural-language command to classify")
    args = parser.parse_args()
    print(json.dumps(classify_command(args.text), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
