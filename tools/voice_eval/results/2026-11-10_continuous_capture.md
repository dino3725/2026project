# 노트북 상시 녹음 구현 및 자동 검증 — 2026-11-10

## 범위

- 기준: main `13d3d7d`, 작업 브랜치 `feature/voice-continuous-capture`.
- 구현: PROJECT_CONTEXT 5.1 ①의 상시 녹음, 제한된 발화 대기열, 오래된 fetch 및 종료 후 발행 차단.
- 노트북 음성 패키지만 변경. 공유 메시지, Jetson 제어·비전·주행, 팔 기본값은 변경하지 않음.
- Whisper 모델, LLM 모델·프롬프트, VAD 시작 0.6/종료 900ms는 유지.
- 메모리 직접 전사·저장 옵션·화자 인증·상태 구독·오디오 전용 stop은 다음 단계.

## 변경 동작

기존에는 한 발화를 녹음하고 마이크를 닫은 뒤 STT/LLM을 처리했다.
이제 run_forever는 녹음 스레드를 시작하고, 현재 처리 스레드는 대기열을 소비한다.
마이크는 세션에 한 번 열고 초기 1.2초 버리기도 한 번 수행한다.
발화 사이에는 VAD 상태/프레임 버퍼만 초기화하고 스트림을 유지한다.

| 항목 | 동작 |
|---|---|
| 대기열 크기 | 처리 중인 1개 외에 최대 2개, YAML로 조정 |
| 과부하 | 가장 오래된 미처리 발화 제거, 순번과 queue_full 기록 |
| fetch 유효시간 | VAD 종료→발행 기본 10초, 대기/STT/LLM 포함 |
| 유효시간 초과 | 전사문 stop을 먼저 검사하고 일반 요청은 LLM 호출 생략; LLM 종료 뒤에도 다시 검사 |
| 확인된 stop | 유효시간과 무관하게 규칙으로 판단, LLM 호출 안 함 |
| stop 이전 발화 | pre-roll 포함 시작 시각이 stop 판정 이전이면 이후 fetch 발행 금지 |
| 새 발화 | stop 판정 이후 시작한 발화는 기존 명령 검증을 적용 |
| 종료 | 처리 중 STT/LLM이 나중에 반환돼도 발행 금지; ROS 발행과 종료에 공통 잠금 사용 |
| 입력 장애 | EOF·2초 무응답 시 녹음 실패 보고, 대기열 폐기, 처리 중 결과 발행 차단 |
| 자원 정리 | 읽기는 최대 100ms 간격으로 종료 확인; parec 종료/강제 종료 및 pipe 닫기 |
| 계측 | 발화 순번·시각, queue_wait, 제거 원인 기록 |

단일 발화 평가 도구가 쓰는 capture_utterance와 run_once는 기존 방식의 단일 시험
진입점으로 유지했다. 일반 ROS 노드는 run_forever를 통해 상시 녹음한다.

## 파일별 역할

- `audio_capture.py`: 종료 가능한 pipe 읽기, 무응답 제한, 프로세스/파일 핸들 정리.
- `pipeline.py`: 한 스트림의 연속 발화 생성기, 녹음 생산자/처리 소비자, 시간 및 stop 이후 발행 검사.
- `utterance_buffer.py`: 순번·PCM·시각을 가진 발화 객체와 제한된 thread-safe 대기열.
- `voice_command_node.py`: 종료와 ROS 발행의 직렬화, 종료 뒤 로그/오류 보고 차단.
- `parameters.py`, `config/voice_command.yaml`: queue_capacity와 max_fetch_age_seconds 추가.
- `test_continuous_capture.py`: 실제 pipe·합성 PCM·가짜 모델·스레드 동기화 검증.
- 기존 노드/설정 테스트: 종료 중 발행 차단 및 새 파라미터 전달 추가.
- README/PROJECT_CONTEXT: 현재 구조, 동작 한계, 재현 명령 갱신.

## 검증 결과

실제 마이크·Whisper/Ollama 추론·ROS 노드·로봇 명령 발행 없이 실행했다.
합성 PCM은 임시 디렉터리에만 저장됐으며 기존 녹음과 평가셋은 변경하지 않았다.

| 검증 | 결과 |
|---|---|
| colcon build --symlink-install --packages-up-to voice_command_pkg | eated_interfaces, voice_command_pkg 2개 성공 |
| colcon test --packages-select voice_command_pkg | 145 passed, 1 skipped |
| flake8 / pep257 | 통과, 위 통과 수에 포함 |
| copyright | 기존 skip 유지 |
| Python 3.10 AST 구문 검사 | 통과 |
| git diff --check | 통과 |

기존 120개 통과에서 25개 검사를 추가했다. 그중 새 연속 수집 테스트 파일은
21개이고, 설정 검사 1개와 노드 종료 검사 3개가 추가됐다.
기존 pytest 마커 및 flake8 의존성 경고는 남아 있으나 실패는 없다.

주요 시나리오:

1. 하나의 pipe에서 초기 프레임을 한 번만 버리고 서로 다른 두 발화를 수집.
2. 최대 발화 길이로 끊긴 뒤에도 같은 스트림에서 다음 발화 수집.
3. STT가 멈춰 기다리는 동안에도 두 번째 이후 발화 수집.
4. LLM이 멈춰 기다리는 동안에도 두 번째 이후 발화 수집.
5. 대기열 과부하 때 2번 발화 제거, 1·3·4번 처리, 스레드 정상 종료.
6. 입력 없는 pipe 종료 요청과 timeout, 불완전 프레임의 EOF 처리.
7. 마이크 장애가 STT 중 발생하면 해당 fetch를 발행하지 않고 원인 보고.
8. STT/LLM 도중 종료 요청이 오면 늦은 결과 발행 차단.
9. 20초 된 일반 요청은 제거하지만 같은 나이의 stop은 규칙으로 처리.
10. LLM 실행 중 fetch 유효시간이 초과되면 발행 직전에 차단.
11. stop 이전에 시작한 발화/pre-roll의 fetch를 무효화하고 이후 새 발화는 구분.
12. ROS 메시지 준비 중 종료 신호가 와도 실제 publish 호출 차단.

재현 명령(저장소 루트):

```bash
source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-up-to voice_command_pkg
source install/setup.bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 colcon test --packages-select voice_command_pkg
colcon test-result --verbose
```

## 남은 한계

- 상시 수집이 즉시 stop 처리를 보장하지 않는다. 앞선 STT/LLM이 끝나야 다음 발화를 전사한다.
- 가득 찬 대기열에서 버린 미전사 발화가 stop일 수도 있다. 오디오 전용 stop은 아직 미구현이다.
- 기본 10초 유효시간 때문에 cold LLM 요청도 폐기될 수 있다. 시간 제한을 늘리면 오래된 fetch가
  허용되는 범위도 늘어난다. 워밍업·LLM timeout 조정은 후속 단계에서 실측해야 한다.
- WAV 저장은 처리할 때 계속 수행한다. 제거된 발화는 저장되지 않으므로 전체 수집 횟수와 다를 수 있다.
- STT/LLM 자체를 실행 중 강제 취소하지는 않는다. 종료 후 결과와 ROS 발행을 차단하는 방식이다.
- Ollama 준비 확인은 아직 시작 시 필수다. Ollama 장애와 독립적인 stop 시작 경로는 후속 작업이다.
- 로봇 IDLE 확인과 화면 피드백은 아직 없다. 이 단계가 실제 운용 조건 전체를 완성한 것은 아니다.
- 합성 입력으로 동시성과 수명 관리를 검증했다. 실제 음성 인식률·실제 마이크 지연을 재측정한 결과는 아니다.
- LLM 프롬프트와 held-out 자료는 변경하지 않았고 평가를 재실행하지 않았다.

## 사람이 확인할 항목

노트북 단독 확인은 반드시 명령 발행을 끈다. 아래 명령은 절차로만 제공하며 자동 실행하지 않았다.

```bash
source ~/2026project/voice_env.sh
ros2 run voice_command_pkg voice_command_node --ros-args -p publish_commands:=false
```

- [ ] 첫 발화를 처리하는 중 두 번째 발화의 VOICE START/END가 기록되는지.
- [ ] 초기 1.2초 대기가 발화마다 반복되지 않는지.
- [ ] 연속 발화 시 queue_full 로그와 제거 순번이 이해 가능한지.
- [ ] Ctrl+C 후 ROS published가 나오지 않고 parec가 남지 않는지.
- [ ] 첫 cold 요청이 10초를 넘을 때 expired로 제거되는지, 이후 준비된 요청은 처리되는지.

로봇에서 확인할 항목(사람이 수행):

- [ ] 실물 시험 조건을 갖춘 뒤 음성 stop의 수신·취소·정지를 별도로 확인.
- [ ] stop 판정 이전에 녹음된 fetch가 나중에 실행되지 않는지 확인.
- [ ] 노트북 입력 중단/종료 이후 새로운 fetch가 발생하지 않는지 확인.
