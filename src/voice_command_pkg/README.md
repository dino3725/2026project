# Local Voice Robot Command

Ubuntu 노트북에서 한국어 음성 명령을 완전히 로컬로 처리하고, 검증된 명령만
ROS2 `/voice/robot_command` topic으로 발행하는 서비스 로봇 명령 파이프라인입니다.

현재 Jetson과의 ROS2 DDS 통신 및 fetch 명령 전달까지 검증됐습니다. 노트북에서
음성 인식, 명령 분류 및 custom ROS2 message publish를 수행합니다.

## 처리 구조

```text
PulseAudio default microphone (한 번 열고 계속 수집)
        ↓
WebRTC VAD (녹음 스레드: speech start/end)
        ↓
완성된 발화 대기열 (기본 2개, 가장 오래된 발화부터 제거)
        ↓
처리 스레드: 16 kHz mono PCM → float32 배열 (메모리 직접 입력)
        ↓
faster-whisper small / CPU / int8 / Korean
        ↓
전사문 정규화 + 정지 키워드 판정 ── 걸리면 → stop (LLM 호출 안 함)
        ↓ 아니면
로봇 상태·응답 대기·발화 시각 검사 (현재 IDLE 구간의 새 요청만)
        ↓ 허용
LLM 준비 상태·발화 시작 시각·fetch 유효시간 검사
        ↓ 통과
Ollama qwen3:4b-instruct (fetch 4종 또는 unknown만 판단)
        ↓
structured JSON validation
        ↓
fetch 발행 직전 상태·유효시간 재검사 → RobotCommand publish
        ↓
Jetson 수락·거절 및 상태 전환 → 고정 템플릿 화면 안내
```

STT·LLM 처리 중에도 녹음 스레드는 다음 발화를 수집합니다. 마이크 안정화용
1.2초 버리기는 최초 시작 때만 적용합니다. Whisper는 한 처리 스레드에서
재사용하며 모델·LLM 프롬프트는 유지합니다. Whisper 전사 힌트와 beam은
`whisper.initial_prompt`, `whisper.hotwords`, `whisper.beam_size`로 설정합니다.
WAV 저장은 기본으로 끄고,
평가·문제 분석 시에만 `save_recordings:=true`로 켭니다.

2026-11-12부터 VAD 시작 비율 기본값은 `0.4`(300ms 창의 4/10 프레임)입니다.
짧은 “스톱” 재생과 실제 노드의 정지 6/6 성공, 사용자의 생활 소음 30초 이상 없음 보고를
근거로 채택했습니다. RMS 200과 종료 무음 900ms는 유지합니다. 다양한 잡음 환경과
느린 “스톱”의 “슈톡” 오전사까지 해결됐다는 의미는 아닙니다.

### Whisper 설정 비교와 짧은 정지 발화

`whisper.beam_size`는 양의 정수입니다. `initial_prompt`와 `hotwords`는 문자열이며,
빈 문자열이면 해당 힌트를 사용하지 않습니다. `[STT READY]`에 beam과 힌트 사용 여부가
출력됩니다. 힌트는 Whisper의 전사를 돕는 문맥이며, LLM 분류 프롬프트나 정지 규칙이 아닙니다.
힌트에 적힌 단어를 명령으로 바로 실행하지 않으며, 실제 전사문에 기존 정지 규칙을 적용합니다.

현재 기본값은 beam 5, 빈 힌트, `whisper.temperatures: [0.0]`입니다. 온도 목록은
Whisper가 낮은 확신의 결과를 재시도할 때 사용할 값이며, `[0.0]`은 첫 beam 탐색만 수행합니다.
기존 목록 `[0.0, 0.2, 0.4, 0.6, 0.8, 1.0]`으로 원복할 수 있습니다. 27개 개발 녹음에서
최종 명령 54/54, 정지 18/18을 유지했고 느린 “스톱”의 재시도 지연을 줄였습니다.
다른 발화에서의 정확도를 보장하지 않으므로 새 녹음으로 확인해야 합니다.
전체 어휘 힌트는 무음에서 정지 단어를 만들어냈기 때문에 기본값에 적용하지 않았습니다.

27개 개발 녹음의 같은 PCM을 사용하는 비교 도구는
`tools/voice_eval/compare_stt_settings.py`입니다. 정답 문장·명령·WAV SHA256을 manifest에
명시하고 4개 설정을 순서 교대하여 비교합니다. 상세 결과와 재현 명령은
`tools/voice_eval/results/2026-11-11_stt_settings.md`를 참고하세요.

VAD가 시작을 감지하지 못한 짧은 말은 `save_recordings:=true`여도 WAV에 남지 않습니다.
따라서 저장된 느린 “스톱”의 전사가 개선돼도 빠른 “스톱”의 녹음 누락까지 해결된 것은 아닙니다.
이 경우 `arecord`로 발화 사이 무음까지 연속 저장한 뒤 `inspect_vad_audio.py`로 시작 전
프레임의 RMS·VAD·임계값 통과 여부를 확인합니다. 임계값을 낮추기 전에 누락과 잡음의
근거를 확보하는 용도입니다. 오디오 정지 전용 감지기는 아래의 실험 기능으로 구현했습니다.

### 정지 전용 오디오 감지 — 실험 기능, 기본 비활성화

같은 PulseAudio 스트림의 30ms PCM을 VAD 판정 전에 별도 KWS 스레드로 전달합니다.
Whisper/LLM과 독립적으로 `멈춰`, `정지`, `스톱`을 찾고, `active`에서는 기존 발행 함수를
통해 stop을 보냅니다. 새 마이크 스트림이나 ROS 토픽은 만들지 않습니다.
초기 1.2초 안정화 구간은 기존처럼 버립니다. 기존 전사문 정지 규칙은 모든 모드에서 유지합니다.

**현재 후보 모델은 운영 채택하지 않았습니다.** 초기 INT8 개발 WAV 49개의 대상 정지 22개 중 17개,
비정지 26개에서 오탐 0개였지만, 실제 파이프라인 조건의 50초 재생에서는 빠른 감지 3/9,
Whisper와 합친 정지 8/9였습니다. 입력 시작 위치에 따른 결과 변화도 확인했습니다.
정확도와 종단 지연 개선을 보장하지 않으며, 기본 `audio_stop.mode: "off"`를 유지합니다.
상세 결과: `tools/voice_eval/results/2026-11-12_audio_stop.md`.

후속 실제 shadow 시험(2026-11-12)은 완료했습니다. 정지 9회에서 KWS 6/9,
전사문 정지 8/9, 두 경로 중 하나에서 감지한 발화는 9/9였습니다. 마지막 “정지”의
Whisper 오전사 “총지”를 KWS가 보완했습니다. 실제 발행은 0건이며 운영 채택은 계속
보류합니다. 생활 소음 시험 완료는 사용자 보고로 기록했습니다. 이 INT8 시험을
단순 반복할 필요는 없으며, 아래의 후속 FP32 변경과 새 확인 절차를 구분합니다.

후속 안정화 1차에서는 **같은 모델의 FP32 버전**을 실험 기본 정밀도로 선택했습니다.
최근 shadow 녹음을 포함한 대상 정지 31개를 같은 방식으로 비교했을 때 INT8 23/31 →
FP32 31/31, 앞 무음 6가지 반복에서는 165/186 → 185/186이었습니다. 비정지 29개 ×
6회는 두 설정 모두 오탐 0회입니다. 50초 운영 조건 재생은 KWS 5/9, 기존 Whisper와
합쳐 9/9였지만 연속 입력 위치를 바꾸면 여전히 누락이 있습니다. 기본 모드는 **off**로
유지합니다. 이후 FP32 실제 마이크 시험은 합산 8/9로 마지막 정지를 놓쳤습니다. 상세 결과는
`tools/voice_eval/results/2026-11-12_audio_stop_stability.md`에 있습니다.

| 파라미터 | 기본값 | 의미 |
|---|---|---|
| `audio_stop.mode` | `off` | `off`: 기존 경로만, `shadow`: 감지 로그만, `active`: 감지 시 stop 처리 |
| `audio_stop.model_dir` | YAML의 고정 리비전 캐시 경로 | 파일 SHA256 확인 후 로드; 실행 중 자동 다운로드 없음 |
| `audio_stop.precision` | `fp32` | `int8`로 이전 설정 재현 가능; 서로 다른 정밀도 파일을 섞거나 자동 대체하지 않음 |
| `audio_stop.utterance_recheck` | `true` | off 이외 모드에서 완성 VAD 발화를 별도 스레드/새 감지 상태로 재검사 |
| `audio_stop.threshold` | `0.01` | 현재 개발 비교 후보, 운영 확정값 아님 |
| `audio_stop.score` | `1.0` | 키워드 탐색 가산점 |
| `audio_stop.queue_frames` | `128` | 전달 버스트를 수용하는 최대 프레임 수; 추론은 녹음 스레드 밖에서 수행 |
| `audio_stop.max_lag_seconds` | `0.5` | 연속 감지는 프레임 읽기, 보완 감지는 VAD 종료 후 최대 나이; 실제 마이크 지연이 아님 |

대기열이 넘치면 미처리 프레임을 버리고 모델의 음향 문맥을 초기화합니다. 오래된 프레임과
초과 추론 결과도 폐기합니다. `[AUDIO STOP RESET]`은 이런 입력 손실을 알립니다.
모델/의존성 오류는 `[AUDIO STOP ERROR]`로 표시하고 전사문 정지는 계속합니다.
`active` 정지는 이전/겹친 fetch를 벽시계와 오디오 위치로 무효화하므로 늦게 돌아온 LLM
결과도 발행하지 않습니다. 동일 발화의 전사문 stop은 오디오 경로 발행이 성공했을 때만
중복 억제합니다. 실패·dry-run에서는 전사문 경로가 다시 시도하며, 새 정지를 막는
전역 cooldown은 없습니다. `run_once` 평가 API는 KWS를 지원하지 않고 `off`만 받습니다.

노트북의 새 터미널에서 준비합니다. 이미 `voice_env.sh`를 source했다면 준비 두 줄은
생략할 수 있습니다. 현재 개발 노트북에는 선택 의존성과 모델을 설치해 검증했습니다.
다른 환경에서 처음 준비할 때만 설치/다운로드 명령을 실행합니다.

```bash
cd ~/2026project
source voice_env.sh

.venv/bin/python -m pip install -r src/voice_command_pkg/requirements-audio-stop.txt
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/download_audio_stop_model.py
```

위 다운로드 도구의 기본 정밀도는 FP32입니다. 이전 결과 재현은 도구에
`--precision int8`, ROS 노드에는 `-p audio_stop.precision:=int8`을 지정합니다.
FP32 파일이 없으면 명확한 오류를 남기고 전사문 정지만 유지하며 INT8로 자동 대체하지 않습니다.
현재 개발 노트북에는 두 버전 모두 해시를 확인해 설치했습니다.

저장 녹음만으로 재현합니다. 마이크·ROS·LLM 서버를 사용하지 않습니다.

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/evaluate_audio_stop.py \
  --manifest tools/voice_eval/audio_stop_dev_20261112.json \
  --output log/audio_stop_recheck.json

# 약 50초: 실제 VAD/Whisper/KWS, 한 PCM 파이프, 발행은 로컬 기록 함수로 대체.
# WAV 끝은 의도적인 입력 종료로 처리하므로, 처리 완료용 끝 무음이 있는 파일을 사용합니다.
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/replay_audio_stop_pipeline.py \
  --wav recordings/stop_probe.oIcb6b/continuous.wav \
  --output log/audio_stop_pipeline_recheck.json
```

입력 시작 위치까지 비교하는 개발 평가(여러 번 재생한 수를 독립 녹음 수로 세지 않음):

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/evaluate_audio_stop_stability.py \
  --manifest tools/voice_eval/audio_stop_stability_dev_20261112.json \
  --output log/audio_stop_stability_recheck.json
```

안정화 2차는 **발화 종료 후 별도 감지기로 재검사**하는 보완 경로입니다. VAD 종료 900ms를
기다리지만 Whisper/LLM 대기와 독립적으로 실행합니다. 대기열 1개, VAD 종료 후 0.5초 제한,
성공한 동일 발화만 중복 억제를 적용합니다. 보완 모델 오류는 다른 정지 경로에 영향을 주지 않습니다.
분리 정지 40/40·비정지 오탐 0/32, 50초 통합 재생은 연속 5/9 + 보완 8/9의 합집합 9/9이며
로컬 stop 콜백 9회였습니다. 실제 ROS 발행은 없습니다. 연속 감지 자체의 민감성과 말하지 않은
“고맙습니다” 전사는 미해결입니다. 상세 결과: `tools/voice_eval/results/2026-11-12_audio_stop_recheck.md`.

다음 실시간 확인은 **새 보완 경로의 명령을 발행하지 않는 shadow 시험**입니다. Jetson을 켤 필요 없습니다.
기존 음성 노드는 Ctrl+C로 종료한 뒤 하나만 실행합니다. 비교 조건을 맞추기 위해 `[LLM READY]` 이후 평소 속도로
“스톱” 3회, “멈춰” 3회, “정지” 3회를 말하고, 각각 5초 간격을 둡니다. 이어서
“콜라 가져다줘”, “휴지 가져다줘”, “안녕하세요”, 생활 소음 30초를 시험합니다.

```bash
mkdir -p recordings
KWS_CHECK_DIR=$(mktemp -d "$PWD/recordings/audio_stop_check.XXXXXX")
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file src/voice_command_pkg/config/voice_command.yaml \
  -p publish_commands:=false \
  -p audio_stop.mode:=shadow \
  -p audio_stop.precision:=fp32 \
  -p audio_stop.utterance_recheck:=true \
  -p save_recordings:=true \
  -p recordings_dir:="$KWS_CHECK_DIR" 2>&1 | tee "$KWS_CHECK_DIR/session.log"
```

Ctrl+C 후 같은 터미널에서:

```bash
grep -E '\[(AUDIO STOP[^]]*|STT|COMMAND|ROS|DROPPED|ERROR)\]' "$KWS_CHECK_DIR/session.log"
echo "결과 위치: $KWS_CHECK_DIR"
```

- 연결 정상: `[AUDIO STOP READY] mode=shadow precision=fp32`와 `[AUDIO STOP RECHECK READY]`, 감지 시 `published=False`.
- 정지마다 `source=stream` 또는 `source=utterance_recheck`가 있어야 합니다. shadow는 중복 이벤트를 모두 기록하므로 줄 수만 합산하지 않습니다.
- 시험 정상: 실제 명령 발행 없음(`[ROS] skipped`), 기존 `[STT]`와 전사문 stop 유지.
- 모델 채택 기준: 9회 모두 감지하고 일반 발화/생활 소음에는 오탐이 없어야 합니다.
  현재 모델이 이 기준을 통과했다고 간주하지 않습니다. RESET/ERROR도 함께 확인합니다.
- `frame_to_decision`은 프레임 읽기 이후, `vad_end_to_decision`은 VAD 종료 이후 시간이며 말끝→정지 시간이 아닙니다. RECHECK DROP/ERROR가 없어야 합니다.
- WAV는 VAD 발화만 저장합니다. 생활 소음 전체나 VAD 미검출 음성을 보존하는 기능은 아닙니다.

원복은 노드를 끄고 `-p "audio_stop.mode:='off'"`로 다시 실행하면 됩니다. 기본 YAML은 이미 off입니다.
이번 단계에서 Jetson 명령이나 실물 시험은 필요하지 않습니다. 모델 검증을 통과하기 전에는
`active`와 실제 명령 발행을 함께 켜지 않습니다.

### 로봇 상태 구독과 발행 조건

노드는 `/control/state`(String, reliable/transient-local/depth 1)와
`/control/command_status`(RobotCommandStatus, reliable/volatile/depth 10)를 구독합니다.
상태를 받기 전, IDLE 이외 상태, 앞선 요청 응답 대기 중에는 일반 요청을 차단합니다.
전사문 stop은 상태·응답 조건을 건너뛰며, dry-run과 종료 후 발행 금지는 그대로 적용합니다.

- IDLE을 확인한 이후 시작한 발화만 받습니다. pre-roll을 포함해 작업 중 녹음된 요청은
  IDLE로 복귀해도 재실행하지 않습니다. IDLE 확인 직후 약 1초 기다린 뒤 새로 말하세요.
- 일반 요청의 LLM 처리 전과 발행 직전에 상태를 검사합니다. 노드에서도 상태 갱신과
  발행을 같은 잠금으로 보호하고, 잠금을 기다리는 동안 지난 fetch 유효시간을 다시 확인합니다.
- fetch를 발행하기 직전에 응답 대기를 등록합니다. 수락 응답만 받고 아직 IDLE로 보이더라도
  두 번째 fetch는 허용하지 않습니다. 작업 상태를 거쳐 IDLE로 복귀하면 새 발화를 받습니다.
- 명시적 거절은 그 시점 이후 새 발화부터 허용합니다(로봇도 IDLE이어야 함).
  `robot.response_timeout_seconds` 기본 5초는 무응답 안내 기준입니다. 타임아웃으로
  요청을 재전송하거나 발행 제한을 자동으로 풀지 않습니다. 실제 상태와 연결을 확인하고,
  필요하면 사람이 stop/X 복구를 수행하여 ERROR·복귀·IDLE 상태 전환을 확인하세요.
- 상태 메시지는 heartbeat가 아닙니다. 상태 메시지의 나이를 이유로 연결을 끊지 않습니다.
  `robot.connection_poll_seconds` 기본 0.5초마다 DDS graph에서 상태 발행자를 확인합니다.
  발행자가 없거나 여러 개이면 차단하고, 발행자 교체/복구 시 구독을 새로 만들어
  이전 대기 메시지를 버리고 현재 제어기의 보관된 상태를 다시 받습니다.
- graph의 장애 감지는 DDS 발견 지연의 영향을 받습니다. 제어 프로세스가 멈췄지만
  graph에는 남아 있는 경우까지 즉시 탐지하지는 못합니다.
- 수락 결과는 현재 로컬 요청의 action/target과 맞고, 상태 발행자와 같은 노드의 유일한
  결과 발행자일 때 사용합니다. 현재 메시지에 request ID가 없으므로 **명령 발행 노드 하나**를
  전제로 합니다. 다른 발행자의 동일 명령이나 같은 대상의 매우 늦은 과거 응답을
  완벽하게 구별하는 프로토콜은 아닙니다. 무관한 응답·중복 수락·기한 초과 응답은 안내에 쓰지 않습니다.
- `[FEEDBACK] event=robot_accepted`는 실제 수락 응답 뒤에만 출력합니다.
  `robot_carrying`은 수락된 대상과 DRIVE_TO_PLACE 상태가 모두 확인됐을 때 표시합니다.
  ERROR는 원인 미상의 오류/정지일 수 있어 인식 실패라고 단정하지 않습니다.
  `robot_stop_sent`와 `robot_stop_accepted`는 전송과 수락 안내이며 물리적 정지 완료 증명은 아닙니다.
- `[ROBOT STATE]`, `[ROBOT STATUS]`, `[ROBOT GATE]`, `[ROBOT CONNECTION]`으로 상태를 추적합니다.
  안내는 `messages.robot_*`에서 바꿉니다. 물체 이름 자리에는 `{object}`만 지원합니다.

`publish_commands:=false`에서도 상태 검사는 수행합니다. 로봇이 연결되지 않았다면
일반 요청은 `robot_unknown`으로 폐기되는 것이 정상입니다. IDLE에서 dry-run 판정에
성공해도 실제 요청 대기나 수락 안내를 만들지 않습니다. ROS 없는 STT/LLM 측정 도구는
상태 검사를 연결하지 않은 독립 `VoiceCommandPipeline`을 사용합니다.

### LLM 워밍업과 장애 복구

- Whisper 로드 후 `[STT READY]`를 표시하고 녹음·전사를 시작합니다.
  별도 `voice_llm` 스레드가 실제 분류 요청과 동일한 프롬프트·JSON 스키마·옵션으로
  더미 문장 1회를 처리합니다. 더미 결과는 명령 발행 경로로 보내지 않습니다.
- `[LLM READY]` 이후 새로 말한 일반 요청부터 받습니다. 준비 중 요청은
  `llm_not_ready`, 준비 완료 전에 시작한 대기 발화는 `llm_before_ready`로 폐기합니다.
  pre-roll 시작 시각도 검사하므로 완료 경계에 걸친 발화는 다시 말해야 할 수 있습니다.
- Ollama가 꺼져 있거나 모델이 없거나 워밍업 중이어도 전사문 stop 판정은 계속합니다.
  Ollama 실패 때문에 마이크·Whisper 처리 전체를 종료하지 않습니다.
- 일반 요청 대기는 **10초와 남은 fetch 유효시간 중 짧은 값**으로 제한합니다.
  예를 들어 대기·STT에 3초를 썼다면 LLM은 최대 약 7초를 기다립니다.
  `llm_timeout`/`llm_error` 요청은 폐기하고 고정 안내문을 표시합니다.
- 실패 시 준비 상태를 해제합니다. 이전 클라이언트 HTTP 호출이 반환한 뒤 최소
  `llm.retry_seconds`(5초)를 기다리고 워밍업으로 복구합니다. 복구 전 발화는 재실행하지 않습니다.
  작업 스레드는 하나여서 대기시간 초과마다 새 스레드·추가 요청을 쌓지 않습니다.
- 워밍업의 소켓 제한은 별도 120초입니다. 소켓 제한은 무응답 시간 기준이므로
  느리게 응답하는 서버에서는 HTTP 호출 자체가 더 오래 남을 수 있습니다. 이때도
  일반 요청의 대기 기한은 별도로 지키며, 늦은 결과는 폐기합니다.
  서버 측 추론 취소까지 보장하는 구조는 아닙니다.
- `keep_alive: -1`은 **정수**로 설정합니다. Ollama가 요청 후 모델을 유지하도록 합니다.
  이전의 문자열 `"10m"` 설정 파일은 새 YAML로 교체하세요. 노드 종료 시 모델을
  일부러 내리지 않습니다. Ollama 재시작·명시적 unload·다른 모델의 메모리 사용까지 막지는 않습니다.
- `[LLM WARMUP]`, `[LLM READY]`, `[LLM ERROR]`, `[LLM RETRY]`로 상태를,
  `[LLM TIME]`으로 load/prompt evaluation/generation 시간을 확인합니다.
  화면 안내 문장은 `messages.llm_preparing`, `messages.llm_ready`,
  `messages.llm_failure`에서 관리합니다. TTS나 LLM 응답 문장 생성은 사용하지 않습니다.

이번 변경은 시작 비용을 준비 단계로 옮깁니다. cold 워밍업 자체를 없애지는 않습니다.
또한 일반 요청 처리 중 들어온 stop은 앞선 처리와 자신의 STT를 기다릴 수 있습니다.
오디오 정지 감지와 화자 인증은 별도 후속 단계입니다.

### 녹음 저장 선택

- `save_recordings: false` (기본): 파일이나 저장 디렉터리를 만들지 않고 메모리에서 전사.
- `save_recordings: true`: 처리할 발화를 `recordings_dir`에 WAV로 보관.
  전사는 저장된 파일을 다시 읽지 않고 동일한 메모리 입력을 사용합니다.
- 저장 경로·권한·디스크 오류는 `[RECORDING ERROR]`로 알리고 전사는 계속합니다.
- `[AUDIO] sequence=... recording=disabled/failed/파일경로`로 저장 상태를 확인합니다.
  결과의 `audio_path`는 미저장/실패 시 null이며 `recording_saved`, `recording_error`도 제공합니다.
- WAV에는 원래 PCM을 저장합니다. 파일 입력을 사용하는 기존 평가 도구도 계속 지원합니다.
  독립 평가 스크립트의 명시적 저장 동작은 노드의 `save_recordings` 설정과 별개입니다.

### 대기열과 오래된 명령 처리

- `capture.queue_capacity`: 처리 중인 발화를 제외한 대기열 크기. 기본 2개입니다.
  가득 차면 가장 오래된 미처리 발화를 제거하고 `[DROPPED] ... reason=queue_full`을
  기록합니다. 제거된 발화는 WAV로 저장하지 않습니다.
- `capture.max_fetch_age_seconds`: VAD 종료부터 fetch 발행까지의 최대 시간.
  기본 10초이며 대기·STT·LLM 시간이 모두 포함됩니다. 판정 전과 발행 직전에
  검사합니다. LLM 요청 대기도 남은 유효시간으로 제한하고 늦은 응답은 폐기합니다.
- 오래된 발화도 먼저 전사해 stop을 검사합니다. 전사문에서 확인된 stop은
  위 시간 제한을 적용하지 않고 LLM을 건너뜁니다.
- stop을 판정한 시각 이전에 시작한 발화는 이후 fetch로 발행하지 않습니다.
  아직 녹음 중인 발화의 pre-roll도 포함하며, 그 발화의 stop 검사는 유지합니다.
- 종료 후에는 진행 중 STT·LLM의 결과가 나와도 발행하지 않습니다. 마이크 EOF나
  2초 PCM 무응답도 오류로 처리하고 입력 및 발행을 중단합니다.
- `[TIME] queue_wait`는 녹음 종료 후 처리 시작까지의 시간입니다.
  `voice_end_to_command`는 VAD 종료부터 판정까지이며 종료 무음은 별도입니다.

**현재 한계:** 상시 녹음은 즉시 정지 감지가 아닙니다. STT·LLM은 순차 처리하므로
뒤에 들어온 stop도 기다릴 수 있고, 과부하로 제거된 미전사 발화는 stop인지 알 수
없습니다. 오디오 전용 정지 감지는 후속 단계입니다. WAV를 보관하는 수는 실제
말한 횟수와 같지 않을 수 있으므로 평가에서 대기열 제거 로그도 함께 확인하세요.

## 지원 명령

가져올 수 있는 물체는 네 종류입니다.

```json
{"action":"fetch","object":"coke"}
{"action":"fetch","object":"vaseline"}
{"action":"fetch","object":"tissue"}
{"action":"fetch","object":"airpod"}
```

플랫폼 정지 명령:

```json
{"action":"stop","object":""}
```

지원하지 않거나 모호한 명령:

```json
{"action":"unknown","object":""}
```

`unknown`은 ROS topic으로 발행하지 않습니다. `stop`은 `/voice/robot_command`에
발행하며, 실제 action 취소·베이스 정지·매니퓰레이터 hold는 stop을 지원하는
Jetson Control Node가 수행합니다.

## 의미 분류 기준

| 의미 | 결과 |
| --- | --- |
| 목·입안의 갈증, 마실 음료 필요 | `coke` |
| 입술·손·피부의 건조함, 보습 필요 | `vaseline` |
| 휴지 요청, 액체·소스·얼룩을 닦아야 함 | `tissue` |
| 에어팟·무선 이어폰·청취 도구 필요 | `airpod` |
| 미지원 물체, 다중 물체, 모호한 참조, 기타 제어 | `unknown` |

여러 지원 물체를 동시에 요청하면 한 물체를 임의로 선택하지 않고 `unknown`으로
처리합니다.

## 정지 판정 (LLM 미사용)

`stop`은 LLM이 판단하지 않습니다. `transcript_rules.py`가 전사문을 정규화한 뒤
(공백·문장부호 제거, 소문자, "멈쳐/멈처 → 멈춰", "스탑 → 스톱" 같은 오인식 보정)
아래 키워드가 **문장 어디에든** 있으면 바로 `stop`을 발행합니다.

`멈춰` `멈추` `멈춤` `정지` `스톱` `stop` `중단` `움직이지마` `그만움직` `그대로서`

- 정지를 놓치면 위험하고, 잘못 멈추면 불편한 정도입니다. 그래서 "음악 정지",
  "멈추지 말고 …"처럼 다른 뜻으로 쓴 경우에도 `stop`으로 처리합니다.
- LLM 출력 스키마에는 `stop`이 없습니다. 전사문 stop 판정은 Ollama를 호출하지 않습니다.
  Whisper 준비 후에는 Ollama 연결 실패·워밍업 중에도 전사문 정지를 처리합니다.
- 키워드를 바꾸면 `test/test_transcript_rules.py`(정지 문장 전부 감지, 일반 문장 오정지 0)를
  통과해야 합니다.

## 패키지 구조

```text
src/voice_command_pkg/
  voice_command_pkg/
    voice_command_node.py   ROS2 노드: 파이프라인을 작업 스레드에서 돌리고 결과를 발행
    pipeline.py             상시 VAD 생산자 + 메모리 STT/LLM 소비자 (ROS 의존 없음)
    audio_samples.py        little-endian PCM16 → 정규화 float32 배열
    utterance_buffer.py     제한된 발화 대기열, 순번·녹음 시각 전달
    audio_capture.py        PulseAudio 녹음, WebRTC VAD 상태 머신, VadConfig
    command_classifier.py   Ollama 분류기, 출력 스키마·허용 조합 검증
    prompts/system_prompt.txt  분류기 시스템 프롬프트 (바꾸면 회귀 테스트 필수)
    command_protocol.py     발행 허용 규칙 (fetch 4종, stop만 통과)
    robot_feedback.py       로봇 상태·응답 대기·피드백 판정 (ROS 의존 없음)
    parameters.py           ROS 파라미터 이름·기본값 → PipelineConfig
  config/voice_command.yaml 실험값 (코드 수정 없이 변경)
  launch/voice_command.launch.py
  test/                     pytest + ament lint
tools/voice_eval/           분류기 회귀·평가 스크립트, 평가셋, 결과 기록
voice_env.sh                노트북 실행 환경 (ROS + .venv)
```

## 준비 (노트북)

```bash
cd ~/2026project
python3 -m venv .venv                       # 처음 한 번
.venv/bin/pip install -r src/voice_command_pkg/requirements.txt
ollama list                                  # qwen3:4b-instruct 확인

source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-up-to voice_command_pkg
```

ROS 실행 파일은 `/usr/bin/python3`로 실행됩니다. Jetson의 `eated_pane.sh`처럼
`voice_env.sh`가 `.venv`의 site-packages를 `PYTHONPATH`에 추가합니다.
`pip install rclpy`는 하지 않습니다.

## 실행

```bash
source ~/2026project/voice_env.sh
ros2 launch voice_command_pkg voice_command.launch.py
```

`[LISTENING]`은 마이크 수집 시작을 뜻합니다. 일반 요청은 `[LLM READY]` 이후 새로
말합니다. 전사문 정지는 LLM 준비 전에도 처리합니다. 종료는 `Ctrl+C`입니다.

- 발행 없이 확인만:
  `ros2 run voice_command_pkg voice_command_node --ros-args -p publish_commands:=false`
- 다른 실험값: YAML을 복사해 고친 뒤 `config_file:=/path/to/my.yaml`
- VAD만 확인: `ros2 run voice_command_pkg vad_monitor` (발화 시작·끝만 표시)
- 녹음 저장은 기본으로 꺼져 있습니다. 평가용으로 켜려면 실행 인자에
  `-p save_recordings:=true`를 추가합니다. 기본 저장 위치는 `~/2026project/recordings/`이며
  WAV는 커밋하지 않습니다. 기존 녹음은 자동 삭제하지 않습니다.

노트북에서 명령 발행 없이 녹음도 보관하는 예:

```bash
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file ~/2026project/src/voice_command_pkg/config/voice_command.yaml \
  -p publish_commands:=false -p save_recordings:=true
```

**주의:** Jetson이 켜져 있고 `ROS_DOMAIN_ID`가 같으면 `fetch`/`stop`이 실제
로봇에 전달됩니다. 로봇 없이 시험할 때는 `publish_commands:=false`를 쓰세요.

## 테스트

```bash
# 저장소 루트에서: ROS 라이브러리는 읽지만 노드/로봇/마이크는 실행하지 않음
source /opt/ros/humble/setup.bash
source install/setup.bash
cd src/voice_command_pkg
PYTHONPATH="$PWD:$PYTHONPATH" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest test -q

# 저장소 루트의 별도 터미널에서: colcon 검증
source /opt/ros/humble/setup.bash
source install/setup.bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 colcon test --packages-select voice_command_pkg
colcon test-result --verbose
```

이 노트북에서는 `~/.local`의 anyio pytest 플러그인이 시스템 pytest와 맞지 않아
`colcon test` 앞에 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`이 필요합니다.
ROS의 launch_testing과 다른 pytest 버전 조합에서도 플러그인 로딩 오류가 발생할
수 있어 위 명령에서 자동 로드를 끕니다. 테스트는 가짜 발행자와 가짜 모델,
합성 PCM pipe를 사용합니다. 실제 ROS 명령은 발행하지 않습니다.

상시 녹음의 실제 마이크 확인은 사람이 `publish_commands:=false`로 실행합니다.
첫 명령 처리 중 두 번째 문장을 말해 `[VOICE START]`/`[VOICE END]`가 계속 나오는지,
최초 1.2초 대기가 반복되지 않는지, Ctrl+C 후 늦은 발행이 없는지 확인하세요.

개발용 WAV의 파일/메모리 입력 비교(실제 Whisper 사용, ROS·Ollama 호출 없음):

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/compare_stt_inputs.py \
  recordings/개발용_녹음.wav --output log/stt_input_comparison.json
```

명시한 16 kHz mono PCM16 파일만 읽고, 캐시된 small/CPU/int8 모델을 사용합니다.
held-out 자료에는 실행하지 마세요. 출력은 파일 해시·입력 샘플 동일성·전사문·시간을
포함하므로 로컬 `log/`에 보관합니다. 기존 결과 파일을 덮어쓰지 않습니다.

LLM 워밍업·유휴 후 처리·연결 실패 시 정지 확인(개발용 WAV, ROS 발행 없음):

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/measure_llm_readiness.py \
  --stop-wav recordings/개발용_정지.wav \
  --fetch-wavs recordings/개발용_콜라.wav recordings/개발용_휴지.wav \
  --output log/llm_readiness_check.json --idle-seconds 60
```

실행 전 `/api/ps`를 기록하며 모델을 강제로 unload하지 않습니다. 실제 Whisper/Ollama를
사용하지만 마이크를 열거나 ROS 노드를 만들지 않습니다. 연결 실패는 로컬 포트 1로
분리해서 확인하므로 정상 Ollama 서비스를 중단하지 않습니다. 측정은 녹음 파일의 PCM을
처리하기 시작한 시각부터이며 실제 발화·VAD 종료 무음·DDS·로봇 반응은 포함하지 않습니다.
과거 `measure_baseline.py`는 비교용으로 120초/10m 설정을 명시적으로 유지합니다.

실제 DDS를 사용한 상태/피드백 검증(로봇·마이크·Whisper·Ollama 불필요):

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
python3 tools/voice_eval/check_robot_feedback.py \
  --output "log/robot_feedback_check_$(date +%Y%m%d_%H%M%S).json"
```

도구는 localhost/domain 91과 매번 다른 시험용 토픽을 사용하며 모든 로봇 토픽을
remap합니다. VoiceCommandNode는 dry-run으로 생성하고 음성 worker는 시작하지 않습니다.
상태·결과 샘플만 보내고, 결과 검사를 위한 pending 상태는 메모리에서 설정합니다.
fetch/stop 메시지는 시험용 토픽에도 보내지 않습니다. 정상 기준은 17개 PASS와
JSON의 `passed: true`, `command_messages: 0`입니다.
실제 마이크/Jetson 확인 명령과 순서는
`tools/voice_eval/results/2026-11-11_robot_feedback.md`를 참고하세요.

분류기 회귀 테스트 (Ollama 사용, ROS 발행 없음):

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/test_llm_pick_objects.py
```

`test_command_classifier.py`는 프롬프트 SHA256을 고정해 둡니다. 프롬프트를
바꾸면 이 테스트가 실패합니다. 회귀 테스트를 다시 돌려 결과를
`tools/voice_eval/results/`에 기록한 뒤 고정값을 갱신하세요.

## ROS2 메시지

- Topic: `/voice/robot_command`, Type: `eated_interfaces/msg/RobotCommand`, QoS depth 10
- Fields: `header`(stamp = 발행 시각), `action`, `target_class`, `transcript`
- 통과 조건: `fetch` + `coke|vaseline|tissue|airpod`, 또는 `stop` + 빈 문자열.
  `unknown`과 그 밖의 조합은 발행하지 않습니다.

## 파라미터 (인식 기본값은 2026-11-04 기준선 유지)

전체 목록과 설명은 `config/voice_command.yaml`에 있습니다.

| 그룹 | 주요 값 |
| --- | --- |
| `save_recordings` | 기본 false; true일 때 처리한 발화의 WAV 저장 |
| `capture.*` | 대기열 2개, VAD 종료→fetch 발행 유효시간 10초 |
| `vad.*` | mode 3, start window 300 ms 중 60%, RMS gate 200, pre-roll 450 ms, 종료 무음 900 ms, 최대 15 s, 스트림 시작 시 한 번 1.2 s 버림 |
| `whisper.*` | `small`, CPU, `int8`, `ko` |
| `llm.*` | `qwen3:4b-instruct`, 일반 요청 10 s, 워밍업 120 s, 재시도 간격 5 s, 정수 `keep_alive: -1` (`think: false`, `temperature: 0` 고정) |
| `messages.llm_*` | 준비 중·준비 완료·실패 안내 템플릿 |
| `robot.*` | 응답 안내 제한 5 s, 연결 graph 검사 간격 0.5 s |
| `messages.robot_*` | 상태·수락·거절·정지·시험 모드 안내 템플릿 |

## YOLO 기반 객체 인식

음성 명령에서 선택된 물체를 카메라 영상에서 찾기 위해 별도 환경에서 YOLO11
객체 인식 모델을 학습하고 테스트했습니다. 하나의 모델이 다음 네 클래스를
탐지합니다.

```text
coke
vaseline
tissue
airpod
```

현재 음성 명령 시스템과 객체 인식 시스템은 각각 독립적으로 검증했으며, 아직
하나의 ROS2 실행 파이프라인으로 연결하지 않았습니다.

### 데이터셋

Roboflow에서 Object Detection 데이터셋을 구성하고 YOLO11 형식으로 변환했습니다.
학습, validation, 최종 test 데이터를 분리해 사용했습니다.

### 학습 환경

- Ubuntu 22.04
- NVIDIA GeForce RTX 5060 Ti 8 GB
- Python 3.11.16
- PyTorch 2.11.0 + CUDA 12.8
- Ultralytics 8.4.152
- Model: YOLO11n
- Input size: 640 × 640
- Epochs: 100
- Batch size: 16

학습 명령:

```bash
yolo detect train \
  model=yolo11n.pt \
  data=$HOME/yolo_ws/datasets/coke_tissue_airpod_vaseline/data.yaml \
  epochs=100 \
  imgsz=640 \
  batch=16 \
  device=0 \
  patience=20 \
  project=$HOME/yolo_ws/runs \
  name=coke_tissue_airpod_vaseline
```

학습된 weight 위치:

```text
~/yolo_ws/runs/coke_tissue_airpod_vaseline/weights/best.pt
```

### Test 결과

학습에 사용하지 않은 test 이미지 151장으로 평가했습니다.

| Class | Precision | Recall | mAP50 | mAP50-95 |
| --- | ---: | ---: | ---: | ---: |
| All | 0.952 | 0.924 | 0.949 | 0.823 |
| airpod | 0.959 | 1.000 | 0.995 | 0.796 |
| coke | 0.965 | 0.839 | 0.951 | 0.822 |
| tissue | 0.998 | 1.000 | 0.995 | 0.938 |
| vaseline | 0.884 | 0.857 | 0.855 | 0.734 |

전체 test 데이터 기준 `mAP50 = 0.949`, `mAP50-95 = 0.823`입니다.
`tissue`와 `airpod`은 높은 검출 성능을 보였고, `coke`와 `vaseline`은 일부
환경에서 미검출 또는 클래스 혼동이 발생할 수 있습니다.

### RealSense RGB 검출

```bash
yolo detect predict \
  model=$HOME/yolo_ws/runs/coke_tissue_airpod_vaseline/weights/best.pt \
  source=4 \
  conf=0.25 \
  show=True \
  device=0
```

RGB 영상에서 class, confidence, bounding box를 실시간으로 확인했습니다. 현재
확인된 대표적인 오분류는 다음과 같습니다.

- `airpod`을 `tissue`로 판단
- `vaseline`을 `airpod`으로 판단

향후 실제 로봇 카메라의 실패 사례를 데이터셋에 추가해 fine-tuning하고,
RealSense depth로 거리 또는 3차원 위치를 계산할 예정입니다.

## 저장소에서 제외되는 데이터

`.gitignore`는 다음 항목을 제외합니다.

- `.venv`와 Python cache
- 녹음 WAV 및 기타 오디오
- 로컬 model artifact
- 로그와 임시 파일
- `.env`와 credential 파일

실제 음성이 담긴 파일은 개인정보를 포함할 수 있으므로 공개 저장소에 commit하지
않습니다.

## 현재 범위와 다음 단계

현재 완료 범위:

```text
Laptop microphone
→ VAD
→ faster-whisper
→ Local LLM
→ validated fetch/stop command
→ eated_interfaces/msg/RobotCommand
→ ROS2 /voice/robot_command
```

아직 포함되지 않은 범위:

- Wi-Fi DDS 설정 및 검증
- TurtleBot/Nav2 이동
- OpenManipulator 물체 집기
- 음성 stop의 실제 하드웨어 종단 검증

다음 단계에서는 실제 로봇을 저위험 상태에 두고 음성 stop이 Jetson의 action 취소,
`cmd_vel = 0`, 매니퓰레이터 hold 및 `ERROR` 상태 전환까지 이어지는지 검증해야
합니다. 음성 stop만을 유일한 비상정지 수단으로 사용해서는 안 됩니다.


### 종단 지연 측정 (2026-11-12)

발행 성공 시 `[LATENCY]` JSON에 command_stamp_ns, 호스트/부팅별 monotonic 기준,
VAD 종료→publish 호출, 마지막 VAD 음성 프레임 읽기→publish 호출 시간을 기록합니다.
dry-run은 별도 event이며 실제 명령 시각을 만들지 않습니다. 이 시간은 실제 말끝/물리 정지 지연이 아닙니다.
`tools/voice_eval/record_latency.py`는 양쪽에서 기존 토픽만 구독하고,
`analyze_latency.py`는 시험당 명령 1개와 시계 근거를 확인한 뒤 지표를 생성합니다.
`ERROR`나 accepted 응답은 팔/바퀴의 정지 완료로 취급하지 않습니다. 실제 종단 정지는
한 영상의 말끝/팔/바퀴 정지 표식을 넣어 별도로 측정합니다. `summarize_latency.py`는
경로별 중앙값/p95/누락을 집계합니다. KWS 기본 off와 기존 안전 경로는 유지했습니다.
양쪽 실행 순서·동기화·영상 표식·정상 판정은
`tools/voice_eval/results/2026-11-12_end_to_end_latency.md`를 참고하세요.

## 화자 인증 — 등록·보정·검증 후 활성화 (2026-11-12)

> **2026-11-16 운영 선택:** 사용자가 추가 독립 녹음 검증을 생략하고 개발 기준으로
> 진행하도록 요청했습니다. 아래의 최초 절차는 일반 기본 절차이며, 이 노트북에서는
> 문서 끝의 `speaker_development.yaml` 실행 설정을 사용합니다.
> 독립 검증 완료로 표시하지 않습니다.


등록된 사용자의 `fetch`만 허용하는 선택 기능입니다. 현재 기본값은
`speaker.mode: "off"`입니다. 실제 본인/타인 데이터로 검증하기 전에는 켜지 않습니다.
`stop`은 모든 모드에서 화자 결과를 기다리지 않고 기존 규칙으로 처리합니다.
이 변경은 노트북 전용이며 Jetson 코드·공유 메시지는 변경하지 않습니다.

- `off`: 기존 동작, 화자 모델 로드 없음.
- `shadow`: 발화별 결과를 `[SPEAKER]`에 기록. 가져오기 제한은 적용하지 않습니다.
- `enforce`: 검증된 프로필만 사용. 모델/프로필 오류·미준비·품질 미달·시간 초과·타인 판정은
  가져오기를 차단합니다. 실패한 요청을 나중에 자동 재실행하지 않습니다.
- STT 시작 직전에 별도 작업 스레드에 같은 PCM을 전달합니다. 화자 추론은 한 번에 한 건만
  유지하며, STT 후 추가 대기는 `speaker.wait_seconds`(기본 1초) 및 fetch 유효시간으로 제한합니다.
  승인된 fetch도 발행 직전에 기존 IDLE/유효시간/stop 무효화 검사를 다시 거칩니다.
- 코사인 유사도는 확률이 아닙니다. 등록 기본 임계값 0.75는 관찰용 임시값이며 운영 기준이 아닙니다.
- 프로필은 노드 시작 때 고정합니다. 파일을 바꿔도 실행 중 노드에 자동 반영하지 않습니다.
  등록/검증과 런타임의 모델 SHA256·전처리 설정이 같아야 합니다.

### 설치 (현재 개발 노트북은 설치 완료)

```bash
cd ~/2026project
.venv/bin/python -m pip install torch==2.6.0+cpu --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r src/voice_command_pkg/requirements-speaker.txt
.venv/bin/python -m pip install --no-deps resemblyzer==0.1.4
```

기존 `requirements.txt`의 `webrtcvad-wheels`가 `webrtcvad` 모듈을 제공합니다.
Resemblyzer 메타데이터가 요구하는 별도 `webrtcvad` 배포판을 중복 설치하지 않습니다.
따라서 `pip check`는 Resemblyzer의 배포판 의존성이 없다고 표시할 수 있습니다.
Resemblyzer 패키지에 포함된 모델 SHA256을 로드 전에 확인하며 런타임 다운로드는 없습니다.
ECAPA 비교용 `speechbrain==1.0.3`, `torchaudio==2.6.0+cpu`는 운영에 필요하지 않습니다.

### 1. 지금 할 일: 내 목소리 등록

기존 음성 노드를 Ctrl+C로 종료한 뒤 **노트북에서만** 실행합니다.
Jetson/eated/ROS2/Ollama를 켤 필요가 없으며 이 도구는 ROS 명령을 발행하지 않습니다.

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/record_speaker.py
```

화면의 20문장을 평소 말투와 실제 사용할 마이크 위치에서 한 문장씩 읽습니다.
Enter를 누른 뒤 **“지금 말씀하세요”** 표시를 보고 말합니다. 각 녹음은 6초이며 자동 종료됩니다.
내용을 잘못 읽으면 `r`로 다시 녹음할 수 있습니다. 무음·너무 짧거나 심하게 찌그러진
음성은 자동 재녹음을 요청합니다. 그동안 주변 사람은 말하지 않아야 합니다.
완료되면 `recordings/speaker_enrollment_*/manifest.json` 형식의 실제 경로와
`speaker_profiles/owner.json`을 출력합니다. 프로필은 아직 `calibrated: false`입니다.
중간 종료 시 프로필을 만들지 않으며, 기존 프로필은 덮어쓰지 않습니다.

정상 확인:

```bash
cd ~/2026project
.venv/bin/python -c "import json; p=json.load(open('speaker_profiles/owner.json')); print('등록 수:', len(p['enrollment_hashes']), '검증 완료:', p['calibrated'])"
```

정상 출력은 `등록 수: 20 검증 완료: False`입니다. 이것은 등록 완료이며 인증 검증 완료는 아닙니다.
원본 녹음과 프로필은 Git에서 제외되며, 새 파일은 사용자만 읽고 쓰는 권한으로 생성합니다.

### 2. 등록 이후: 기준값 보정과 별도 검증

각 명령은 새 녹음 폴더를 출력합니다. 다음 단계는 등록 결과를 확인한 후 진행합니다.
타인 녹음은 참여자의 동의를 받고, 각 세션에서 한 사람만 읽습니다.
실제 사용 환경과 비슷하게 녹음하며 등록에 쓴 파일을 다시 사용하지 않습니다.

```bash
# 내 목소리 10문장 — 기준값 결정용
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/record_speaker.py --purpose genuine-dev
# 동의한 다른 사람 10문장 — 기준값 결정용
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/record_speaker.py --purpose other-dev
```

아래 변수에는 위 명령이 출력한 `manifest.json`의 전체 경로를 입력합니다.

```bash
read -rp '본인 development manifest 경로: ' SPEAKER_GENUINE_DEV
read -rp '타인 development manifest 경로: ' SPEAKER_OTHER_DEV
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/calibrate_speaker.py calibrate \
  --profile speaker_profiles/owner.json \
  --genuine "$SPEAKER_GENUINE_DEV" --other "$SPEAKER_OTHER_DEV" \
  --output speaker_profiles/owner_development.json
```

개발 녹음에서 FAR(타인 수락률) 0%, FRR(본인 거부율) 10% 이하를 만족하는 임계값을
고정합니다. 점수가 겹쳐 기준을 만족하지 못하면 활성화하지 않습니다.
이후 별도 세션에서 `--purpose genuine-test`, `--purpose other-test`로 각각 10개 이상
새로 녹음하고, 고정된 임계값을 아래와 같이 검증합니다.

```bash
read -rp '새 본인 test manifest 경로: ' SPEAKER_GENUINE_TEST
read -rp '새 타인 test manifest 경로: ' SPEAKER_OTHER_TEST
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/calibrate_speaker.py validate \
  --profile speaker_profiles/owner_development.json \
  --genuine "$SPEAKER_GENUINE_TEST" --other "$SPEAKER_OTHER_TEST" \
  --output speaker_profiles/owner_validated.json
```

등록/개발/검증 간 PCM SHA256 중복은 거부합니다. 검증에서 품질 실패한 본인 음성도 거부율에
포함합니다. 같은 FAR/FRR 기준을 통과해야 `calibrated: true`가 됩니다.
소수 녹음의 통과는 다른 모든 사람·장소에서의 성능 보장이 아니므로 실제 환경 확인이 남습니다.

활성화는 이 결과를 확인한 **후** 별도로 안내합니다. `speaker.mode: enforce`와
검증 파일의 `speaker.profile_path`를 함께 지정해야 합니다. `shadow`는 보호 기능이 아니므로
관찰 시험에서는 기존 `publish_commands: false`를 유지합니다. ROS CLI로 off를 지정할 때는
YAML 불리언 변환을 피하려고 `-p "speaker.mode:='off'"`처럼 따옴표를 보존합니다.

시험 근거와 남은 항목은 `tools/voice_eval/results/2026-11-12_speaker_auth.md`를 참고하세요.


### 2026-11-16 사용자 선택: 개발 프로필로 실행

본인 9/10 허용·타인 0/10 허용의 개발 결과와 기준값 0.7646047983524492를 그대로
사용합니다. 사용자가 추가 녹음 검증 생략을 요청했습니다. 검증 상태 `calibrated: false`는
유지하며, `speaker.allow_development_profile: true`를 명시한 별도 설정에서만 허용합니다.
등록만 된 owner.json은 이 옵션을 켜도 사용할 수 없습니다. 유효한 개발 근거와 기존
모델/등록/품질 검사를 통과한 owner_development.json이 필요합니다.

노트북에서 기존 음성 노드를 종료한 뒤 실행합니다. Jetson은 기존 eated 실행과
ROS_DOMAIN_ID=37을 유지하며 복사/재빌드할 항목이 없습니다.

```bash
cd ~/2026project
source voice_env.sh
export ROS_DOMAIN_ID=37
export ROS_LOCALHOST_ONLY=0
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file src/voice_command_pkg/config/voice_command.yaml \
  --params-file src/voice_command_pkg/config/speaker_development.yaml \
  -p publish_commands:=true
```

준비 로그는 `[SPEAKER READY] mode=enforce calibrated=False development_override=True`입니다.
LLM READY/LISTENING 및 로봇 IDLE 상태가 함께 준비되면 가져오기 명령을 사용할 수 있습니다.
인증이 거부되거나 모델/프로필/시간 초과 오류가 나면 가져오기는 발행하지 않고,
stop은 누구의 목소리든 기존 경로로 처리합니다. 이 명령은 실제 Jetson 명령을 발행합니다.

일반 기본 설정은 off/allow_development_profile=false를 유지합니다.
위 두 번째 params 파일을 뒤에 적용하는 실행에서 사용자 선택을 반영합니다.


### ROS 실행 환경 사전 확인 (2026-11-16 수정)

등록 도구의 `.venv/bin/python`과 `ros2 run`의 `/usr/bin/python3`는 검색하는 시스템
라이브러리 범위가 다릅니다. Ubuntu의 coverage 6.2가 보이는 ROS 환경에서 numba 0.68.1이
`coverage.types.Tracer`를 참조해 화자 준비가 실패하는 문제를 확인했습니다.
requirements-speaker.txt에 coverage==7.6.1을 추가해 프로젝트 .venv에서 먼저 읽도록 합니다.
시스템 패키지는 제거하거나 수정하지 않습니다.

노트북 개발 환경에는 수정 버전을 설치했습니다. 새 환경은 다음 설치 후 확인합니다.

```bash
cd ~/2026project
.venv/bin/python -m pip install -r src/voice_command_pkg/requirements-speaker.txt
source voice_env.sh
/usr/bin/python3 tools/voice_eval/check_speaker_environment.py \
  --profile speaker_profiles/owner_development.json \
  --allow-development-profile
```

이 도구는 마이크·ROS 노드·명령 발행 없이 실제 ROS Python의 모델과 프로필 준비를 확인합니다.
정상 기준은 `[SPEAKER READY] mode=enforce calibrated=False development_override=True`와
`PASS: ROS Python model/profile ready`입니다. `.venv/bin/python`으로 실행하면 실제 ROS 환경
확인을 생략하지 않도록 오류로 종료합니다.

`[ROBOT STATUS] action=stop`은 Jetson에서 수신한 상태 응답입니다. 이 음성 노드가 명령을
발행했는지는 `[COMMAND]`, `[ROS] published`, `[LATENCY]`의 published 이벤트로 구분합니다.
다른 발행자나 Jetson 실행기의 정지 요청 응답도 음성 노드에서 표시될 수 있습니다.
