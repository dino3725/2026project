# 2026-11-11 짧은 스톱 연속 녹음 분석

## 결론

사용자가 제공한 50초 WAV에서 9개 발화 구간을 찾았다. 기존 설정은 8개를 녹음하고,
그중 하나를 “슈톡”으로 전사해 최종 전사문 정지는 **7/9**였다.
시작 비율 0.4 후보는 9개 모두 녹음하며 평소 속도의 앞 세 “스톱”을 모두 인식했지만,
다섯 번째 전사 오류는 남아 최종 정지는 **8/9**였다. 완전 해결로 기록하지 않는다.

운영 기본값 0.6은 유지한다. 0.4는 이 자료에서 유효한 **실시간 dry-run 시험 후보**다.
이번 자료의 발화 밖 구간에서 추가 시작은 없었지만, 한 사람·한 공간의 50초로 생활 잡음,
TV/대화 등에서의 불필요한 녹음과 명령 오전사까지 검증한 것은 아니다.

## 원본과 방법

- 파일: `recordings/stop_probe.oIcb6b/continuous.wav`, 50.0초, 16 kHz/mono/PCM16.
- SHA256: `a8dac70c0b49869076d379adf525e26c7e1f011cdb6ba6101bfe4e80f52c95b1`.
- 사용자에게 안내한 순서(평소 스톱 3회 → 천천히 스톱 3회 → 멈춰 3회)를 정답으로 가정했다.
  RMS 구간과 전사 결과가 이 순서에 부합한다. 아래 시각은 WAV 시작 기준이다.
- 먼저 성공한 VAD 시작과 독립적으로 RMS가 높은 구간을 찾아 9개를 잘라 전사했다.
  누락된 두 번째 구간도 직접 입력하면 “스톱”으로 전사됐다.
- 다음으로 실제 `pipeline.capture_utterances`에 WAV의 30ms 프레임을 입력했다.
  마이크 열기/읽기/닫기 함수만 단일 프로세스에서 대체했으며, 실제 VAD·pre-roll·종료 판단을
  그대로 실행했다. startup 1.2초 제외, pre-roll 450ms, 종료 무음 900ms를 포함한다.
- 얻은 PCM에 현재 small/CPU/int8/ko, beam 5, 빈 힌트, temperature `[0.0]`을 적용했다.
  정지 키워드 규칙까지만 평가했다. LLM과 ROS 발행은 실행하지 않았다.
- 이는 입력 프레임/분할/STT 재생 시험이다. 실시간 마이크, CPU 경합, 발화 대기열 지연,
  ROS 전달과 실물 정지 완료 시험은 아니다.

## 9개 발화별 결과

| 번호 | 에너지 구간(초, 대략) | 안내한 발화 | 기존 시작 시각 | 0.4 시작 시각 | 기존 전사 | 0.4 전사 |
| ---: | --- | --- | ---: | ---: | --- | --- |
| 1 | 12.27–12.69 | 평소 스톱 | 12.69 | 12.60 | 스톱 | 스톱 |
| 2 | 14.04–14.37 | 평소 스톱 | 누락 | 14.34 | 전사 단계 미도달 | 스톱 |
| 3 | 15.99–16.80 | 평소 스톱 | 16.32 | 16.26 | 스톱 | 스톱 |
| 4 | 18.75–19.80 | 천천히 스톱 | 19.05 | 18.96 | 스톱 | 스톱 |
| 5 | 21.45–22.35 | 천천히 스톱 | 21.72 | 21.66 | 슈톡 | 슈톡 |
| 6 | 24.00–24.81 | 천천히 스톱 | 24.30 | 24.24 | 스톱 | 스톱 |
| 7 | 26.85–27.69 | 멈춰 | 27.03 | 26.97 | 멈춰 | 멈춰 |
| 8 | 28.68–29.22 | 멈춰 | 28.92 | 28.86 | 멈춰 | 멈춰 |
| 9 | 30.72–31.17 | 멈춰 | 30.90 | 30.84 | 멈춰 | 멈춰 |

0.4 후보에서 STT는 약 1.73~1.92초였다. 녹음 종료 대기 0.9초와 실시간 대기열/ROS 시간은
이 수치에 포함되지 않는다. 전사문 정지가 즉시 정지나 정지 전용 오디오 감지기를 뜻하지 않는다.

## 두 번째 발화가 누락된 이유

기존 조건은 300ms의 창(30ms × 10개) 안에서 다음 두 조건을 모두 통과한 프레임 6개다.

1. WebRTC VAD가 음성으로 판단.
2. RMS가 200 이상.

두 번째 발화는 VAD 자체에서는 연속 7개 프레임이 음성이었다. 그러나 RMS까지 통과한
프레임은 4개뿐이었다. 즉, STT/LLM 이전의 **시작 지속시간과 음량 조건의 조합**에서 탈락했다.

| 프레임 시작(초) | RMS | VAD 음성 | RMS 200 통과 |
| ---: | ---: | --- | --- |
| 14.22 | 995 | 예 | 예 |
| 14.25 | 905 | 예 | 예 |
| 14.28 | 542 | 예 | 예 |
| 14.31 | 225 | 예 | 예 |
| 14.34 | 168 | 예 | 아니요 |
| 14.37 | 106 | 예 | 아니요 |
| 14.40 | 95 | 예 | 아니요 |

0.4는 4/10개, 즉 120ms 상당의 통과 프레임으로 시작한다. 마이크 증폭이나 STT 힌트가
아니라 발화 시작에 필요한 음성 프레임 수만 줄이는 후보이다.

## 후보 비교

전체 50초를 재생했고, startup 1.2초를 제외한 경우에도 아래 시작 수는 같았다.
발화 밖 추가 시작은 모든 후보에서 0개였다. 범위 대응은 위의 에너지 구간을 사용했다.

| 변경한 값 | 시작 수 | 전사문 정지 | 판단 |
| --- | ---: | --- | --- |
| 기존 ratio 0.6 / RMS 200 / mode 3 | 8 | 7/9 | 두 번째 시작 누락, 다섯 번째 STT 오류 |
| ratio 0.5만 | 8 | 미측정 | 누락을 해결하지 못함 |
| ratio 0.4만 | 9 | 8/9 | 실시간 시험 후보 |
| RMS 150만 | 8 | 미측정 | 누락을 해결하지 못함 |
| RMS 100만 | 9 | 8/9 | 잡음 게이트까지 낮추므로 우선 선택하지 않음 |
| VAD mode 2만 | 8 | 미측정 | 누락을 해결하지 못함 |
| VAD mode 1만 | 8 | 미측정 | 누락을 해결하지 못함 |

기존 27개 개발 WAV를 파일별 새 VAD로 재생했을 때 ratio 0.6과 0.4 모두 각 파일에
시작 1개, 총 27개였다. 이 확인은 시작 수 비교이며, 해당 27개의 분할 후 STT/LLM 정확도를
다시 측정한 결과는 아니다. 잘린 WAV 재생이 연속 스트림의 이전 상태까지 복제하지도 않는다.

다섯 번째 “슈톡”은 두 시작 비율과 RMS 100 후보에서도 유지됐다. 따라서 시작 비율만으로
이 오류를 해결할 수 없다. “슈톡”을 임의로 정지 동의어에 추가하지 않았다. 추가 정지 전용
오디오 감지나 전사 보완은 비정지 발화 오탐 검증과 함께 별도로 진행해야 한다.

`trailing_bytes_ignored: 640`은 맨 끝의 20ms(640 / 32000초)가 30ms 프레임에 못 미친다는
뜻이다. 약 14초에서 발생한 누락과 관계가 없으며 WAV 자체는 50초 모두 보존돼 있다.

## 도구 변경과 검증

`inspect_vad_audio.py`에 `--start-speech-ratio`, `--start-min-rms`, `--vad-mode`를 추가했다.
CLI 기본값과 운영 YAML은 유지한다. 새 CLI로 기존 8개, ratio 0.4의 9개 시작을 다시 확인했고
flake8을 통과했다. 운영 코드/모델/인터페이스 변경이 없어 이번에는 colcon 빌드를 다시 하지 않았다.

원시 결과와 실제 capture 함수 재생 스크립트는 `log/stop_probe_20261111/`에 있다.
`analysis.json`, `pipeline_replay.json`, `old27_vad.json`, `cli_baseline.json`, `cli_ratio04.json`을
보관했다. WAV와 로그는 커밋하지 않으며, 검증 기록/도구만 로컬 커밋하고 push하지 않는다.

오프라인 재현(마이크나 로봇을 사용하지 않음):

```bash
cd ~/2026project
VAD_REPORT_DIR=$(mktemp -d "$PWD/log/vad-check.XXXXXX")
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/inspect_vad_audio.py recordings/stop_probe.oIcb6b/continuous.wav \
  --start-speech-ratio 0.4 --output "$VAD_REPORT_DIR/ratio04.json"
```

정상 결과는 start/end 각각 9개이고, 기존에 없던 start 14.34초가 추가된다.

## 다음 노트북 확인

기존 음성 노드는 Ctrl+C로 종료한다. 아래 값은 이번 실행에만 적용되며 YAML을 바꾸지 않는다.
50초 연속 녹음을 다시 할 필요는 없다. 실제 노드에서 시작·전사·정지를 함께 확인한다.

```bash
cd ~/2026project
source voice_env.sh
VAD_CHECK_DIR=$(mktemp -d "$PWD/recordings/vad_check.XXXXXX")
echo "확인 로그: $VAD_CHECK_DIR"
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file "$PWD/src/voice_command_pkg/config/voice_command.yaml" \
  -p publish_commands:=false -p vad.start_speech_ratio:=0.4 \
  -p save_recordings:=true -p recordings_dir:="$VAD_CHECK_DIR" \
  2>&1 | tee "$VAD_CHECK_DIR/session.log"
```

1. LISTENING 후 평소 속도로 “스톱” 3번, “멈춰” 3번을 각각 3~4초 간격으로 말한다.
2. “콜라 가져다줘”, “안녕하세요”를 한 번씩 말한다.
3. 말하지 않고 30초 기다린다. 평소 작업 중 나는 키보드·의자 등 생활 소음도 관찰한다.
4. Ctrl+C 후 같은 터미널에서 아래 로그를 확인한다.

```bash
rg '\[(VOICE START|STT|COMMAND|TIME|ERROR)\]' "$VAD_CHECK_DIR/session.log"
echo "결과 위치: $VAD_CHECK_DIR"
```

정상 기준: 정지 6개가 각각 시작 → 올바른 전사 → `by=rule:`로 이어지고 `llm=0.000s`,
`[ROS] skipped`가 나온다. 일반 문장을 정지로 오인하지 않으며, 조용한 30초에서 불필요한
VOICE START/STT가 없어야 한다. 생활 소음에서 시작되면 그 결과도 그대로 기록한다.
Jetson이 꺼져 있을 때 일반 요청의 `robot_unknown`은 예상되는 상태 차단이다.

## 로봇에서 확인할 항목

- [ ] 이번 단계는 명령 발행을 끈 노트북 시험이다. Jetson 실행·재빌드·pull은 필요 없다.
- [ ] Jetson이 이미 정상 실행 중이면 아래 명령으로 관찰한다. 노트북 dry-run 동안 새 명령이 없어야 한다.

```bash
cd /home/fcsl/2026project
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=37
ros2 topic echo /voice/robot_command eated_interfaces/msg/RobotCommand
```

- [ ] 실제 정지 동작은 아직 재시험하지 않았다. dry-run 결과를 먼저 확인한 뒤 기존 피드백
  시험 절차에 따라 사람이 수행한다. 이번 결과를 물리적 정지 완료 보장으로 사용하지 않는다.
