# 로봇 상태 구독·발행 조건·화면 안내 — 2026-11-11

## 작업 범위와 현재 상태

노트북 `dev/laptop`의 기준 커밋 `5701e49` 위에 계획 5.4 ②·③을 구현했다.
Jetson 원격 브랜치 `origin/jetson/development`의 `e7ca1b2`를 읽어 비교했다.
공유 메시지, 상태 정의, 사용 토픽은 서로 맞으며 Jetson 코드/인터페이스는 변경하지 않았다.
원격 브랜치를 fetch하여 비교했지만 병합하지 않았다. 사용자 합의에 따라 검증된 이번
변경은 로컬 커밋으로 보관하고, GitHub push는 당일 노트북 개발 종료 시 한꺼번에 진행한다.

에이전트가 직접 수행한 검증은 자동 테스트와 격리된 DDS 통신 시험이다.
이후 사용자는 아래 실제 마이크 및 Jetson 연동 시험이 모두 정상이라고 보고했다.
ERROR 상태에서 가져오기 요청을 차단하는 시험 방법을 추가로 설명한 뒤 받은 확인이다.
실물 결과는 사용자 보고이며, 에이전트가 실물을 실행하거나 추가 로그를 분석한 결과와 구분한다.

## 구현 내용

### 상태·응답 판정

`robot_feedback.py`에 ROS 없는 판정 로직을 분리했다.

- 상태 미수신/미지원 상태, IDLE 이외 상태에서는 fetch를 차단한다.
- 현재 IDLE 구간이 시작된 시각보다 나중에 시작한 발화만 허용한다.
  pre-roll을 포함한 발화 시작 시각을 사용하므로 작업 중 들어온 요청은 IDLE 복귀 후에도 폐기한다.
- fetch 발행 직전에 pending을 등록한다. 응답이나 작업 상태 도착 전의 추가 fetch를 막는다.
- 수락 응답만으로 pending을 해제하지 않는다. 작업 상태를 거쳐 IDLE로 돌아왔을 때
  해제한다. 명시적 거절은 그 시각 이후의 새 발화부터 다시 검사한다.
- 5초 동안 수락/거절을 받지 못하면 안내한다. 시간 초과로 재전송하거나 자동으로
  제한을 해제하지 않는다. 늦은 응답도 안내에 사용하지 않는다.
- stop은 pending fetch를 대체할 수 있다. stop 뒤 실제 ERROR/복구/IDLE를 관찰하기 전에는
  남아 있던 IDLE만 보고 새 fetch를 허용하지 않는다.

### ROS 노드와 음성 파이프라인 연결

`voice_command_node.py`는 다음을 구독한다.

| 토픽 | 타입 | QoS |
| --- | --- | --- |
| `/control/state` | std_msgs/msg/String | reliable, transient-local, depth 1 |
| `/control/command_status` | eated_interfaces/msg/RobotCommandStatus | reliable, volatile, depth 10 |

- LLM 전과 발행 직전에 상태를 검사한다. 노드의 최종 발행 검사에서는 상태 갱신과
  같은 잠금을 사용하고, 대기 중 지난 fetch 유효시간도 다시 검사한다.
- 파이프라인의 publish callback에 발화 시각 정보도 전달한다. 시각 정보가 없는 fetch는
  발행하지 않는다. stop은 이 검사와 graph 조회를 건너뛴다.
- DDS graph에서 상태 발행자가 정확히 하나인지 0.5초 간격 및 fetch 검사 때 확인한다.
  연결 상실·발행자 교체·여러 발행자 감지 시 기존 상태/응답 연결을 버린다.
- 설치된 Humble의 구독 콜백은 메시지 인자 하나만 받는다. 이 API에 맞춰 구현했다.
  연결이 바뀌면 구독을 새로 만들고 세대 번호로 과거 콜백을 무시한다.
  재구독 후 현재 발행자의 보관된 상태를 받아야 fetch를 허용한다.
- 상태 메시지는 heartbeat가 아니므로 오래된 수신 시각만으로 IDLE을 무효화하지 않는다.
- dry-run에서도 상태를 검사하지만 실제 요청 대기나 성공 안내를 만들지 않는다.
  실제 publish 실패는 전달 여부 불명으로 취급하고 재전송하지 않는다.
- `std_msgs` 의존성을 명시했다. 모델·프롬프트·VAD·공유 메시지 변경은 없다.

### 화면 안내

`messages.robot_*` YAML 템플릿을 추가했다. 물체 자리에는 `{object}`만 허용한다.

- `robot_accepted`: 현재 요청과 맞는 수락 응답 뒤에만 “콜라 가져올게요.” 등 표시.
- `robot_carrying`: 수락된 대상과 DRIVE_TO_PLACE 상태를 확인한 뒤 한 번 표시.
  상태가 수락 응답보다 먼저 도착하는 경우도 처리한다.
- `robot_error`: ERROR만 보고 인식 실패로 단정하지 않고 상태 확인·복구를 안내.
- `robot_stop_sent` / `robot_stop_accepted`: 전송과 수락 안내를 구분.
  물리적 정지 완료를 확인한 메시지로 표현하지 않는다.
- ERROR/RETURN_HOME 뒤 늦게 온 fetch 수락으로 “가져올게요”를 표시하지 않는다.
- unknown, 준비 상태 미확인, 작업 중, 응답 대기, 응답 시간 초과, dry-run 안내 포함.

## 자동 검증 결과

```bash
cd ~/2026project
source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-up-to voice_command_pkg
source install/setup.bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 colcon test --packages-select voice_command_pkg --event-handlers console_direct+
colcon test-result --verbose
```

- 대상 패키지 2개 빌드 성공.
- 총 235개: **234 passed, 1 skipped, 0 failures**.
  skip은 기존 copyright 테스트다. flake8/pep257 통과.
- 기존 187개 통과 기준에서 47개 테스트 추가:
  순수 상태 정책 28개, ROS 노드 가짜 객체 검사 16개, 파이프라인 연결 검사 3개.
- 상태 변경 중 발행 차단, 잠금 대기 후 유효시간 재검사, stop의 상태/graph 독립성,
  dry-run 무발행, 발행 실패 후 제한 유지, 과거 구독 콜백 무시, 종료 후 무처리를 확인했다.
- Python 3.10 문법 29개 파일 검사 및 새 DDS 도구 flake8, `git diff --check` 통과.

## 실제 DDS 검증

재현 명령(ROS 환경과 빌드된 음성 패키지만 필요):

```bash
cd ~/2026project
source /opt/ros/humble/setup.bash
source install/setup.bash
python3 tools/voice_eval/check_robot_feedback.py \
  --output "log/robot_feedback_check_$(date +%Y%m%d_%H%M%S).json"
```

localhost/domain 91과 실행마다 고유한 `/feedback_check_<uuid>/...` 토픽을 사용한다.
로봇의 세 토픽을 모두 remap하고 실제 VoiceCommandNode는 `publish_commands=false`로
생성한다. 음성 worker는 시작하지 않는다. 시험 제어기는 상태·수락 결과만 보내고,
응답 검사에 필요한 pending은 메모리에 직접 설정한다. 시험 토픽에도 fetch/stop을 보내지 않는다.

**17개 검사 통과, 명령 메시지 0건.** 보관된 IDLE 수신, 상태보다 늦은 수락,
운반 안내, 과거 발화 차단, 응답 제한 타이머, 발행자 소멸/교체, 중복 발행자 차단,
정지 수락·복구를 실제 DDS 콜백으로 확인했다.

초기 시험은 Humble의 단일 인자 콜백과 맞지 않아 실패했다. 설치된 executor 소스를
확인해 API를 수정하고, 구독 재생성과 세대 번호 차단을 추가한 뒤 통과했다.
이 호환성은 단위 테스트에도 추가했다.
원본 JSON은 `log/robot_feedback_20261111/`에 보관하며 커밋에서 제외한다.

## 사용자 확인 절차

### 1. 노트북만으로 상태 미수신과 정지 판정 확인

Jetson 제어 노드가 꺼진 상태에서 노트북 터미널 A:

```bash
cd ~/2026project
source voice_env.sh
export ROS_DOMAIN_ID=37
FEEDBACK_CHECK_DIR=$(mktemp -d /tmp/eated-feedback-check.XXXXXX)
echo "로그 위치: $FEEDBACK_CHECK_DIR"

ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file "$PWD/src/voice_command_pkg/config/voice_command.yaml" \
  -p publish_commands:=false -p save_recordings:=false \
  2>&1 | tee "$FEEDBACK_CHECK_DIR/no_robot.log"
```

`[LISTENING]` 이후 “콜라 가져다줘”: `robot_unknown`으로 폐기, 상태 확인 안내가 정상.
이후 “멈춰”: `by=rule:멈춰`, `llm=0.000s`, `[ROS] skipped`가 정상.
이번에는 로봇 상태 없이 일반 fetch를 분류하지 않는 것이 의도한 동작이다.
Ctrl+C로 종료하고 터미널 A는 다음 시험에도 유지한다.

### 2. Jetson 준비와 상태 관찰

Jetson 개발 브랜치의 기존 빌드를 사용한다. 이번 변경 때문에 인터페이스를 다시 맞출
필요는 없다. 이미 로봇이 실행 중이면 중복 실행하지 않는다.
Jetson 그래픽 터미널에서:

```bash
cd /home/fcsl/2026project
export ROS_DOMAIN_ID=37
bash ./eated
```

1 하드웨어 → 2 MoveIt → 3 카메라 → 4 비전 → 5 조이스틱 드라이버 → 6 조이스틱 제어를
준비한다. 8 명령 구독과 9 상태 구독도 실행한다. **7 제어 노드 실행 시 팔이 HOME으로
움직인다.** 주변을 확인하고 실행한 뒤 startup 완료와 IDLE을 확인한다.

별도 Jetson 터미널에서 수락 결과 기록:

```bash
cd /home/fcsl/2026project
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=37
JETSON_FEEDBACK_LOG=$(mktemp /tmp/eated-feedback-status.XXXXXX.log)
echo "로그 위치: $JETSON_FEEDBACK_LOG"
ros2 topic echo /control/command_status eated_interfaces/msg/RobotCommandStatus \
  2>&1 | tee "$JETSON_FEEDBACK_LOG"
```

### 3. 연결된 dry-run

노트북 터미널 A에서 1번의 실행 명령을 다시 사용하되 로그 이름을
`"$FEEDBACK_CHECK_DIR/connected_dry_run.log"`로 바꾼다.
`[ROBOT STATE] ... -> IDLE`과 `[LLM READY]`를 모두 확인하고 약 1초 후
“콜라 가져다줘”를 말한다. fetch/coke 판정과 `robot_dry_run`, `[ROS] skipped`가 정상이다.
Jetson 8번에는 새 명령이 없어야 한다. 완료 후 Ctrl+C로 종료한다.

### 4. 로봇에서 확인할 항목

다음 명령은 실제 발행을 켠다. 로봇 주변을 확인하고 Y 버튼을 사용할 수 있는 상태에서
사람이 실행한다. 기존 노트북 시험 노드는 종료한 상태여야 한다.

```bash
cd ~/2026project
source voice_env.sh
export ROS_DOMAIN_ID=37
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file "$PWD/src/voice_command_pkg/config/voice_command.yaml" \
  -p publish_commands:=true -p save_recordings:=false \
  2>&1 | tee "$FEEDBACK_CHECK_DIR/robot.log"
```

| 순서 | 시험 | 정상 기준 |
| --- | --- | --- |
| 1 | LLM READY와 로봇 IDLE 확인 후 약 1초 뒤 “콜라 가져다줘” | fetch 전달, 수락 응답 뒤 `robot_accepted` |
| 2 | 작업 중 또는 DRIVE_TO_PLACE에서 “휴지 가져다줘” | `robot_busy`, 추가 fetch 무발행, Jetson 8번에도 추가 명령 없음 |
| 3 | 작업 진행 관찰 | 수락된 물체가 DRIVE_TO_PLACE가 되면 `robot_carrying` 한 번 |
| 4 | “멈춰” | 노트북 stop 발행, Jetson stop 수락·실제 정지·ERROR |
| 5 | ERROR에서 “휴지 가져다줘” | `robot_error`, fetch 무발행 |
| 6 | 주변 확인 후 X로 HOME 복구 | RETURN_HOME → IDLE, 과거 차단 요청의 자동 실행 없음 |
| 7 | IDLE 이후 새 요청 | 새 요청 정상 처리 |

정상 정지가 안 되면 Y 버튼을 사용하고 시험을 중단한다. stop 후 수동 주행 권한이
복구되므로 조이스틱 스틱은 중립으로 둔다. X 복구도 실제 팔 이동이다.
종료는 실제 정지 확인 → 노트북 Ctrl+C → Jetson 제어/주변 노드 종료 → 하드웨어 마지막 순서다.

노트북 터미널 A에서 로그 요약:

```bash
grep -H -E '\[(ROBOT STATE|ROBOT STATUS|ROBOT GATE|ROBOT CONNECTION|FEEDBACK|STT|COMMAND|ROS|TIME|DROPPED)\]' \
  "$FEEDBACK_CHECK_DIR"/*.log
pgrep -af 'voice_command_node|parec'
```

Jetson 결과 기록 터미널은 Ctrl+C 뒤 `cat "$JETSON_FEEDBACK_LOG"`로 확인한다.
시험용 음성 노드와 녹음 프로세스는 종료 후 남지 않아야 한다.

## 사용자 실물 확인 완료 — 2026-11-11

사용자 보고: “다 정상적으로 실행되는거 확인했어”. 안내한 시험 범위의 확인 완료로 기록한다.

- [x] IDLE에서 새 fetch 전달 및 수락 안내.
- [x] 작업 중 추가 fetch 차단 및 운반 상태 안내.
- [x] 음성 stop 전달, 실제 동작 정지 및 ERROR 상태 확인.
- [x] ERROR에서 가져오기 요청 차단.
- [x] X 버튼으로 HOME 복구 후 IDLE에서 새 요청 처리.

새 실측 시간이나 세부 로그는 추가 제출되지 않았으므로 수치 성능 결과는 추가하지 않았다.
이번 확인으로 모든 장애 상황이나 아래 프로토콜 한계까지 해소됐다는 의미는 아니다.

## 한계와 후속 단계

- 실제 네트워크 단절 감지는 DDS graph 발견 속도에 의존한다. 프로세스 정지/네트워크 장애를
  0.5초 이내에 반드시 감지한다는 의미는 아니다. 별도 heartbeat는 추가하지 않았다.
- 응답 메시지에는 요청 ID가 없다. action/target과 유일한 제어기 발행자를 기준으로
  현재 로컬 요청을 연결하며, 여러 명령 발행자의 동일 요청이나 같은 대상의 매우 늦은
  과거 응답을 완벽히 구분할 수 없다. 음성 명령 노드는 하나만 실행한다.
- 상태 depth 1 특성상 매우 빠른 중간 상태를 놓치면 pending 해제가 지연될 수 있다.
  확인 없이 제한을 자동으로 해제하지 않는다. 실제 상태 확인과 복구가 필요하다.
- stop은 로봇 상태 조건을 건너뛰지만 앞선 STT/LLM 및 자신의 전사를 기다릴 수 있다.
  오디오 정지 감지, 화자 인증, 시간 동기화 기반 실제 종단 지연 측정은 후속 단계다.
