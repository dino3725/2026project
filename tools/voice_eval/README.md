# voice_eval — 음성 명령 분류기 평가

`voice_command_pkg.command_classifier`를 텍스트로 평가하는 스크립트와 기록입니다.
모두 로컬 Ollama만 사용하고 ROS 메시지는 발행하지 않습니다.

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/test_llm_pick_objects.py
```

| 파일 | 상태 |
| --- | --- |
| `test_llm_pick_objects.py` | **현재 회귀 테스트** (물체 4종 + stop + unknown). 노드와 같은 경로: 정지 규칙 → LLM |
| `test_llm_classifier.py`, `test_llm_dev.py`, `dev_cases_v1.json` | 이전 분류기(물체 구성이 다름) 개발 기록 |
| `test_llm_final_heldout.py`, `final_heldout_v2.json` | 분류기 v2 held-out, **이미 1회 실행 완료**. 다시 돌리지 않습니다 |
| `results/` | 평가 결과 기록 |

## 규칙

- 프롬프트(`src/voice_command_pkg/voice_command_pkg/prompts/system_prompt.txt`)를
  바꾸면 회귀 테스트를 다시 돌리고 결과를 `results/`에 남깁니다.
- held-out 평가는 분류기를 동결한 뒤 한 번만 실행합니다: 동결 → SHA256 기록 → 1회 실행.

## 기록

| 날짜 | 내용 | 결과 |
| --- | --- | --- |
| 2026-11-04 | ROS2 패키지화 후 확인 (프롬프트 SHA256 `f3c8d6bf…36be73`, 변경 없음) | `test_llm_pick_objects.py` 64/64 |
| 2026-11-04 | 정지를 규칙으로 판정, LLM에서 stop 제거 (프롬프트 SHA256 `b184e9bc…3ff8df6`) | 64/64, stop 17/17 규칙 판정 → `results/stop_rules_20261104.md` |
