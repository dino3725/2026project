# 2026-11-18 정지 누락 진단을 위한 연속 입력 보관

## 해결하려는 문제

최근 실시간 시험의 두 번째 스톱으로 대응한 WAV는 사용자가 직접 들어도 내용이
불명확했습니다. 기존 `save_recordings`는 순차 처리에 도달한 완성 발화만 저장하므로,
VAD가 무시한 입력이나 대기열에서 버린 발화를 복원할 수 없습니다. 이번 구현은
정지 인식률 개선을 추가 주장하는 대신, 다음 시험에서 원인을 구분할 자료를 남깁니다.

## 구현

- `continuous_recording_seconds`: 기본 0(끔), 선택 시 0.03~300초. 프레임 단위로
  올림하며 최대 10,000프레임, PCM 약 9.6MB를 메모리에 보관합니다.
- 기존 마이크 하나의 초기 안정화 이후 PCM을 VAD 판정 전에 복사 없이 참조 보관합니다.
  무음·생활 소음·시작 조건에 미달한 짧은 발화도 포함합니다. KWS에는 원본을 먼저 전달합니다.
- 제한 도달 또는 정상 종료/입력 오류 시 별도 스레드가 WAV와 metadata.json을 저장합니다.
  녹음 스레드는 디스크 쓰기나 저장 완료를 기다리지 않습니다. 제한 도달 후 음성 처리는 계속합니다.
- 매번 `recordings_dir/continuous_*/`를 새로 생성합니다. 저장 실패를 별도 로그로
  알리고 음성 처리를 중단하지 않습니다. 종료 때 저장 대기는 최대 2초입니다.
- metadata에는 PCM SHA256, 프레임 수, VAD/KWS 설정, 입력 읽기 시각, 종료 이유가 있습니다.
  WAV 0초는 기존 `[VOICE START] audio=...`의 0초와 같습니다. 초기 1.2초 안정화는 제외됩니다.
- 저장한 파일을 재생할 때는 `--post-warmup`을 사용해 초기 구간을 또 버리지 않습니다.
- `audio_stop_diagnostic.yaml`은 발행 false, KWS shadow, 위치 보완/2배 후보,
  발화 저장과 최대 300초 연속 저장을 함께 선택합니다. 기본 운영 설정은 바꾸지 않았습니다.

입력 시각이 불연속이면 해당 시점 앞까지만 저장하고 `input_discontinuity`로 표시합니다.
SHA256은 저장 PCM의 무결성을 검사하며 마이크 하드웨어 수준의 누락 부재를 보장하지 않습니다.
저장 완료 전에 강제 종료되면 메모리 자료를 잃을 수 있고, 저장 오류 시 일부 WAV만 남을 수
있습니다. 따라서 `[CONTINUOUS AUDIO SAVED]`와 metadata 확인이 필요합니다.

## 파일 기반 통합 확인

기존 50초 `stop_probe.oIcb6b/continuous.wav`를 실제 시간 간격으로 공급하고
Whisper/ECAPA/KWS와 진단 저장을 함께 실행했습니다. ROS·마이크·실제 LLM은 실행하지 않았습니다.

- 로컬 stop 콜백 9회, 입력 생산 오류 0, KWS 프레임 누락 0.
- 저장 1,627프레임 / 48.81초. 50초 입력에서 초기 1.2초를 제외하고 재생 도구가
  마지막 불완전 프레임에 보충한 10ms를 포함합니다.
- 저장 PCM이 위 입력과 **바이트 단위로 완전히 일치**하며 SHA256도 일치합니다.
- 저장한 WAV를 `--post-warmup`으로 다시 재생한 결과 VAD 시작/종료 이벤트 18개가
  처음 재생과 모두 같았고 로컬 stop 콜백도 9회, 프레임 누락 0이었습니다.
- EOF를 정상 마이크의 입력 종료 오류 경로로 주입했으므로 metadata의 end_reason은
  `input_error`입니다. 이것은 이번 파일 재생에서 예상한 값이며 실제 마이크 오류가 아닙니다.
- 저장 기능을 켰다고 실제 9회 소음 시험의 불명확한 누락이 해결된 것은 아닙니다.

빌드 성공, 음성 패키지 **399 passed / 1 skipped**, 평가 도구 **60 passed**입니다.
저장 한도, 무음 포함, 초기 안정화 제외, 입력 불연속, 저장 충돌 방지, 디스크 오류/지연,
저장 한도 이후 KWS 원본 전달, 진단 overlay의 발행 차단을 자동 검사했습니다.
초기 코드 스타일 검사에서 발견한 줄 길이/들여쓰기를 수정하고 최종 검사도 통과했습니다.

근거: `log/continuous_diagnostic_20261118/`의 replay.json, integrity.json,
roundtrip.json, roundtrip_check.json, 테스트 로그.
WAV·원시 결과는 Git에 포함하지 않습니다.

## 로봇에서 확인할 항목

Jetson 실행·빌드·파일 복사는 필요 없습니다. 이번 구현은 자동 검사와 기존 파일로
확인했으며 새 녹음을 완료 조건으로 요구하지 않습니다. 다음 실시간 확인을 할 때는
다른 음성노드를 종료하고 노트북 터미널 하나에서 아래처럼 실행하면 됩니다.

```bash
cd ~/2026project
source voice_env.sh
export ROS_DOMAIN_ID=37
export ROS_LOCALHOST_ONLY=0
mkdir -p recordings
DIAG_DIR=$(mktemp -d "$PWD/recordings/stop_diagnostic.XXXXXX")

ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file src/voice_command_pkg/config/voice_command.yaml \
  --params-file src/voice_command_pkg/config/speaker_development.yaml \
  --params-file src/voice_command_pkg/config/audio_stop_diagnostic.yaml \
  -p recordings_dir:="$DIAG_DIR" \
  2>&1 | tee "$DIAG_DIR/session.log"
```

1. `publish_commands=false` 안내와 `CONTINUOUS AUDIO ARMED`, `AUDIO STOP READY mode=shadow`,
   `AUDIO STOP RECHECK READY ... gain=2.0`를 확인합니다.
2. `LISTENING`, `SPEAKER READY`, `LLM READY`까지 나온 후 말합니다. 준비가 늦어져
   저장 한도에 먼저 도달했다면 같은 명령을 새 디렉터리에서 다시 시작해야 합니다.
3. 스톱 3회 → 멈춰 3회 → 정지 3회, 각 발화 사이 약 5초 간격을 둡니다.
4. ‘콜라 가져다 줘’, ‘휴지 가져다 줘’, ‘안녕하세요’를 말하고 생활 소음만 30초 유지합니다.
5. Ctrl+C로 종료합니다. 300초를 채울 필요는 없습니다. 저장 한도가 먼저 끝나도
   음성노드는 계속 실행되므로 시험 종료 시 Ctrl+C를 눌러야 합니다.
6. 아래로 로그와 결과 경로를 확인합니다.

```bash
grep -E '\[(CONTINUOUS AUDIO[^]]*|AUDIO STOP[^]]*|STT|COMMAND|ROS|DROPPED|ERROR)\]' \
  "$DIAG_DIR/session.log"
find "$DIAG_DIR" -name metadata.json -print -exec cat {} \;
echo "결과 위치: $DIAG_DIR"
```

- 저장 확인 기준: 메타데이터와 WAV가 있고 PCM SHA256·프레임 수가 일치하며 원하는
  시험 구간이 audio_end 안에 들어 있음. SAVED는 보조 확인입니다. 노드 종료 중에는
  로그가 차단돼 SAVED가 없을 수 있습니다. 종료 이유와 ERROR/input_discontinuity도
  함께 확인하며 input_error를 정상 종료로 임의 변경하지 않습니다.
- 정지 후보의 실시간 목표: 정지 9회 모두 오디오 경로로 감지, 일반 문장/소음 오탐 0.
  여러 감지 경로의 로그 줄 수를 더해 성공 횟수로 계산하지 않습니다.
- 모든 KWS 이벤트 published=False, ROS skipped가 정상입니다. Jetson 없이 일반 요청이
  robot_unknown으로 버려지는 것은 예상한 결과이며 화자 인증 정확도 시험이 아닙니다.

저장된 연속 파일의 VAD 판정을 다시 보려면 같은 터미널에서 실행합니다.

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/inspect_vad_audio.py \
  "$DIAG_DIR"/continuous_*/continuous.wav --output "$DIAG_DIR/vad.json"
```

이 도구는 WAV 앞부분을 버리지 않으므로 저장 로그와 audio 시간을 비교할 수 있습니다.
음성이 실제로 담겼는지 청취한 뒤 VAD 시작 유무, 오디오 감지, STT 순으로 확인합니다.
연속 파일이 있어도 말소리가 불명확하면 억지로 정답 라벨을 붙이지 않습니다.

## 후속 실시간 시험: stop_diagnostic.8bAOVn

사용자는 안내한 시험 후 생활 소음도 테스트했다고 보고했습니다. 전체 session.log,
발화 WAV 12개, continuous_l04t3vlf의 WAV/metadata를 직접 확인했습니다.

| 경로 | 스톱 | 멈춰 | 정지 | 합계 |
|---|---:|---:|---:|---:|
| 연속 KWS | 2/3 | 3/3 | 2/3 | 7/9 |
| 완성 발화 재검사 | 3/3 | 3/3 | 3/3 | 9/9 |
| 전사문 규칙 | 3/3 | 3/3 | 3/3 | 9/9 |

오디오 두 경로의 합집합은 9/9입니다. 세 번째 스톱과 세 번째 정지를 재검사가
보완했습니다. shadow이므로 같은 발화의 경로별 감지 로그가 각각 나오는 것이 정상이며,
16개 오디오 로그를 16개 발화 또는 중복 stop 발행으로 세지 않습니다. 전 이벤트
published=False, ROS skipped 12회, 실제 발행 0입니다.

재검사 성공 계산 시간은 59~171ms, 중앙값 139ms입니다. 이 값은 발화 종료 대기
900ms를 포함한 실제 말끝→로봇 정지 지연이 아닙니다. LLM 준비는 약 2.041초로
첫 발화 이전에 끝났습니다. 과거 66초 cold warmup 부하까지 검증한 결과는 아닙니다.

‘콜라 가져다 줘’, ‘휴지 가져다 줘’, ‘안녕하세요’에서 정지 오탐은 없었습니다.
이 세 발화의 재검사는 0.5초 예산으로 만료됐으므로 실시간 재검사가 끝까지
검사한 것은 아닙니다. 별도 파일 무제한 재생에서도 세 파일 모두 정지 감지 없음,
정지 파일 9개는 모두 감지했습니다. 운영 제한을 늘리거나 임계값을 바꾸지 않았습니다.

### 생활 소음과 연속 원음 검증

- 연속 PCM 3,962프레임 / 118.86초, metadata PCM SHA256 일치.
- 마지막 발화 VAD 종료는 audio=81.270초. 이후 **37.59초**가 연속 파일에 남아 있습니다.
- 전체 연속 파일의 VAD 재생 시작/종료 이벤트 24개가 실시간 로그와 일치합니다.
  마지막 구간에 VAD 추가 시작·종료가 없었습니다.
- 전체 연속 파일을 실제 FP32 KWS로 순서대로 재생했으며 마지막 37.59초에 정지 감지 0.
  실시간 로그에도 일반 문장 및 그 이후 구간의 정지 이벤트가 없습니다.
- 발화 WAV 12개가 모두 연속 WAV의 대응 위치와 바이트 단위로 일치합니다.
  사용자 생활 소음 보고와 저장된 구간 근거를 함께 기록합니다. 음향 환경 일반에 대한
  오탐률이나 마이크 하드웨어 수준의 손실 부재를 주장하지 않습니다.

### 저장 종료 표시와 로봇 상태

SAVED 로그는 없지만 WAV와 metadata는 완성돼 있고 무결성 검사를 통과했습니다.
노드의 `_log_pipeline`은 `_closing` 이후 로그를 출력하지 않으므로 저장 성공과
종료 로그 존재는 별개입니다. 메타데이터 종료 이유는 `input_error`입니다. Ctrl+C가
녹음 자식 프로세스도 종료하는 경우 가능하지만 해당 종료 오류 로그가 없어 이번
종료 원인을 확정하지 않습니다. 확보된 연속 구간의 무결성과 구분해 기록합니다.

실제 `/control/state` 발행자 1개에서 ERROR를 수신했습니다. 일반 명령의
`robot_error`/`speaker_not_checked`는 로봇 상태 때문에 차단된 것이며 타인 인증 거절이
아닙니다. 이번 시험의 정지 감지는 이 상태와 무관하게 동작했습니다. 제어기 ERROR의
원인은 이번 음성 로그만으로 판단할 수 없습니다.

이번 **한 세션의 shadow 정지 9회 및 저장된 생활 소음 구간은 목표를 충족**했습니다.
실제 발행 중복 억제, Jetson 수신/물리 정지, 다양한 환경과 cold warmup 안정화는
별도 확인 범위입니다. 다음 후보 검증은 이번 연속 원음의 로컬 active 콜백 재생이며,
이번 결과 확인을 위해 동일한 마이크 시험을 다시 요구하지 않습니다. 기본 mode=off 유지.

근거: `log/continuous_live_20261118/analysis.json`과 analyze.py.
개별 WAV 및 연속 WAV/session.log SHA256, 구간 대응, VAD/KWS 재생 결과를 보관했습니다.
이번에는 분석·문서만 변경했으며 음성 처리 코드·설정은 변경하지 않았습니다.
