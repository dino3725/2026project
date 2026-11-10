# 메모리 직접 전사와 선택적 WAV 저장 — 2026-11-11

## 구현 범위

`feature/voice-memory-transcription`에서 앞 단계의 상시 녹음을 유지하며
PROJECT_CONTEXT 5.2 ①을 구현했다. 이전 상시 녹음 변경도 아직 커밋되지 않은 상태로
함께 있으며, 이번 작업에서 기존 변경을 되돌리거나 커밋/push하지 않았다.

기존: PCM → WAV 저장 → 파일 디코딩 → Whisper.

변경: PCM → float32 배열 → Whisper. 평가용 WAV 저장은 독립적인 선택 사항이다.

- 16 kHz mono little-endian PCM16을 float32로 변환하고 32768로 정규화한다.
- 빈 PCM과 불완전한 16-bit 샘플을 거부한다. 리샘플링/추가 VAD는 하지 않는다.
- `save_recordings` 기본값은 false. 저장하지 않으면 디렉터리도 만들지 않는다.
- true일 때 처리할 발화의 원래 PCM을 WAV로 보관한다. STT는 저장 여부와 무관하게 배열 입력이다.
- 저장 경로 오류·디스크 쓰기 실패는 `[RECORDING ERROR]`로 보고하고 전사는 계속한다.
- `audio_path`는 미저장 또는 실패 시 null이다. `recording_saved`와 `recording_error`를 추가했다.
- `[AUDIO]`는 파일명 대신 순번과 `recording=disabled/failed/경로`를 표시한다.
- 기존 평가 도구의 `transcribe_audio(model, Path, language)` 호출도 유지했다.
- 기존 녹음, 모델·프롬프트·VAD 설정, 정지 키워드, Jetson 코드, 공유 메시지는 변경하지 않았다.

설치된 faster-whisper 1.2.1 소스의 ndarray 입력 경로와 WAV 디코딩의 정규화 방식을
확인한 후 구현했다. numpy를 직접 사용하는 의존성으로 requirements와 package.xml에 명시했다.
라이브러리 설치/업그레이드는 수행하지 않았다.

## 주요 파일

- `audio_samples.py`: 순수 PCM 변환 함수.
- `pipeline.py`: 메모리 STT와 선택적 저장, 오류/메타데이터 처리.
- `parameters.py`, `voice_command.yaml`: save_recordings 설정.
- `test_memory_transcription.py`: 새 검사 16개.
- `compare_stt_inputs.py`: 명시한 개발용 녹음의 파일/메모리 입력 비교. ROS·LLM 호출 없음.
- README, PROJECT_CONTEXT: 현재 동작·실행 방법·후속 작업 갱신.

## 자동 검증

| 검사 | 결과 |
|---|---|
| 노트북 colcon build | eated_interfaces, voice_command_pkg 성공 |
| voice_command_pkg pytest / colcon test | 161 passed, 1 skipped |
| 새 검사 | 16개 추가, 기존 상시 녹음/정지/종료 검사도 통과 |
| flake8 / pep257 | 통과 |
| 비교 스크립트 flake8 | 통과 |
| Python 3.10 AST 검사 | 패키지/비교 스크립트 25개 통과 |
| git diff --check | 통과 |

copyright 검사는 기존처럼 건너뛰었다. 기존 pytest 마커와 flake8 의존성 경고는 남아 있다.

새 검사에는 샘플 부호·바이트 순서·진폭·길이·dtype, 파일 입력 호환성,
Whisper의 지연 생성기 오류 처리, 저장 OFF 시 파일 미생성, 저장 ON 시 WAV 원음 일치,
잘못된 저장 경로와 디스크 오류 후 전사 지속, 저장 실패 중 종료 후 발행 차단을 포함했다.
실제 디스크를 채우거나 권한을 변경하지 않고 임시 파일과 모의 오류를 사용했다.

## 실제 Whisper 입력 비교

사용자가 11/11 상시 녹음 시험에서 만든 개발용 WAV 중 4개를 사용했다.
새 녹음이나 held-out 평가가 아니다. 원본 파일을 변경하지 않았다.

- 모델: small, CPU, int8, ko; faster-whisper 1.2.1, numpy 2.2.6.
- 로컬 캐시만 사용. 다운로드·ROS 노드·로봇 발행·Ollama 호출 없음.
- 워밍업 1회 후 각 파일을 두 방식으로 두 번씩 전사: 총 8쌍, 16회.
- 각 쌍의 파일/메모리 실행 순서를 교대했다.
- 기존 파일 디코더 출력과 직접 PCM 변환 배열을 샘플 단위로 비교했다.
- 모든 쌍에서 입력 배열과 전사문이 완전히 일치했다.

아래 시간은 각 파일의 2회 평균이며 단위는 초다.

| 녹음 파일 | 파일 입력 | 메모리 입력 | 두 방식의 전사문 |
|---|---:|---:|---|
| vad_command_20261111_001212_002.wav | 1.749 | 1.776 | 콜라 가져다 줘 |
| vad_command_20261111_001216_003.wav | 5.704 | 5.368 | QG 하죠 도저 |
| vad_command_20261111_001239_005.wav | 1.743 | 1.749 | 휴지 가져가줘 |
| vad_command_20261111_001252_006.wav | 1.810 | 1.699 | 멈춰 |

8회 전체 중앙값: 파일 입력 **1.754초**, 메모리 입력 **1.753초**.
약 1ms 차이이므로 이 소규모 측정으로 속도 향상을 주장하지 않는다.
파일 입력 시간에는 읽기/디코딩이 들어가지만, 녹음 후 WAV를 쓰는 시간은 이 비교에 포함하지 않았다.
첫 모델 로드와 워밍업도 표의 시간에서 제외했다. 다른 시스템 부하를 엄격히 통제한 벤치마크는 아니다.

전사 오류 사례 `QG 하죠 도저`도 동일하게 재현됐다. 이번 변경이 입력 변환의
동등성을 유지한다는 근거이며, STT 정확도를 개선했다는 결과는 아니다.

상세 결과와 파일 SHA256은 로컬 `log/memory_transcription_20261111/`에 있다.
`comparison.json`은 원 실행 결과다. 최초 스크립트의 파일명/파일 결과 키 중복을 수정했고,
기록된 SHA256으로 파일명을 확인한 `comparison_with_filenames.json`도 남겼다.
전사문·시간은 수정하지 않았고 원 결과를 덮어쓰지 않았다. 현재 스크립트는 처음부터
파일명과 파일 입력 결과를 다른 키로 기록한다.

재현 명령(개발용 자료에만 사용, output은 새 경로 지정):

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/compare_stt_inputs.py \
  recordings/vad_command_20261111_001212_002.wav \
  recordings/vad_command_20261111_001216_003.wav \
  recordings/vad_command_20261111_001239_005.wav \
  recordings/vad_command_20261111_001252_006.wav \
  --rounds 2 --output log/memory_transcription_repeat/comparison.json
```

## 남은 작업과 확인 범위

- 이 단계에서 실제 마이크/ROS 노드를 새로 실행하지 않았다. 실제 녹음의 오프라인 STT는 실행했다.
- 상시 입력·종료·대기열 정책은 기존 테스트로 회귀 확인했다.
- WAV 저장 OFF라도 로그에는 전사문이 표시된다. 전사문 로그 저장 여부와 WAV 저장 옵션은 다르다.
- 저장을 켜면 파일 쓰기는 처리 스레드에서 수행한다. 저장 장치 지연이 완전히 사라지는 설계는 아니다.
- 대기열에서 제거한 발화는 저장하지 않는다. 저장 실패 시 성공한 WAV가 있다고 가정하면 안 된다.
- 첫 LLM 요청 지연, 인증, IDLE 차단, 정지 전용 감지는 그대로 후속 작업이다.
- 프롬프트·평가셋을 변경하지 않았으며 held-out을 재실행하지 않았다.

## 사람이 확인할 항목

명령 발행 없이 기본 미저장 모드 확인:

```bash
source ~/2026project/voice_env.sh
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file ~/2026project/src/voice_command_pkg/config/voice_command.yaml \
  -p publish_commands:=false
```

종료 후 다시 실행할 때 `-p save_recordings:=true`를 추가하면 저장 모드다.

- [ ] OFF에서 recording=disabled이고 새 WAV 없이 STT/명령 판정이 되는지.
- [ ] ON에서 recording=경로이고 실제 WAV가 생성되는지.
- [ ] 처리 중 다음 발화 수집과 Ctrl+C 종료가 유지되는지.

로봇에서 확인할 항목(사람이 수행):

- [ ] 노트북 단독 확인 후 기존 fetch/stop 전달과 실제 제어 동작을 별도로 확인.
