# 2026-11-12 오디오 정지 안정화 1차 — FP32 비교와 시작 위치 평가

## 결정과 범위

같은 한국어 모델의 FP32 버전을 실험 기본 정밀도로 선택했습니다. 감지 모드는 계속 **off**입니다. 분리 발화에서 정지 미검출이 줄고, 운영 조건의 50초 파일 재생에서는 기존 “슈톡” 누락을 오디오 경로가 보완했습니다. 하지만 연속 입력의 시작 위치에 따른 미탐은 남아 있어 안정화 전체 완료나 실물 정지 성능 달성으로 간주하지 않습니다.

코드 변경: 정밀도별 모델 선택/해시 검증/다운로드, 선택값 로그 표시, INT8 원복 옵션, 실패 사례를 포함한 새 개발 manifest, 시작 위치별 재현·집계 도구를 구현했습니다. 마이크 스트림, VAD, Whisper, LLM 프롬프트, stop 우선순위, 정지 후 fetch 차단과 Jetson 코드는 유지했습니다.

## 1. 실패 사례 재현

- 입력 목록: `tools/voice_eval/audio_stop_stability_dev_20261112.json`.
- 기존 분리 WAV 49개에 최근 `audio_stop_check.YgZ9jI`의 12개를 추가했습니다.
- 분리 파일 61개 = 대상 정지 31개 + 비정지 29개 + 범위 밖 정지 “그만 움직여” 1개.
- 별도로 연속 50초의 정지 9회를 사용했습니다. 원본 SHA256을 모두 검증했습니다.
- 모든 자료는 개발/튜닝 자료입니다. 새로운 화자의 일반화 성능이나 held-out 평가가 아닙니다.
- 앞 무음 0/60/120/180/240/300ms를 붙여 반복했습니다. 연속 파일만 운영과 같이 처음 1200ms를 제거하며, 결과 시각은 원본 음성 좌표로 되돌려 채점합니다.
- 각 파일 끝 합성 무음 600ms를 명시합니다. 앞 합성 무음에서 나온 검출을 정답으로 세지 않고, 한 검출을 여러 정지에 배정하지 않습니다.
- 파일 시작 위치를 바꾼 반복은 같은 녹음의 변형입니다. 186회 = 31개 × 6이며 서로 독립적인 186개 발화를 뜻하지 않습니다.

## 2. 후보 비교와 채택 이유

동일 INT8 모델에서 탐색 경로 수 4→16, 동일 정지 단어의 토큰 표현 추가는 분리 발화 검출 23/31을 개선하지 못했습니다. 입력 이득 2배/4배는 각각 29/31·30/31로 개선됐지만 연속 위치 문제를 해결하지 못했고 입력 파형 변경이 필요해 채택하지 않았습니다. FP32는 입력 파형/단어/임계값을 유지하고 31/31이어서 이번 단계의 변경으로 선택했습니다.

| 설정 | 분리 정지 /31, 앞 무음 0ms | 비정지 오탐 /29 | 연속 6위치의 정지 검출 수 /9 |
|---|---:|---:|---|
| baseline | 23 | 0 | 3, 5, 6, 6, 4, 5 |
| beam16 | 23 | 0 | 3, 5, 6, 6, 4, 5 |
| token_variants | 23 | 0 | 3, 4, 6, 6, 4, 5 |
| variants_beam16 | 23 | 0 | 3, 4, 6, 6, 4, 5 |
| fp32 | 31 | 0 | 5, 6, 5, 7, 8, 8 |
| gain4 | 30 | 0 | 5, 5, 6, 7, 7, 5 |
| gain2 | 29 | 0 | 5, 6, 5, 7, 7, 5 |

같은 가중치의 정밀도별 파일과 동일 입력을 비교한 결과입니다. 이번 자료에서 INT8 수치 차이가 미검출에 영향을 준다는 근거지만, 정밀도만 바꾸면 모든 환경에서 해결된다는 뜻은 아닙니다.
추가 탐색으로 FP32 4개 위상을 병렬 재생했을 때 해당 연속 파일의 9개 발화를 포괄했지만 검출 이벤트는 24개로 중복됐습니다. 이번에는 병렬 위상 처리를 운영 코드에 넣지 않았습니다. 중복 정지와 새로운 정지를 구분하는 검증 및 추가 CPU 비용 평가가 선행돼야 합니다.

## 3. 최종 코드의 위치별 평가

| 지표 | 기존 INT8 | 선택 FP32 |
|---|---:|---:|
| 앞 무음 0ms의 분리 정지 | 23/31 | 31/31 |
| 6위치 모두 감지한 분리 파일 | 23/31 | 30/31 |
| 분리 정지 반복 검출 | 165/186 | 185/186 |
| 비정지에서 검출이 나온 반복 | 0/174 | 0/174 |
| 정지 반복의 추가 검출 | 0 | 0 |
| 합성 무음 10초 | 0 | 0 |
| 합성 가우시안 잡음 10초 × RMS 100/500/2000 | 모두 0 | 모두 0 |

합성 잡음은 실제 생활 소음이 아니며, 이 결과로 시간당 실환경 오탐률을 주장하지 않습니다. FP32의 유일한 분리 발화 실패는 `shadow_03`(세 번째 스톱)의 앞 무음 60ms 조건입니다. 나머지 위치 5개에서는 이 발화도 감지했습니다.

| 연속 파일 앞 무음 | INT8 /9 | FP32 /9 |
|---:|---:|---:|
| 0ms | 3 | 5 |
| 60ms | 5 | 6 |
| 120ms | 6 | 5 |
| 180ms | 6 | 7 |
| 240ms | 4 | 8 |
| 300ms | 5 | 8 |

연속 파일에서는 기본 운영 조건(앞 무음 0ms) 3/9→5/9로 개선됐지만, 120ms 조건은 6/9→5/9로 감소했습니다. 따라서 모든 조건에서 단조롭게 개선된 것으로 해석하지 않습니다. 두 정밀도 모두 연속 파일의 채점 창 밖 추가 검출은 0건입니다.

### 분리 정지 파일별 6위치 감지 횟수

| ID | 정답 문장 | INT8 /6 | FP32 /6 |
|---|---|---:|---:|
| stt_13 | 멈춰 | 6 | 6 |
| stt_14 | 정지 | 6 | 6 |
| stt_15 | 스톱 | 4 | 6 |
| stt_16 | 지금 멈춰 | 6 | 6 |
| stt_17 | 로봇 정지 | 6 | 6 |
| stt_18 | 잠깐 스톱 | 6 | 6 |
| stt_19 | 콜라 가져오지 말고 멈춰 | 6 | 6 |
| stt_20 | 휴지 가져다줘, 아니 멈춰 | 6 | 6 |
| vad_check_01 | 스톱 | 6 | 6 |
| vad_check_02 | 스톱 | 3 | 6 |
| vad_check_03 | 스톱 | 6 | 6 |
| vad_check_04 | 멈춰 | 6 | 6 |
| vad_check_05 | 멈춰 | 6 | 6 |
| vad_check_06 | 멈춰 | 6 | 6 |
| vad_end600_01 | 스톱 | 1 | 6 |
| vad_end600_02 | 멈춰 | 6 | 6 |
| vad_end600_07 | 아니 멈춰 | 6 | 6 |
| vad_end750_01 | 휴지 가져다줘 아니 멈춰 | 4 | 6 |
| vad_end750_03 | 아니 멈춰 | 6 | 6 |
| vad_end750_04 | 휴지 가져다줘 아니 멈춰 | 6 | 6 |
| vad_end750_06 | 스톱 | 5 | 6 |
| vad_end750_07 | 멈춰 | 6 | 6 |
| shadow_01 | 스톱 | 3 | 6 |
| shadow_02 | 스톱 | 4 | 6 |
| shadow_03 | 스톱 | 3 | 5 |
| shadow_04 | 멈춰 | 6 | 6 |
| shadow_05 | 멈춰 | 6 | 6 |
| shadow_06 | 멈춰 | 6 | 6 |
| shadow_07 | 정지 | 6 | 6 |
| shadow_08 | 정지 | 6 | 6 |
| shadow_09 | 정지 | 6 | 6 |

## 4. 실제 코드의 VAD/Whisper/KWS 통합 재생

`replay_audio_stop_pipeline.py`로 원본 연속 50초를 실시간 속도로 PCM 파이프에 전달했습니다. 초기 안정화 1200ms, VAD 시작 0.4/종료 900ms, 실제 Whisper small과 실제 KWS를 사용했습니다. 마이크·ROS 발행자·Ollama 요청은 사용하지 않았고 발행 함수는 성공을 반환하는 로컬 기록 함수입니다.

| 발화 | 의도 | 오디오 감지 | 전사문 | 최종 로컬 stop 출처 |
|---:|---|---|---|---|
| 1 | 평소 스톱 | 없음 | 스톱 | 전사문 |
| 2 | 평소 스톱 | 없음 | 스톱 | 전사문 |
| 3 | 평소 스톱 | 스톱 | 스톱 | 오디오, 전사문 중복 억제 |
| 4 | 느린 스톱 | 스톱 | 스톱 | 오디오, 전사문 중복 억제 |
| 5 | 느린 스톱 | 스톱 | 슈톡 | 오디오 |
| 6 | 느린 스톱 | 없음 | 스톱 | 전사문 |
| 7 | 멈춰 | 멈춰 | 멈춰 | 오디오, 전사문 중복 억제 |
| 8 | 멈춰 | 없음 | 멈춰 | 전사문 |
| 9 | 멈춰 | 멈춰 | 멈춰 | 오디오, 전사문 중복 억제 |

- 오디오 5/9, 전사문 정지 판정 8/9, 중복 억제 후 로컬 stop 콜백 **9회**.
- 이전 INT8 통합 재생의 합산 8/9보다 이번 조건에서 1회 개선됐습니다.
- 입력 대기열 손실 0개, producer 오류 0개, 실제 ROS 발행 0건.
- 파일 EOF는 의도한 입력 종료입니다. 기존 라이브 입력 장애 종료 경로로 끝납니다.
- 기록 중 KWS 평가 도구도 실행돼 CPU를 공유했습니다. 이 조건에서 프레임 손실이 없었으나 모든 부하/마이크 환경에서의 실시간 동작을 보장하지 않습니다.
- `frame_to_decision`은 이번 통합 재생에서 약 25~29ms. 마지막 프레임을 읽은 뒤의 처리 시간으로, 말끝→로봇 정지 지연과 다릅니다. 단순 콜백 수만 세지 않고 원본 발화와 대응시켰습니다.

## 5. 구현과 자동 검증

- `audio_stop.precision`: fp32/int8 선택. 실험 기본 fp32, 감지 모드 기본 off.
- `audio_stop_model.py`: 정밀도별 4개 파일의 고정 SHA256 검증. FP32 파일 누락/변조 시 오류를 알리고 기존 전사문 경로를 유지하며 INT8로 조용히 대체하지 않습니다.
- `[AUDIO STOP READY]`에 실제 precision 표시. 다운로드·단독 평가·통합 재생 도구의 `--precision int8` 옵션으로 이전 설정 재현.
- `evaluate_audio_stop_stability.py`: 샘플 해시 확인, 중복 ID 검사, 입력 위치 변형, 원본 좌표 환산, 누락/추가검출/범위 밖 발화 구분, 합성 대조군, 정밀도별 요약.
- 노트북 패키지 2개 빌드 성공. 음성 패키지 **296 passed / 1 skipped**, 평가 도구 **27 passed**. 기존 copyright skip, flake8/pep257 통과.
- 정밀도별 모델 파일 선택과 검증 인자, 누락 시 자동 대체 금지, YAML 기본값 대응, 합성 앞부분의 잘못된 정답 처리 방지, 좌표 환산, 반복 수 분모를 자동 시험했습니다.
- 테스트 코드 줄 길이 오류 2개를 수정한 뒤 전체 패키지 검사를 통과했습니다.
- LLM 프롬프트/공유 ROS 인터페이스/Jetson 제어 코드 변경 없음. held-out 평가 실행 없음.

## 6. 재현 명령

저장소 루트에서 실행합니다. 현재 노트북에는 두 정밀도 모델이 준비돼 있습니다. 다른 환경은 선택 의존성을 설치한 뒤 다운로드 도구를 정밀도별로 실행합니다.

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/download_audio_stop_model.py --precision fp32
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/download_audio_stop_model.py --precision int8
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/evaluate_audio_stop_stability.py \
  --manifest tools/voice_eval/audio_stop_stability_dev_20261112.json \
  --output log/audio_stop_stability_recheck.json
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/replay_audio_stop_pipeline.py \
  --wav recordings/stop_probe.oIcb6b/continuous.wav --precision fp32 \
  --output log/audio_stop_pipeline_fp32_recheck.json
```

## 7. 사용자 실제 마이크 확인 및 남은 단계

아래 절차 안내 후 FP32 마이크 시험을 진행했으며, 누락이 남았습니다(8장). 이번 FP32 생활 소음 시험의 완료 여부는 아직 확인되지 않았습니다. `src/voice_command_pkg/README.md`의 shadow 명령에 `-p audio_stop.precision:=fp32`를 사용합니다. `publish_commands=false`로 실제 발행 없이 스톱/멈춰/정지 각 3회, 일반 문장 3개, 생활 소음을 확인합니다. 정상 연결 로그는 `mode=shadow precision=fp32`, 이벤트는 `published=False`입니다.
기능 채택 여부는 9/9 감지와 비정지 오탐 0을 단기 확인 기준으로 보되, 새 화자/다른 환경까지 검증됐다고 해석하지 않습니다. 입력 위치에 따른 연속 음성 미탐은 계속 해결해야 합니다. 오디오 경로가 감지한 발화만 빨라지고, 놓친 발화는 여전히 Whisper를 기다립니다.

### 로봇에서 확인할 항목

이번 확인은 노트북 단독이며 Jetson 실행/빌드/실물 동작 명령은 필요 없습니다. 실물 정지·명령 수락·모든 액션 취소·새 fetch 허용 및 물리적 지연은 이후 사람이 양쪽 통합 시험에서 확인합니다. 지금은 기본 mode=off를 유지합니다.

## 모델과 증거 파일

- 모델: [kairess/ko-kws-zipformer-tiny](https://huggingface.co/kairess/ko-kws-zipformer-tiny). 동일 리비전의 FP32 파일을 사용했으며 새 학습이나 원격 코드 실행은 하지 않았습니다.
- 리비전: `c2430dd975019802a68077e0fc4dd0521ae193e9`. sherpa-onnx/core 2.0.1 유지.
- 모델 카드의 KWS 지표는 3음절 이상 명사 기준이며 잡음 환경의 오탐 한계도 명시합니다. 이를 우리 2음절 정지 단어의 인식률로 인용하지 않습니다.
- FP32 파일은 외부 캐시에 저장하며 원본 음성/모델/로컬 로그를 Git에 넣지 않습니다.

| FP32 파일 | SHA256 |
|---|---|
| decoder.onnx | `61945f2b85eb78f5d843d6effa4b242ffde1194e272a95e0412366b1d967ec7f` |
| encoder.onnx | `e59230ef379562ed360c8981ff2602c37ba9107cab6228ce94306cc04be3c70f` |
| joiner.onnx | `9dc94a4aaf783b37281e60f084b7111e2d65e31578906844875affa83ac17616` |

| 로컬 결과 파일 | SHA256 |
|---|---|
| log/audio_stop_stability_20261112/stability.json | `e298018c4742fe273db366af364af9867f0f1340d29416ff8057def31d6cbb07` |
| log/audio_stop_stability_20261112/pipeline_fp32.json | `096fc8c01bf7569c2c7233c9e4b89733832bf36a4bd657af75bfd5feb854d960` |
| log/audio_stop_stability_20261112/candidates.json | `10f301531288fda495d93e73f354a7f38f8f64702f9b063571fb12e393798d60` |
| log/audio_stop_stability_20261112/gain_precision.json | `b90ae0377fee4f291d0f840a0bb17f0dc2757c081b03e9d84161c9e4e9e0fd38` |
| log/audio_stop_stability_20261112/streams.json | `5f7714b9e350150b392632bec4329b01f91cef56a83708b50302a6495046f5be` |

## 8. 후속 FP32 실시간 시험 — 안정화 미완료

입력: `recordings/audio_stop_fp32.AYnvxO/session.log` 및 저장된 WAV 13개.
아래 정답 대응은 안내한 스톱/멈춰/정지 각 3회, 일반 문장 3회의 순서를 따랐다는 전제입니다.
중간에 추가된 002의 “고맙습니다.”는 실제 발화 여부가 확인되지 않았으므로 잡음이나 환각으로 단정하지 않습니다.

| WAV 순번 | 예상 발화 | 실시간 KWS | 실제 전사문 | 전사문 stop | 파일 초기화 FP32 KWS |
|---:|---|---|---|---|---|
| 001 | 스톱 | 스톱 | 스톱 | O | 스톱 |
| 002 | 확인 중 | 없음 | 고맙습니다. | X | 없음 |
| 003 | 스톱 | 스톱 | 스톱 | O | 스톱 |
| 004 | 스톱 | 스톱 | 스톱 | O | 스톱 |
| 005 | 멈춰 | 없음 | 멈춰 | O | 멈춰 |
| 006 | 멈춰 | 없음 | 멈춰 | O | 멈춰 |
| 007 | 멈춰 | 멈춰 | 멈춰. | O | 멈춰 |
| 008 | 정지 | 정지 | 종지 | X | 정지 |
| 009 | 정지 | 정지 | 종지 | X | 정지 |
| 010 | 정지 | 없음 | 총지 | X | 정지 |
| 011 | 콜라 가져다줘 | 없음 | 콜라 가져다 줘 | X | 없음 |
| 012 | 휴지 가져다줘 | 없음 | 휴지가 좋다. | X | 없음 |
| 013 | 안녕하세요 | 없음 | 안녕하세요 | X | 없음 |

- 실시간 KWS: 스톱 3/3, 멈춰 1/3, 정지 2/3 → **6/9**.
- 전사문 규칙 stop: 스톱 3/3, 멈춰 3/3, 정지 0/3 → **6/9**.
- 관찰된 두 경로의 합집합 **8/9**. 마지막 정지는 두 경로 모두 놓쳤습니다.
- shadow이므로 KWS가 감지한 “정지”의 전사가 “종지”이면 `[COMMAND] unknown`이 그대로 나옵니다.
  합집합 8/9는 실제 stop 발행 성공률이 아닙니다. KWS 6건 모두 published=False, ROS 13건 모두 skipped입니다.
- 시작 시 state_publishers=0이며 비정지 후보는 robot_unknown으로 차단됐습니다. 이것은 정지 감지 실패 원인이 아닙니다.
- 로그상 KWS RESET/ERROR, pipeline ERROR는 없습니다. 일반 문장 3개에서는 KWS 이벤트가 없었습니다.
  연속 원음과 무음 구간 길이가 없으므로 이 로그로 생활 소음 오탐률을 확정하지 않습니다.

### 같은 WAV의 추가 재생

런타임 `KoreanStopSpotter`와 기존 `evaluate_audio_stop.replay`로 WAV 13개를 정밀도별로 재생했습니다.
각 파일마다 감지기를 초기화했고, 끝에 합성 무음 600ms를 붙였습니다. 정지 검출은 모두 원본 WAV 안에서 발생했습니다.
INT8은 정지 8/9(001 누락), FP32는 9/9이며 나머지 4파일은 양쪽 모두 검출이 없었습니다.
002는 정답이 미확정이므로 비정지 정확도 계산에는 포함하지 않습니다.

새 정지 9파일에 FP32 앞 무음 0/60/120/180/240/300ms를 적용하면 **53/54**입니다.
001의 60ms 조건만 누락했고 나머지는 모두 감지했습니다. 이는 독립 발화 54개가 아닌 동일 9파일의 반복입니다.
파일별 초기화 9/9와 실시간 6/9의 차이 및 위치 변형의 누락은 연속 입력 상태/시작 위치를 후속 조사할 근거입니다.
원래 마이크 입력의 발화 사이 구간이 저장되지 않아 이 WAV를 단순 연결해 원본 실시간 실패를 재현했다고 주장할 수 없습니다.
누락 원인이 모델 내부 상태 하나로 확정됐다는 뜻도 아닙니다.

### LLM 준비 중의 처리 지연

전체 로그의 LLM 준비는 wall_seconds=57.525s(load=6.404s, prompt_eval=49.691s)였습니다.
준비와 겹친 첫 6발화의 STT 시간 중앙값은 **3.0205초**, 준비 후 7발화는 **1.714초**입니다.
CPU 경합 가능성과 일치하지만, 발화와 실행 조건이 다르므로 인과관계나 FP32 자체의 비용을 단정하지 않습니다.
정지는 준비 완료 전에도 계속 처리해야 하며 LLM 준비를 stop 허용 조건으로 추가하지 않습니다.
`frame_to_decision` 0.047~0.170초는 로컬 프레임 읽기 이후 시간이지 실제 말끝→로봇 정지 지연이 아닙니다.

### 결정과 다음 작업

FP32 변경만으로 실시간 정지 안정화가 완료되지 않았습니다. 기본 off, 실험 fp32, VAD 시작 0.4/종료 900ms를 유지합니다.
이번에는 런타임 코드/임계값/정지 어휘를 바꾸지 않았습니다. 특히 “종지/총지”를 무조건 stop 별칭으로 추가하지 않습니다.
다음 구현 과제는 연속 KWS의 시작 위치 민감성을 완화하는 보완 감지를 기존 개발 음성에 먼저 적용하고,
누락뿐 아니라 오탐·중복 발행·CPU 비용·기존 stop 우선순위까지 검증하는 것입니다.
새 연속 입력이 필요해지기 전에는 같은 마이크 시험을 반복 요구하지 않습니다.

현재 결과 확인(노트북, 읽기/오프라인 실행만):

```bash
cd ~/2026project
grep -E '\[(LLM WARMUP|LLM READY|TIME|AUDIO STOP[^]]*)\]' \
  recordings/audio_stop_fp32.AYnvxO/session.log
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/evaluate_audio_stop.py \
  --wav recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135734_010.wav \
  --precision fp32 --output log/audio_stop_fp32_last_stop_recheck.json
```

첫 명령은 준비 시간과 STT 시간을 보여줍니다. 두 번째는 실시간에서 누락된 마지막 정지 WAV의 파일별 재생입니다.
예상 결과는 keyword=정지, audio_end=1.11이며 실시간 안정화 통과 판정은 아닙니다.
이번 기록만으로 Jetson 코드 변경/빌드/실물 시험은 필요 없습니다. 실제 ROS 발행은 하지 않았습니다.

### 후속 시험 증거 해시

| 파일 | SHA256 |
|---|---|
| recordings/audio_stop_fp32.AYnvxO/session.log | `367f22d5f1f4f685d7ef8ae486ccf7aa8a9d181634bb2ac976ad94118e71e5e5` |
| log/audio_stop_fp32_live_20261112/clip_replay.json | `4ce3b6dd543dd0946c7521b9892f90999d937c6028819bfa4e29e325c43b295f` |
| log/audio_stop_fp32_live_20261112/phases.json | `088260390032e298e5804212095f4561a2539c57908707ff030bdf185a753ab8` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135616_001.wav | `14266135c25baac68b47c5ef8c3a927002e376964da8116f41c4785a3d379b08` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135620_002.wav | `a6a87f06f02cecba09003f27142e861a3c3775195171d2e60ad2c9749dd318b3` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135625_003.wav | `5b0feefee14e7a6137d840dfbc07f13171d3cbe7089da1107d7792b3948d3c28` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135634_004.wav | `857f00c7df61d2db574ced6540a70f5f8d2c890f71d0c86f519d3b43b3693647` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135642_005.wav | `5099fb72c843957a7e8d2295a28f7d82a66b81c8c1fe0bfff32350e27fb359a2` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135652_006.wav | `68152648e20432f5ec0b4e21dcf6513dcdb6d6cd2306b05f8b62c3f72933e0a8` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135707_007.wav | `28fbe3a4174ea4e54a8485cf84c727908db3f9d442a71ff0287b1f061eff4f96` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135718_008.wav | `f2f0cda14f2b4dab671f8abd20b163f77f571544677aea4f94185bab87e47253` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135727_009.wav | `edac59f54fa9154164531444e6390fa5dfa0cd87fe1ec76c1e32624057c1bc83` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135734_010.wav | `a301d446ee9035fc40071dd77161c7d51f7bdc9d06cbfd9f63a00eb1e7a93c2d` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135745_011.wav | `1eb3c9a885c942578fecac8f83badab833549926c53535a9df736daf55658f0e` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135752_012.wav | `6cabf30f21b564f2f7ba39971b6c1acc8683a6f96200355e12fa9cd5bcd300e3` |
| recordings/audio_stop_fp32.AYnvxO/vad_command_20261112_135803_013.wav | `231ebd0118a8f26774a8a5196b78ebf415b07c540e40392599b6f4f6fac387b4` |
