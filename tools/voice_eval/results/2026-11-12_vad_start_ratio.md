# 2026-11-12 VAD 시작 비율 0.4 채택

## 근거와 범위

- 11/11 연속 녹음: 기존 6/10 조건에서 누락된 짧은 스톱이 4/10 조건에서 녹음됨.
- 실제 노드 로그 `recordings/vad_check.eaAniS/session.log`: 시작 8개, 스톱 3/3,
  멈춰 3/3 규칙 판정 성공, 일반 2개 정지 오인 없음, ERROR 없음, 명령 발행 없음.
- 로그의 `speech=4/10`으로 시험 설정을 확인했다. 일반 2개는 로봇 상태 미수신으로
  LLM 전에 차단됐으므로 이 시험은 일반 명령의 LLM 정확도 평가가 아니다.
- 11/12 사용자가 생활 소음 30초 시험도 문제없었다고 확인했다. 이 부분은 사용자 보고다.
  로그의 종료 시각이나 별도 연속 녹음으로 30초 구간을 직접 입증한 것은 아니다.

YAML, ROS 파라미터 기본값, 순수 VadConfig의 시작 비율을 0.4로 맞췄다.
mode 3, RMS 200, 시작 창 300ms, pre-roll 450ms, 종료 무음 900ms는 유지한다.
이제 `-p vad.start_speech_ratio:=0.4` 없이 실행해도 같은 시작 조건을 사용한다.
기존 조건으로 비교할 때는 실행 인자 `-p vad.start_speech_ratio:=0.6`을 사용한다.

느린 스톱이 “슈톡”으로 전사되는 별도 사례와 다양한 환경에서의 정지 신뢰성은 아직
해결되지 않았다. 이번 채택은 확인한 개발 환경의 시작 누락 개선이며 실물 정지 보장이 아니다.

## 검증

- `colcon build --symlink-install --packages-up-to voice_command_pkg`: 2개 패키지 성공.
- `colcon test --packages-select voice_command_pkg`: 264 passed, 1 skipped.
- `colcon test-result --verbose`: 265 tests, 0 errors, 0 failures, 1 skipped.
- 4개 통과 프레임의 시작, 3개 이하 프레임의 시작 차단, 이전 0.6 설정 복원 검증.
- 기존 skip과 pytest mark/deprecation 경고는 남아 있다. 실제 마이크나 로봇을 직접 실행하지 않았다.

## 다음 단계: 종료 대기 600ms 시험

시작 기준 0.4를 유지하고 종료 무음만 900ms에서 600ms로 바꿔 시험한다.
종료 판단 부분에서 300ms를 줄이는 후보이며 전체 응답 시간이 반드시 300ms 줄어든다는
뜻은 아니다. 문장 중간의 쉼에서 발화가 분리되는지와 정지 인식을 확인한 뒤 채택한다.
운영 기본 종료 무음은 아직 900ms다.

새 터미널 준비:

```bash
cd ~/2026project
source voice_env.sh
```

기존 음성 노드를 종료한 뒤 실행:

```bash
END_CHECK_DIR=$(mktemp -d "$PWD/recordings/vad_end600.XXXXXX")
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file "$PWD/src/voice_command_pkg/config/voice_command.yaml" \
  -p publish_commands:=false -p vad.end_silence_ms:=600 \
  -p save_recordings:=true -p recordings_dir:="$END_CHECK_DIR" \
  2>&1 | tee "$END_CHECK_DIR/session.log"
```

LISTENING 후 아래 문장을 평소 속도로 한 문장씩 말하고 결과를 기다린다.

1. 스톱
2. 멈춰
3. 콜라 가져다줘
4. 목마른데 마실 거 가져다줘
5. 입술이 건조한데 바세린 가져다줘
6. 휴지 가져다줘, 아니 멈춰

6번은 쉼표에서 평소처럼 짧게 쉬고 이어 말한다. 의도보다 길게 쉬거나 잘못 말한 경우
그 순서를 함께 알려준다. Ctrl+C로 종료한 뒤 같은 터미널에서:

```bash
grep -E '\[(VOICE START|STT|COMMAND|TIME|ERROR)\]' "$END_CHECK_DIR/session.log"
echo "결과 위치: $END_CHECK_DIR"
```

정상 기준은 각 문장이 의도한 단위로 인식되고 `end_silence=0.600s`가 나오는 것이다.
1·2·6번은 규칙 정지이며 LLM을 사용하지 않아야 한다. 일반 문장에 정지를 만들어내지 않아야
한다. Jetson 상태가 없으면 일반 요청의 `robot_unknown`은 예상되는 동작이다.
이후 원본/로그를 분석해 문장 분리와 전사 오류, 대기시간을 비교한다.

## 로봇에서 확인할 항목

- [ ] 이번 단계는 노트북 dry-run이다. Jetson 실행·재빌드·pull은 필요 없다.
- [ ] Jetson이 이미 실행 중이면 아래 관찰 명령에서 dry-run 발화로 새 명령이 나오지 않아야 한다.

```bash
cd /home/fcsl/2026project
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=37
ros2 topic echo /voice/robot_command eated_interfaces/msg/RobotCommand
```
