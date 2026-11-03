# PROJECT_CONTEXT — EATED 서비스 로봇 (2026 가을 텀 프로젝트)

이 문서는 기존 두 저장소의 코드 분석과 이번 학기 개발 계획을 정리한 것입니다.
코딩 에이전트와 같은 맥락에서 작업하기 위해 씁니다.
기준 시점: 2026-11-03. 노트북과 Jetson의 작업 상태가 각 저장소의 GitHub 최신 커밋과 같음을 확인했습니다.

## 1. 과제 요구사항과 일정

- 실제 하드웨어 기반 프로젝트. 발표는 개인별로, 본인이 맡은 역할을 중심으로 합니다.
- 11/5(목) 23:59: 제안 슬라이드 LMS 제출
- 11/6(금): 제안 발표 (1인 10분 + 질의응답)
- 11/20(금): 중간 점검 (진행 5분, 동작 영상 1개)
- 12/4(금): 최종 발표 및 시연
- 제안 발표에 들어갈 내용:
  - 프로젝트 소개, 전체 시스템 목적
  - 시스템 구성도 (하드웨어 + ROS2 노드·토픽 한 장)
  - 나의 역할 (입력, 출력, 맞물리는 지점)
  - 지금까지의 진행 (사진 또는 영상 1개), 이번 학기 구축 내용 (기존 결과와 본인 작업 구분)
  - 이슈, 개선 방향과 이유
  - 12/4 목표 (숫자로), 최종 시연 계획, 주차별 계획

## 2. 출처 저장소

| 저장소 | 내용 | 최신 |
|---|---|---|
| `kr28284-max/Eated_ws-2026` (main) | Jetson ROS2 워크스페이스: control/vision/joystick/interfaces | 2026-10-01 |
| `dino3725/local-voice-robot-command` (`ros2-command-integration`) | 노트북 음성 파이프라인 + ROS2 발행 | 2026-09-22 |

- 음성 저장소의 `main`은 옛 분류기(coke/tissue/snack, ROS 없음)라서 로봇과 호환되지 않습니다. 실제 최신은 `ros2-command-integration`입니다.
- 두 저장소의 `src/eated_interfaces`는 현재 바이트 단위로 동일합니다(md5 일치). 앞으로는 한 곳에서만 관리합니다.

## 3. 시스템 구성

```text
[노트북]
마이크 → WebRTC VAD → faster-whisper small (CPU int8, ko) → Ollama qwen3:4b-instruct (JSON)
      → 허용목록 검증 → /voice/robot_command (eated_interfaces/RobotCommand)
                                   │  Wi-Fi DDS
[Jetson]                           ▼
control_node (상태 머신)
  ├─ /find_object (FindObject 액션) → vision_node2: YOLO best.pt + aligned depth → base_link 좌표
  ├─ /vision/open_palm ← hand_gesture_node: MediaPipe, DRIVE_TO_PLACE에서만 동작
  ├─ /joy ← joy_node (F710: A 재시도, X HOME 복구, Y 비상정지)
  ├─ /move_action, /compute_cartesian_path, /execute_trajectory (MoveIt 2)
  ├─ /arm_controller/follow_joint_trajectory, /gripper_controller/gripper_cmd
  ├─ 발행: /control/state, /control/command_status, /control/target_class,
  │       /control/manual_drive_enabled, /control/release_countdown_sec
  └─ /cmd_vel (안전 루프의 0 속도) ── total_control도 /cmd_vel 발행
```

### 상태 머신 (control_pkg/task_state.py)

```text
IDLE → MOVE_TO_RECOGNITION → VISION_SEARCH → PICKING → MOVE_TO_CARGO
     → DRIVE_TO_PLACE (수동 주행) → [손바닥 2초] → PLACING → RETURN_HOME → IDLE
```

- 일반 실패: `ERROR` → 자동 HOME → `IDLE`. HOME도 실패하면 `ERROR`에 머물고 X로 재시도합니다.
- Y 버튼 또는 음성 stop: 모든 액션 취소, 팔 hold, `ERROR` 유지. X를 눌러야 복구됩니다.
- 수동 주행 허용 상태: `IDLE`, `DRIVE_TO_PLACE`, `ERROR`.

### 메시지

- `RobotCommand`: `header`, `action`(fetch/stop/unknown), `target_class`, `transcript`
- `RobotCommandStatus`: `header`, `accepted`, `action`, `target_class`, `message`
- `FindObject`: goal `target_class` / result `success, x, y, z, confidence, frame_id, message` / feedback `detected, stable_time, confidence`

### 주요 파라미터 (현재값)

- **VAD:** mode 3, 30 ms 프레임, 시작 창 300 ms 중 80%, 시작 RMS 200, 종료 무음 900 ms, pre-roll 450 ms, 최대 15초, 새 스트림마다 1.2초 버림
- **LLM:** `think: false`, `temperature: 0`, `seed: 0`, `num_predict: 32`, `keep_alive: 10m`, 타임아웃 120초
- **비전:** conf 0.5, 안정 감지 누적 2.0초, 최소 샘플 10, 인식 타임아웃 10초, 깊이 창 7, 최대 10 Hz, 프레임 나이 0.5초 이내
- **Pick:**
  - 파지점 = 검출 위치 + (0.02, 0, 0)
  - PREGRASP는 그 위 +5 cm, RETREAT는 최대 15 cm 수직 상승
  - MoveIt 속도·가속 배율 0.10, position-only IK
- **카메라:** eye-in-hand. `end_effector_link` → `d435i_link` = (−0.065, 0.0195, 0.052)
- **조이스틱:** 최대 0.20 m/s, 0.60 rad/s, 50 Hz 발행, 중립 복귀 필요
- **YOLO `best.pt`:** YOLO11n, Ultralytics 8.4.152, 클래스 airpod/coke/tissue/vaseline
  - 테스트 151장 기준 mAP50 0.949, mAP50-95 0.823
  - vaseline mAP50 0.855 / 재현율 0.857, coke 재현율 0.839
  - 알려진 오인식: airpod→tissue, vaseline→airpod

## 4. 분석에서 나온 문제 (중요도 순)

1. **음성 stop이 느리고 유실될 수 있습니다.**
   - stop도 VAD 종료 대기 0.9초 + Whisper + LLM을 모두 거칩니다. LLM은 웜 2.5~3.5초, 콜드 52.8초(옛 분류기 측정값)입니다.
   - `keep_alive: 10m`라서 10분 넘게 쉬면 모델이 내려갑니다.
   - 시작 시 워밍업 호출이 없습니다(모델 목록만 확인).
   - 발화 처리 중에는 마이크 스트림이 닫혀 있어, 그 사이에 한 말은 사라집니다.
2. **stop 이후에는 음성만으로 복구할 수 없습니다.** 음성 stop은 어떤 상태에서든 `ERROR`로 가고, 복구는 X 버튼으로만 됩니다.
3. **사용자 피드백이 없습니다.**
   - 음성 쪽이 `/control/command_status`와 `/control/state`를 구독하지 않습니다.
   - `command_status`는 수락 시점에만 발행되고, 이후 실패는 알려지지 않습니다.
4. **팔 복귀 경로에 충돌 검사가 없습니다.**
   - RETREAT 계획이 실패하면 상승 없이 CARGO로 관절공간 이동합니다(`test_no_executable_path_still_allows_cargo`로 의도된 동작).
   - 실패 시 자동 HOME과 X 복구도 `FollowJointTrajectory`로 직행합니다.
5. **현재 6분류의 평가가 부족합니다.**
   - 엄밀한 held-out 평가(160문장, 92.5%)는 옛 클래스 기준입니다.
   - 현재는 텍스트 64문장 회귀뿐이고, stop 17문장 중 10개가 프롬프트에 그대로 있습니다.
   - README의 "48/48"은 오래된 수치입니다.
6. **도달 가능성 판단이 없습니다.** 비전 좌표를 확인 없이 바로 MoveIt에 넘깁니다. 물체가 팔 범위 밖이면 계획 실패 → HOME으로 끝납니다. 지금은 사람이 로봇을 물체 앞에 놓아줘서 동작합니다.
7. **`/cmd_vel` 경합이 있습니다.** 수동 주행 권한이 꺼지면 `total_control`(50 Hz)과 `control_node` 안전 루프(20 Hz)가 0 속도를 계속 발행합니다. 이대로 Nav2를 붙이면 Nav2의 속도 명령이 덮어써집니다.
8. **파지점이 단일 상수입니다.** 바운딩 박스 중심의 깊이(물체 앞면)에 +2 cm를 더하는 방식을 네 물체에 똑같이 씁니다. 파지 실패 시 재시도도 없습니다.
9. **위생 문제가 있습니다.**
   - `ROS_DOMAIN_ID`가 미설정(기본 0)입니다.
   - `/home/fcsl/eated_ws`, `/home/sh/...` 경로가 하드코딩되어 있습니다.
   - LICENSE 파일이 없습니다(Ultralytics는 AGPL-3.0).
   - 음성 README에 오래된 문장이 남아 있습니다.

### 좋은 점 (유지할 것)

- `operation_id` 기반 작업 무효화, 액션 목표 일괄 취소
- 버튼 상승 에지 판정, `TRANSIENT_LOCAL` 주행 권한 전달, 스틱 중립 요구
- 3중 명령 검증: LLM 스키마 → Python 허용목록 → `validate_voice_command`
- 순수 함수 분리와 단위 테스트. ROS 없이 도는 18개 테스트가 통과함을 확인했습니다.

## 5. 이번 학기 개발 계획 (파이프라인 순서)

### 5.1 VAD — 노트북
- 녹음 스레드(스트림 1회 오픈, 상시 청취)와 처리 스레드를 큐로 분리합니다. 1.2초 버리기는 시작 시 1회만 합니다.
- 종료 무음(900 ms)을 500~600 ms로 낮춰 보고, 말 잘림과 지연을 실측해 정합니다.
- 화자 구분(wake word)은 범위 밖이며, 이슈로만 기록합니다.
- 지표: 처리 중 말한 stop 유실 0/20, 종료 감지 지연

### 5.2 STT — 노트북
- WAV 파일 없이 메모리 배열로 바로 전사합니다. 녹음 저장은 디버그 옵션으로 둡니다.
- `initial_prompt`에 로봇 어휘(콜라, 바세린, 휴지, 에어팟, 멈춰, 정지)를 넣습니다. 설치 버전이 `hotwords`를 지원하면 비교합니다.
- `beam_size`와 모델 크기(small vs base)를 바꿔 속도/정확도 표를 만듭니다.
- 전사문을 정규화합니다(공백 제거, "멈쳐"/"멈처" 같은 흔한 오인식 흡수).
- 지표: 발화당 STT 시간 중앙값, 실제 녹음셋 명령 단어 인식률

### 5.3 LLM — 노트북
- **stop 빠른 경로:** 정규화한 전사문에 정지 키워드가 있으면 LLM 없이 즉시 stop을 발행합니다.
- `keep_alive: -1`로 상시 로드하고, 시작 시 더미 문장 하나로 워밍업합니다. 타임아웃을 줄이고 실패하면 "다시 말해주세요"로 처리합니다.
- 현재 6분류로 held-out을 재평가합니다(동결 → SHA256 → 1회 실행). 평가셋은 실제 녹음을 STT로 돌린 결과로 만듭니다.
- 이유: stop 미탐은 위험하고 fetch 오탐은 불편한 정도입니다. 그래서 stop은 규칙으로, fetch는 LLM으로 처리합니다.
- 지표: stop 재현율 100%, 오정지 0, 웜 지연, 대기 후 첫 명령 지연, held-out 정확도

### 5.4 ROS2 토픽 — 노트북 (+ 공통)
- 스크립트를 `voice_command_pkg`(노드, launch, 파라미터 YAML)로 패키지화합니다.
- `/control/command_status`와 `/control/state`를 구독해 TTS나 화면으로 피드백합니다.
  - `DRIVE_TO_PLACE`에 도달하면 파지 성공입니다.
  - 그 전에 `ERROR`나 `RETURN_HOME`으로 가면 실패입니다.
- 로봇이 작업 중이면 fetch를 보내기 전에 "지금 작업 중이에요"라고 안내합니다.
- 종단 지연 측정: `header.stamp`를 Jetson 수신 시각과 비교합니다. 두 PC의 chrony 시간 동기화가 필요합니다.
- `ROS_DOMAIN_ID`를 명시 설정하고, 인터페이스는 단일 출처로 관리합니다.
- 지표: 발화 종료 → `ERROR` 전환 지연, 피드백 커버리지 100%

### 5.5 매니퓰레이터 — Jetson
- **우선 작업:** 도달 가능성 판정
  - MoveIt 호출 전에 `base_link` 좌표를 수평 거리와 높이로 1차 판정하고, 애매하면 MoveIt `plan_only`로 확인합니다.
  - 범위 밖이면 베이스 정렬을 요청합니다.
- **여유가 있으면:**
  - planning scene에 책상을 충돌 물체로 넣고, 복귀를 MoveIt 계획으로 합니다. 또는 RETREAT 성공을 CARGO 진입 조건으로 둡니다.
  - 물체별 파지 보정값을 둡니다.
  - 파지 실패 시 재인식 후 1회 재시도합니다.
- 지표: 정렬 전/후 Pick 성공률(물체별 10회), 실패 상황 충돌 0회

### 5.6 자율주행 — Jetson
1. `turtlebot3_manipulation_cartographer`로 지도를 만들고 위치에 이름을 붙입니다(픽업/전달/대기). 이 단계는 제공 패키지 사용으로 명시합니다.
2. `turtlebot3_manipulation_navigation2`로 단독 주행을 확인합니다.
3. `twist_mux`로 우선순위를 정합니다(비상정지 잠금 > 조이스틱 > Nav2). `total_control`과 `control_node`의 0 속도 연속 발행 방식도 바꿉니다.
4. `control_node`가 `NavigateToPose`로 `DRIVE_TO_PLACE`를 자동화합니다. 목표는 `_active_goals`에 등록되어 비상정지 시 취소되고, Nav2가 실패하면 수동 주행으로 넘어갑니다.
5. 팔 자세(HOME/CARGO, CARGO는 1번 관절 −73°)에 따라 footprint를 바꾸고, 물체를 들었을 때 속도·가속을 제한합니다.
6. **베이스 정렬 노드 (핵심 기여):**
   - 물체가 범위 밖이면 회전각 atan2(y, x)와 전진 거리를 계산합니다.
   - 오도메트리로 저속 이동한 뒤 다시 인식합니다(최대 2회).
   - Nav2 기본 도착 허용 오차(약 25 cm) 때문에 이 단계가 필요합니다.
- 지표: 전달 지점 도착 성공률과 오차(cm), 정렬 전/후 Pick 성공률, 비상정지 시 주행 취소 확인

Nav2 자체는 공개 자료를 쓴 기반으로 명시합니다. 본인 기여는 이 로봇 고유의 문제를 푼 부분입니다: `/cmd_vel` 권한 정리, 상태 머신 연동, 도달 가능성 판정과 베이스 정렬.

## 6. 진행 방식

- 이번 학기는 1인으로 진행합니다. 기존 두 저장소를 히스토리째 합친 새 저장소 `2026project` 하나를 노트북(`~/2026project`)과 Jetson(`/home/fcsl/2026project`) 양쪽의 워크스페이스로 씁니다. 실행 스크립트의 하드코딩 경로도 이 경로로 바꿨습니다.
- 발표에서는 기존 결과(합치기 전 커밋, 일부는 이전 팀원 작업)와 이번 학기 본인 작업(합친 뒤 커밋)을 구분해 표시합니다.
- 우선순위: 노트북 음성(5.1~5.4) → Jetson 자율주행과 도달 판정·베이스 정렬(5.6, 5.5 우선 작업) → 매니퓰레이터 안전·보정(5.5 여유가 있으면 항목).

## 7. 12/4 목표 (1주차 기준선 측정 후 확정)

| 항목 | 현재 | 목표 예시 |
|---|---|---|
| 음성 stop 지연 (발화 종료 → `ERROR`) | 실측 필요 | 중앙값 ≤ 1.5초 |
| stop 인식 / 오정지 | 미검증 | 20/20, 오정지 0 |
| 대기 후 첫 명령 지연 | 약 53초 | ≤ 4초 |
| 실제 음성 분류 정확도 | 텍스트만 | 녹음 held-out 60문장 ≥ 90% |
| 결과 알림 | 없음 | 수락·거절·완료·실패 100% |
| 전달 지점 자율 도착 | 없음 | 10회 중 9회 이상 |
| 자율 도착 후 Pick 성공률 | 없음 | 정렬 전 대비 향상 (수치 기록) |

## 8. 주차별 계획

| 주차 | 할 일 |
|---|---|
| 1주 (11/6~13) | 기준선 측정, SLAM 지도, `twist_mux` 설계, 레포 통합과 양쪽 환경 구축 |
| 2주 (11/13~20) | 상시 녹음, stop 빠른 경로, LLM 워밍업, Nav2 단독 주행 → 중간점검 영상 |
| 3주 (11/20~27) | ROS2 패키지화와 피드백, `control_node`·Nav2 연동, 도달 판정·베이스 정렬 |
| 4주 (11/27~12/4) | STT 비교, held-out 재평가, 전체 시나리오 반복 시험, 수치 정리, 리허설 |

## 9. 최종 시연 시나리오

1. "목마른데 마실 거 가져다줘" → "콜라 가져올게요" → 인식, 필요하면 정렬 → Pick → 사용자에게 자율 주행 → 손바닥 → 놓기 → 대기 위치로 복귀
2. 작업 중 "멈춰" → 즉시 정지, 화면에 지연 시간 표시
3. "오늘 날씨 어때" → "할 수 없는 명령이에요"
