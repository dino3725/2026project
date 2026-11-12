# 2026-11-12 종단 지연 측정 도구

## 완료 범위와 아직 측정하지 않은 것

노트북의 구조화된 발행 시각 로그, 두 컴퓨터에서 실행하는 읽기 전용 ROS 관찰기, 단일 시험 분석기, 영상 수동 표식 분석, 반복 시험 집계를 구현했습니다. 실제 로봇 명령은 실행하지 않았습니다. **실제 말끝→바퀴·팔 정지 지연과 목표 중앙값 1.5초 충족 여부는 아직 측정하지 않았습니다.** 이전 AUDIO STOP/TIME 로그를 이 값으로 대체하지 않습니다.

기본 오디오 감지 모드는 off를 유지합니다. 오디오 감지 안정화 미완료는 기존 기록대로 남겨 두고, 우선 기존 전사문 정지 경로의 지연 기준선을 측정합니다. KWS를 켠 비교는 동일 조건의 별도 시험으로 진행해야 합니다. Jetson 제어 코드, 공유 메시지, 토픽 이름, 비상정지 동작은 바꾸지 않았습니다.

## 측정 구간과 해석

| 출력 | 시작 → 끝 | 포함하지 않거나 보장하지 않는 것 |
|---|---|---|
| `vad_end_to_publish_call_seconds` | 노트북 VAD 종료 → publish 호출 시작 | 종료 무음 900ms 이전 구간, 실제 로봇 반응 |
| `last_speech_frame_read_to_publish_call_seconds` | VAD가 음성으로 판정한 마지막 프레임을 읽은 시각 → publish 호출 | 실제 음향 말끝/마이크 버퍼 지연; 잡음도 VAD 음성일 수 있음 |
| `frame_read_to_publish_call_seconds` | 연속 KWS 입력 프레임 읽기 → publish 호출 | 실제 발화 끝 시각 |
| `publish_call_seconds` | 로컬 publish 함수 호출 시간 | DDS 전달·Jetson 수신 완료 |
| `publish_call_to_status_observed_laptop_seconds` | 노트북 publish 시작 → 노트북 관찰기의 상태 응답 수신 | 물리 정지 완료; 같은 호스트 부팅의 monotonic clock 사용 |
| `header_to_command_observed_jetson_seconds` | 명령 header.stamp → Jetson 관찰기 명령 수신 | control_node 내부 콜백 진입 시각과 다름 |
| `header_to_state_observed_jetson_seconds` | 명령 header.stamp → Jetson 관찰기의 새 상태 전환 수신 | 컨트롤러 내부 전환 시각, 물리 정지 완료와 다름 |
| `physical_stop_seconds` | 같은 영상의 말끝 → 바퀴·팔 중 늦게 멈춘 시각 | 사람이 두 정지 시점을 표시한 경우에만 계산 |

현재 control_node는 ERROR를 설정한 뒤 액션 취소·제로 속도·팔 hold를 요청합니다. 수락 응답도 물리적 정지 완료의 센서 증명이 아닙니다. 이미 ERROR이면 새로운 ERROR 전환이 없을 수 있습니다. 이런 시험은 상태 전환 지연이 미측정이며 0초가 아닙니다.

노트북의 `[LATENCY]`는 성공 발행과 dry_run을 구분하며 dry_run의 command_stamp_ns는 null입니다. 발행 실패에는 성공 로그를 만들지 않습니다. 로그 실패 때문에 stop 발행 성공이 실패로 바뀌지 않도록 측정 로깅을 격리했습니다. header.stamp는 메시지 준비 초기 대신 최종 발행 조건 검사 부근에서 생성합니다.

## 상관관계와 시계 검증

현재 RobotCommandStatus에는 요청 ID가 없고 String 상태 메시지에는 header가 없습니다. 따라서 **음성 노드 로그·관찰기 파일 하나당 명령을 정확히 1개만** 시험합니다. 명령은 header.stamp/action/target으로 양쪽 파일을 일치시킵니다. 상태 응답은 동일 action/target과 단일 컨트롤러 출처, 제한된 시간창으로 대응시키는 추정입니다. 시험 중 다른 음성 명령이나 조이스틱 상태 조작을 섞지 않습니다. 다른 원인의 ERROR까지 인과관계로 증명할 수는 없습니다.

명령 중복/누락, 여러 발행자, 상태 응답 중복, 초기 retained ERROR만 있는 경우, 관찰 순서상 새 전환을 확인할 수 없는 경우에는 해당 지표를 비웁니다. 노트북 trace와 관찰기는 hostname+Linux boot ID가 같아야 monotonic 시각을 비교합니다.

두 컴퓨터 간 시각 비교는 시작·종료의 chrony tracking 근거가 모두 있고, Leap status Normal, 정상 stratum, 외부 기준, 기준 갱신 30분 이내인 경우에만 허용합니다. 관찰된 wall/monotonic 차이가 10ms 넘게 바뀌거나 simulated time이면 거부합니다. 호스트별 오차 추정은 다음 식을 사용하고 두 호스트의 합이 20ms를 넘으면 교차 호스트 지연을 보류합니다.

`|System time| + Root dispersion + 0.5 × Root delay`

이 값은 [chrony 공식 문서](https://chrony-project.org/doc/4.4/chronyc.html)의 시계 오차 추정입니다. 참조 시계의 정확도에 대한 가정을 포함하며 절대 보증은 아닙니다. 시작/종료 스냅샷 사이의 모든 순간을 입증하지도 않습니다. 결과에는 추정 오차를 더하고 뺀 구간도 표시합니다. 동기화 미확인은 같은 호스트의 monotonic 측정이나 단일 영상 측정까지 막지는 않습니다.

## 자동 검증

- 노트북 2개 패키지 빌드 성공.
- 음성 패키지 **316 passed / 1 skipped**, 평가 도구 **49 passed**. flake8/pep257 통과, 기존 copyright skip입니다. 새 검사 코드의 들여쓰기 오류를 수정한 뒤 최종 검사를 통과했습니다.
- 격리 도메인 91/localhost-only에서 실제 rclpy 관찰기를 2초 실행했습니다. 정상 종료, 이벤트 0건, 명령 발행 0건입니다. 이 실행은 네트워크 지연 실측이 아닙니다.
- 기존 격리 ROS 피드백 회귀 17개 통과, 명령 발행 0건. 실제 노드의 dry_run trace 2개를 분석해 발행/물리 정지 지연을 생성하지 않는 것도 확인했습니다.
- 현재 노트북에는 chronyc가 없어 스냅샷에 도구 부재가 기록됐습니다. 설치나 시스템 시각 변경은 수행하지 않았습니다.
- 합성 이벤트로 CLI를 검증했습니다. 가짜 명령 도착 10ms/상태 관찰 20ms/상태 응답 30ms가 정확히 계산됐으며, 물리적 정지는 null로 남았습니다. **이 숫자는 실측값이 아닙니다.**
- 시계 오류·누락·노후 기준·중복 명령·다른 발행자·이미 ERROR·dry-run·물리 표식 누락·중복 집계 방지와 프레임 읽기 시각 기록을 검증합니다. 모델/프롬프트/held-out 평가는 변경하지 않았습니다.

## 실측 준비 — 두 컴퓨터

기존 Jetson 하드웨어/제어 환경이 정상 실행 중이라는 전제입니다. 이 문서는 새 bringup을 실행하지 않습니다. 실제 상태 전환을 측정하려면 시험 직전 ERROR가 아닌 정상 상태가 있어야 합니다. 이전 stop으로 ERROR라면 사용하던 사람이 수행하는 복귀 절차로 상태를 준비합니다. HOME 복귀는 팔을 움직입니다.

양쪽에서 먼저 확인합니다.

```bash
chronyc tracking
chronyc sources -v
```

chronyc가 없다면 시험 전에 각 컴퓨터에서 설치하고 다시 확인합니다.

```bash
sudo apt update
sudo apt install chrony
sudo systemctl enable --now chrony
chronyc tracking
chronyc sources -v
```

Leap status가 Normal이고 선택한 기준 소스가 있는지 확인합니다. 도구가 계산하는 두 컴퓨터 합산 오차 추정이 20ms 이하여야 교차 호스트 수치가 나옵니다. 측정 중 makestep이나 시계 변경은 하지 않습니다. 설치만으로 동기화 완료를 가정하지 않습니다.

Jetson에는 관찰기 파일 하나만 임시 복사하면 됩니다. 브랜치 병합이나 Jetson 패키지 재빌드가 필요하지 않습니다. 노트북에서:

```bash
cd ~/2026project
read -rp 'Jetson IP: ' JETSON_IP
scp tools/voice_eval/record_latency.py \
  "fcsl@$JETSON_IP:/tmp/eated_record_latency_20261112.py"
```

## 시험 1회 실행 순서

아래는 `latency_trial01`이라는 새 디렉터리를 쓰는 첫 시험입니다. 두 번째부터는 양쪽 이름을 trial02 등으로 바꿉니다. 분석/관찰 출력 파일은 덮어쓰지 않으며, 음성 로그도 기존 시험 파일에 덮어쓰지 않습니다.

### 1. 노트북 터미널 A — 음성 노드

기존 음성 노드는 먼저 종료합니다. 아래는 **실제 stop을 발행하는 시험**이며 KWS는 off로 유지합니다. 준비 후에도 아직 말하지 않습니다.

```bash
cd ~/2026project
source voice_env.sh
export ROS_DOMAIN_ID=37
mkdir -p recordings/latency_trial01
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file src/voice_command_pkg/config/voice_command.yaml \
  -p publish_commands:=true \
  -p audio_stop.mode:=off \
  -p save_recordings:=true \
  -p recordings_dir:="$PWD/recordings/latency_trial01" \
  2>&1 | tee recordings/latency_trial01/voice.log
```

`LLM READY`/`LISTENING`과 로봇의 현재 상태를 확인합니다. 준비 후 시계·네트워크 관찰을 시작하여 cold warmup 시간이 시험창을 소모하지 않도록 합니다. stop 자체는 LLM 준비와 무관합니다.

### 2. Jetson 터미널 — 읽기 전용 관찰

```bash
cd /home/fcsl/2026project
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=37
mkdir -p /tmp/eated_latency_trial01
python3 /tmp/eated_record_latency_20261112.py \
  --role jetson --seconds 60 \
  --output /tmp/eated_latency_trial01/jetson.json
```

READY 이후 현재 state 이벤트가 찍히는지 확인합니다. 이 도구는 명령 발행자가 없습니다. Jetson 제어 노드의 기존 구독/발행 상태를 관찰합니다.

### 3. 노트북 터미널 B — 읽기 전용 관찰 후 발화

```bash
cd ~/2026project
source voice_env.sh
export ROS_DOMAIN_ID=37
python3 tools/voice_eval/record_latency.py \
  --role laptop --seconds 40 \
  --output recordings/latency_trial01/laptop.json
```

양쪽 관찰기가 READY이고 40초 시험창이 남아 있을 때 “멈춰”를 **한 번만** 말합니다. 다른 명령/조이스틱 조작은 넣지 않습니다. 응답을 확인한 뒤 관찰기가 자동 종료할 때까지 기다립니다. 음성 노드도 Ctrl+C로 종료합니다. 이후 시험은 새 디렉터리/새 음성 로그로 진행합니다.

첫 시험은 정지 상태의 로봇으로 소프트웨어·네트워크 기록 연결을 확인할 수 있습니다. 이 경우 실제 움직임이 없으므로 물리적 정지 지연을 측정했다고 기록하지 않습니다. 바퀴·팔이 움직이는 실제 정지 시험은 사람이 기존 운영 절차로 준비하고 수행합니다.

### 4. 노트북에서 파일 수집·분석

Jetson 관찰기가 종료되어 JSON 쓰기를 마친 뒤, 노트북 터미널 B에서:

```bash
cd ~/2026project
read -rp 'Jetson IP: ' JETSON_IP
scp "fcsl@$JETSON_IP:/tmp/eated_latency_trial01/jetson.json" \
  recordings/latency_trial01/jetson.json
.venv/bin/python tools/voice_eval/analyze_latency.py \
  --voice-log recordings/latency_trial01/voice.log \
  --laptop recordings/latency_trial01/laptop.json \
  --jetson recordings/latency_trial01/jetson.json \
  --output recordings/latency_trial01/report.json
```

정상 연결 기준: 양쪽 command가 각각 정확히 1개이고 같은 command_stamp_ns, 음성 trace event=published, 단일 컨트롤러의 status_accepted=true. 동기화가 유효하면 Jetson 관찰 지연과 오차 구간이 나옵니다. 상태 전환이 없거나 시계 근거가 없으면 issues를 확인하고 해당 수치를 미측정으로 유지합니다. `physical_stop_seconds:null`은 영상 표식이 없는 상태에서 정상입니다.

## 실제 말끝→물리 정지: 한 영상에 담기

사용자의 명령 음성과 바퀴·팔 움직임을 같은 영상/오디오 시간축으로 기록합니다. 말끝, 바퀴 정지, 팔 정지를 사람이 확인합니다. 가변 프레임률 영상은 프레임 번호/fps로 단순 환산하지 말고 영상 재생 시각(PTS)을 사용합니다. 이미 정지해 있거나 가려져 판정할 수 없는 부분은 null로 둡니다. 실제 파일을 recordings/latency_trial01/trial.mp4로 저장한 예시:

```json
{
  "command_stamp_ns": 1234567890123456789,
  "video_file": "trial.mp4",
  "speech_end_seconds": 5.20,
  "base_stopped_seconds": 6.10,
  "arm_stopped_seconds": 6.35,
  "time_resolution_seconds": 0.0334,
  "notes": "실제 관찰값으로 교체; 동일 영상 시간축, 다른 정지 조작 없음"
}
```

이는 형식 예시이며 실측 데이터가 아닙니다. command_stamp_ns는 report.json의 값을 넣습니다. 실제 표식을 video_markers.json으로 작성한 뒤:

```bash
.venv/bin/python tools/voice_eval/analyze_latency.py \
  --voice-log recordings/latency_trial01/voice.log \
  --laptop recordings/latency_trial01/laptop.json \
  --jetson recordings/latency_trial01/jetson.json \
  --video-markers recordings/latency_trial01/video_markers.json \
  --output recordings/latency_trial01/report_with_video.json
```

두 부분의 표식이 모두 있어야 physical_stop_seconds를 계산합니다. 표식 시간 해상도에 따른 구간은 표시하지만, 사람의 판독 오차나 영상의 음성/화면 동기 오차를 자동으로 검증하지는 못합니다. 결과에 영상·로그의 SHA256을 기록합니다.

## 반복 시험 집계

동일 경로와 조건에서 최소 여러 회 측정하고, 각 시험 디렉터리는 구분합니다. 원본 report와 report_with_video를 같은 명령의 두 시험으로 중복 입력하지 않습니다.

```bash
.venv/bin/python tools/voice_eval/summarize_latency.py \
  --reports recordings/latency_trial01/report_with_video.json \
  recordings/latency_trial02/report_with_video.json \
  recordings/latency_trial03/report_with_video.json \
  --output recordings/latency_summary.json
```

action/감지 경로별 중앙값·nearest-rank p95·최대와 측정/누락 건수를 표시합니다. 누락은 0초로 넣지 않습니다. 적은 횟수의 p95를 안정적인 분포 추정으로 해석하지 않습니다. 목표 중앙값 1.5초와 비교할 값은 실제 영상 표식의 physical_stop_seconds이며, VAD→발행 또는 ERROR 관찰 시간으로 대신 통과 판정을 내리지 않습니다.

### 로봇에서 확인할 항목

- [ ] 양쪽 ROS_DOMAIN_ID=37, 기존 제어 환경 준비, 음성 노드는 하나만 실행.
- [ ] chrony 시작/종료 근거와 오차 조건 확인; 준비 전에는 교차 호스트 수치 미측정.
- [ ] 한 시험에 명령 1개, 양쪽 header.stamp 일치, status_accepted 확인.
- [ ] 새 ERROR 전환 관찰과 물리적 정지 완료를 구분; 기존 ERROR는 전환 없음으로 처리.
- [ ] 실제 동작 시험에서는 사람이 모든 액션 취소·팔/바퀴 정지와 영상 표식을 확인.
- [ ] 이번 에이전트 실행은 로봇 명령/bringup/실물 동작 없이 빌드·자동 검사·읽기 전용 관찰만 수행.

## 검증 증거 SHA256

| 로컬 파일 | SHA256 |
|---|---|
| log/latency_20261112/build.txt | `9937c977ce23c713e9783f5bf356db20c59eb3ae2818f86801adac6f6e31be53` |
| log/latency_20261112/tests.txt | `1079f7b16aa181db26b1705e570efbd9f3e0dba7d88224edd99df15615ac5a10` |
| log/latency_20261112/observer_smoke.json | `d24035b008df6bc9a06c197f3bd2d18a0e49e9ef60a3b83a6089d1dc0345c78f` |
| log/latency_20261112/feedback_smoke.json | `aaa07a43b5ee557da84e9997be7d804688d0b1c805f087e97eefe8d396d2a60c` |
| log/latency_20261112/dry_run_report_verified.json | `6ebadfe6f80816fcf43abaa4ce5262bff67cbfb4d9ab34605f435aa08d353648` |
| log/latency_20261112/synthetic_report.json | `2c3931a754893067c00514be2992d507d4a15eda6872e7d688ea41c0faacfa81` |
