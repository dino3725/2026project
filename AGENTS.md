# AGENTS.md — EATED 서비스 로봇

이 파일은 Codex 같은 코딩 에이전트가 이 저장소에서 작업할 때 따르는 규칙입니다.
프로젝트 배경, 코드 분석 결과, 이번 학기 개발 계획은 `docs/PROJECT_CONTEXT.md`에 있습니다.
작업 전에 반드시 먼저 읽으세요.

## 프로젝트 한 줄 요약

한국어 음성 명령("목마른데 마실 거 가져다줘")을 노트북에서 로컬 STT/LLM으로 분류합니다.
ROS2로 TurtleBot3 + OpenMANIPULATOR-X(Jetson Orin)에 전달하면 로봇이 YOLO + RGB-D로
물체를 찾아 집고, 사용자에게 운반한 뒤 손바닥 신호를 받으면 놓습니다.

## 실행 환경 (두 대의 컴퓨터)

| 컴퓨터 | 역할 | 이 저장소에서 빌드하는 패키지 |
|---|---|---|
| 노트북 (Ubuntu 22.04, ROS2 Humble, i7-10510U, GPU 없음) | VAD → STT → LLM → 명령 발행, 결과 피드백 | `eated_interfaces`, `voice_command_pkg` |
| Jetson Orin (JetPack 6.2.3, CUDA 12.6, ROS2 Humble) | 하드웨어, MoveIt, 비전, 제어 상태 머신, 주행 | `voice_command_pkg`를 제외한 전부 |

- 노트북 빌드: `colcon build --symlink-install --packages-up-to voice_command_pkg`
  (실행 환경은 `source ~/2026project/voice_env.sh`. `.venv` site-packages를 `PYTHONPATH`에 추가합니다)
- Jetson 빌드: `colcon build --symlink-install --packages-skip voice_command_pkg`
- Python 3.10 기준입니다. 3.11+ 전용 문법을 쓰지 마세요. `audioop`은 3.10에서 사용 가능합니다.
- 워크스페이스 경로: Jetson `/home/fcsl/2026project`, 노트북 `~/2026project` (GitHub 레포 이름도 `2026project`).
- Jetson의 Python 추가 패키지(torch, ultralytics, mediapipe)는 `/home/fcsl/2026project/.venv`에 있고, `eated_pane.sh`가 `PYTHONPATH`에 추가합니다.
- ROS2 패키지 이름은 소문자로 시작해야 합니다. 워크스페이스 이름(`2026project`)을 패키지 이름이나 Python 모듈 이름으로 쓰지 마세요.
- 노트북의 음성 패키지는 프로젝트 `.venv`(faster-whisper, webrtcvad-wheels)와 ROS2 setup을 함께 source해서 실행합니다. `pip install rclpy`는 하지 마세요.

## 저장소 구조 (목표)

```text
src/eated_interfaces/    공유 메시지·액션 (유일한 출처, 복제 금지)
src/control_pkg/         [Jetson] 작업 상태 머신, Pick/Place
src/vision_pkg/          [Jetson] YOLO + 깊이 위치 추정, MediaPipe 손바닥
src/joystick_pkg/        [Jetson] F710 수동 주행
src/eated_navigation/    [Jetson, 신규] 지도, Nav2 파라미터, twist_mux, 베이스 정렬
src/voice_command_pkg/   [노트북, 신규] 기존 음성 스크립트를 ROS2 패키지로
tools/voice_eval/        분류기 평가셋과 결과 기록
docs/PROJECT_CONTEXT.md  배경, 분석, 계획
```

## 반드시 지킬 안전 규칙

이 코드는 실제 로봇 팔과 바퀴를 움직입니다.

1. **실제 로봇을 움직이는 명령은 실행하지 않습니다.** `ros2 launch`, `ros2 topic pub /voice/robot_command`, `/cmd_vel` 발행 등이 해당됩니다. 실물 시험은 사람이 합니다. 시험 절차를 제안하는 것은 괜찮습니다.
2. **비상정지 경로를 약화하지 않습니다.** Y 버튼과 음성 `stop`은 모든 액션 목표를 취소해야 합니다(`ControlNode._handle_emergency_stop`, `_cancel_active_goals`). 새 액션 목표(예: Nav2 `NavigateToPose`)는 반드시 `_send_goal`을 거쳐 `_active_goals`에 등록되게 하세요.
3. **stop 처리는 결정적이어야 합니다.** 정지 키워드는 LLM보다 먼저 규칙으로 판정합니다. LLM 결과에만 의존하는 stop 경로를 만들지 마세요.
4. **작업 취소 구조를 유지합니다.** `control_node`의 작업 스레드는 `operation_id`로 무효화됩니다. 긴 대기나 루프에는 `_ensure_active` / `_sleep_cancelable` / `_wait_future`를 사용하세요.
5. **`/cmd_vel`에 새 발행자를 직접 추가하지 않습니다.** 주행 명령은 `twist_mux` 우선순위(비상정지 잠금 > 조이스틱 > Nav2)를 거칩니다.
6. **조이스틱 안전장치를 제거하지 않습니다.** 스틱이 중립으로 돌아와야 주행을 재개하는 장치, 주행 권한(`/control/manual_drive_enabled`, `TRANSIENT_LOCAL`) 구조가 해당됩니다.
7. **팔 이동 기본값을 바꾸지 않습니다.** 팔 이동 속도·가속 배율(현재 0.10), 관절 자세, 그리퍼 값은 사용자의 명시적 요청 없이 바꾸지 마세요.

## 인터페이스 변경 규칙

- `src/eated_interfaces`의 `.msg`/`.action`을 바꾸면 노트북과 Jetson 양쪽이 동시에 다시 빌드되어야 합니다.
- 필드 추가는 PR 설명에 "인터페이스 변경"이라고 명시하세요. 기존 필드의 이름이나 의미 변경은 피하세요.
- 토픽 이름(`/voice/robot_command`, `/control/command_status`, `/control/state`, `/vision/open_palm`, `/find_object`)은 바꾸지 않습니다.
- 물체 클래스는 `airpod`, `coke`, `tissue`, `vaseline` 네 가지입니다. YOLO `best.pt`, `control.yaml`의 `supported_targets`, 음성 분류기 허용목록이 항상 일치해야 합니다.

## 코드 스타일과 테스트

- 기존 패키지에는 `ament_flake8`, `ament_pep257`, copyright 테스트가 있습니다. 새 코드도 통과해야 합니다. 작은따옴표를 쓰는 기존 스타일을 유지하세요.
- ROS에 의존하지 않는 계산 로직(도달 가능성 판정, 키워드 정규화, 정렬량 계산 등)은 `vision_utils.py`나 `command_protocol.py`처럼 순수 함수 모듈로 분리하세요. pytest로 테스트할 수 있어야 합니다.
- ROS가 필요한 로직은 기존 `test_command_start.py`처럼 `SimpleNamespace`로 노드를 대체해 단위 테스트합니다.
- 실행:
  - 순수 테스트: `python3 -m pytest src/<pkg>/test -q`
  - 전체: `colcon test --packages-select <pkg> && colcon test-result --verbose`
- 음성 분류기 프롬프트(`SYSTEM_PROMPT`)를 바꾸면 회귀 테스트를 다시 돌리고 결과를 `tools/voice_eval/results/`에 기록하세요. held-out 평가셋은 분류기를 동결한 뒤 한 번만 실행합니다(기존 방식: 동결 → SHA256 기록 → 1회 실행).

## Git

- 기능별 브랜치를 사용합니다. 이번 학기 작업이 발표 평가에 쓰이므로, 커밋은 작은 단위로 의미 있게 남깁니다.
- 노트북과 Jetson은 같은 원격 저장소를 씁니다. 한쪽에서 push한 뒤 다른 쪽에서 pull하고, 같은 파일을 양쪽에서 동시에 고치지 않습니다.
- 녹음 WAV, `.venv`, `build/`, `install/`, `log/`, 모델 캐시는 커밋하지 않습니다(`.gitignore` 참고).

## 소통

- 사용자와는 한국어로 대화합니다.
- 실물 시험이 필요한 변경은 끝에 "로봇에서 확인할 항목" 체크리스트를 붙여주세요.

## 이번 학기 확정 설계 규칙 (2026-11-04)

계획 전체는 `docs/PROJECT_CONTEXT.md` 5장에 있습니다. 구현할 때 아래 결정을 바꾸지 마세요.

1. **화자 인증은 stop을 막지 않습니다.** fetch는 등록된 사용자 목소리만 받고, stop은 화자와 무관하게 항상 받습니다.
2. **정지 판정 순서:** 정지 전용 키워드 감지 → 정규화한 전사문 키워드. LLM은 정지를 판단하지 않습니다(fetch와 unknown만).
3. **사용자 안내는 정해진 템플릿 문장을 화면에 표시합니다.** LLM으로 응답 문장을 만들지 않고, TTS는 쓰지 않습니다. 문장은 파라미터 파일에서 관리합니다.
4. **로봇이 `IDLE`이 아니면 fetch를 발행하지 않습니다.** stop은 언제든 발행합니다.
5. **Pick 보정은 좌표 사슬 순서를 따릅니다:** 오차 측정 → 카메라 장착 보정 → 팔 확인 → 인식 자세 → 깊이 → 보정 방향 → 물체별 보정 → 도달 판정 → 재시도. 물체별 보정값은 파라미터 파일에 둡니다.
6. **충돌 회피 복귀 경로는 이번 범위에서 제외했습니다.** 따라서 팔 속도·가속 배율 0.10을 낮추지도 올리지도 마세요.
7. **Nav2는 공개 패키지를 쓰고, `NavigateToPose` 목표는 반드시 `_send_goal`을 거칩니다.** 베이스 정렬도 `/cmd_vel`에 직접 발행하지 않고 `twist_mux` 입력을 씁니다.
8. **제외한 기능은 구현하지 않습니다:** wake word, LLM 되묻기, 작은 LLM 비교, TTS, 인식 안정성(중복 물체), 그리퍼 힘 조정, 음성 목적지 지정. 필요해 보이면 먼저 사용자에게 묻습니다.
