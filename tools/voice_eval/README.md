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

## 녹음 기반 STT 개발 비교

`compare_stt_settings.py`는 16 kHz/mono/PCM16 WAV를 메모리로 읽고, 같은 `small` 모델에서
beam 5/1 × initial_prompt 없음/어휘 목록의 네 설정을 비교합니다. `hotwords`는 이번
비교에서는 비워 둡니다. 모델 워밍업은 측정에서 제외하고 설정 순서를 교대합니다.
27개 실제 발화와 1초·3초 합성 무음은 별도 집계하며, 2회 반복을 독립 54명/54발화로
해석하지 않습니다. 일반화 정확도를 위한 held-out이 아니라 설정 선택용 개발셋입니다.

```bash
cd ~/2026project
STT_REPORT_DIR=$(mktemp -d "$PWD/log/stt-recheck.XXXXXX")
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/compare_stt_settings.py tools/voice_eval/stt_dev_20261111.json \
  --rounds 2 --classify --output "$STT_REPORT_DIR/comparison.json"
```

녹음은 Git에 없으므로 해당 파일들이 있는 노트북에서 실행합니다. 다른 녹음은
`purpose: development`, `samples: [{file, reference, action, object, sha256}]` 형식의
새 JSON manifest로 지정합니다. `file`은 manifest 기준 상대 경로도 허용합니다.
SHA256이 있으면 원본과 대조하고, 누락·중복·잘못된 정답은 오류로 처리합니다.
`--classify`는 STT 측정 후 로컬 Ollama를 워밍업하고 전사문 정지 규칙 → 동결된 LLM으로
최종 명령을 확인합니다. 같은 전사문은 결과를 재사용하므로 LLM 지연 측정에는 쓰지 않습니다.
ROS 상태 게이트/명령 발행은 연결하지 않습니다. `--classify` 없이 STT만 비교할 수도 있습니다.

- `comparison.jsonl`: 추론 직후 저장하는 전사·시간·segment temperature 등의 원시 기록.
- `comparison.json`: 설정, 버전, 원본 해시, 집계와 선택적 최종 명령 결과.
- `inspect_vad_audio.py`: 저장된 WAV를 현재 VAD로 재생하여 30ms 프레임별 RMS·판정·전환 저장.
  각 WAV마다 VAD를 새로 생성하므로 실제 상시 스트림의 이전 상태와 결과가 다를 수 있습니다.
- `results/2026-11-11_stt_settings.md`: 27개 문장 전체 비교, 선택 근거, 한계와 사용자 확인 절차.

평가 도구 자체의 정답 대응·문자 오류율·집계 검증:

```bash
source voice_env.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tools/voice_eval/tests -q
```

재시도 비용을 분리하는 후속 비교는 같은 manifest에 다음 옵션을 사용합니다.
기존 Whisper 온도 목록(0.0, 0.2, …, 1.0)을 순회하는 설정과, 0.0에서 한 번만
디코딩하는 설정을 비교하며 LLM의 temperature는 변경하지 않습니다.

```bash
cd ~/2026project
STT_REPORT_DIR=$(mktemp -d "$PWD/log/stt-temperature.XXXXXX")
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/compare_stt_settings.py tools/voice_eval/stt_dev_20261111.json \
  --settings beam5_plain beam5_temperature0 --rounds 2 --classify \
  --output "$STT_REPORT_DIR/comparison.json"
```

힌트는 무음에서도 힌트 문장을 출력하게 만들 수 있습니다. 이번 개발 비교의 전체
어휘 힌트는 무음에서 정지 단어를 만들어 **기본값으로 채택하지 않았습니다**.
`hotwords`도 같은 문제가 확인됐습니다. 단순히 물체 이름의 전사가 개선됐다는 이유로
운영 힌트를 켜지 않습니다. 정확도와 무음 대조군은 항상 같이 확인합니다.

## 짧은 스톱의 VAD 누락 분석

연속 녹음의 시작 조건 후보는 `inspect_vad_audio.py --start-speech-ratio 0.4`로
비교합니다. `--start-min-rms`, `--vad-mode`도 지원하며 실제 노드 기본값을 변경하지 않습니다.
50초 녹음의 시작 누락과 별도 STT 오류, 실시간 dry-run 명령은
`results/2026-11-11_stop_probe.md`에 기록했습니다. 시작 9개와 정지 9개는 서로 다른 지표입니다.
## 오디오 정지 개발 평가 추가 (2026-11-12)

`download_audio_stop_model.py`는 고정 리비전의 모델 파일만 내려받고 SHA256을 검증합니다.
`evaluate_audio_stop.py`는 `audio_stop_dev_20261112.json`의 개발 WAV를 재생합니다.
`replay_audio_stop_pipeline.py`는 WAV를 실시간 속도로 한 파이프에 전달하여 실제
VAD/Whisper/KWS를 확인합니다. ROS 발행은 로컬 기록 콜백으로 대체하고 LLM 요청은 막습니다.
이 도구의 active 모드는 실제 로봇으로 발행하지 않습니다.

모델 후보는 기본 off이며, 운영 채택 전입니다. 직접 파일 재생과 초기 안정화 구간을
버리는 운영 조건의 감지 수가 다르므로 수치를 섞지 마세요. 결과·명령·한계는
`results/2026-11-12_audio_stop.md` 및 `src/voice_command_pkg/README.md`를 참고하세요.
기존 held-out 평가셋을 이 개발 평가에 사용하지 않습니다.
