# 비전 기준선 측정

현재 인식 자세를 유지한 채 `/find_object` 결과를 기록한다. 기록기는 팔·바퀴·그리퍼 명령을 보내지 않는다. 자세 준비는 별도 `measurement_control.py`가 기존 ControlNode의 액션 등록·취소 경로로 수행한다. 아카이브의 팔 측정 도구를 복원한 것이 아니다.

## 측정 전용 제어

- 시작 시 HOME 이동 없음. fetch·손바닥 놓기·A/X 작업 요청을 비활성화한다.
- 수동 주행 권한을 항상 끄고 0속도를 발행한다. 일반 `total_control`과 일반 `control_node`는 동시에 실행하지 않는다.
- 실제 Y 입력을 한 번 확인하고 버튼이 해제된 상태에서만 자세 준비 요청을 받는다.
- 최근 0.5초 이내의 조이스틱과 완전한 관절 상태가 필요하다. 이동 중 입력이 오래되면 취소와 정지를 요청한다.
- 준비 서비스는 기존 `recognition_pose_deg`로 한 번 이동한다. HOME·집기·그리퍼 명령 없이 그 자세를 유지한다.
- 실패·정지 시 자동 HOME 없음. `/measurement/ready`는 준비 완료 및 입력 신선도를 표시한다.
- 소프트웨어 취소 요청은 물리적 정지 보증과 다르다. 하드웨어 bringup 자체에도 초기 구동이 있을 수 있다.

현재 사용자가 환경 준비와 에이전트의 실제 실행을 승인한 세션에서 사용한다. 일괄 실행 스크립트는 제공하지 않는다. 각 단계의 연결·상태를 확인한 후 다음 요청을 보낸다.

```bash
source /opt/ros/humble/setup.bash
source /home/fcsl/2026project/install/setup.bash
cd /home/fcsl/2026project
python3 tools/vision_eval/measurement_control.py --ros-args --params-file src/control_pkg/config/control.yaml
```

위 프로세스를 유지한 상태에서 카메라·joy·하드웨어·vision2를 각각 시작한다. Y 입력 확인 및 관절 수신 후 다음 요청이 실제 팔을 움직인다.

```bash
ros2 service call /measurement/prepare_recognition std_srvs/srv/Trigger '{}'
```

`success=true`는 준비 요청 접수이며 도착 완료가 아니다. `/measurement/ready=true`와 실제 관절값을 확인한 뒤 기록한다. 사용 중 Y 또는 음성 stop은 취소를 요청한다. 노트북의 fetch는 거절된다.

## 좌표 기록

아래 예시는 콜라 밑면 중심이 바퀴 축에서 약 290mm 앞, 중심선상에 있다는 사용자 측정값을 사용한다. 이 위치를 다른 배치에 재사용하지 않는다. 출력 디렉터리는 새 경로여야 하며 `log/` 아래 데이터는 Git에서 제외된다.

```bash
python3 tools/vision_eval/record_vision.py --target coke --trials 3 \
  --reference-x-mm 290 --reference-y-mm 0 \
  --output log/vision_baseline_20261111/coke_center_pilot_01
```

출력:

- `trials.csv`: 성공·실패 모두 포함, 원본 좌표, confidence, 처리 시간.
- `session.json`: 사용자 실측, 현재 Git 상태·주요 파일 해시, 카메라 내부 파라미터, 최신 카메라 TF, 각 시행 관절값·피드백, 통계.
- 시행별 RGB·정렬 깊이·debug PNG: 각 이미지의 시각과 encoding을 JSON에 기록한다. 이 스냅샷들은 최종 수신 프레임이며, 액션 내부에서 집계한 모든 프레임 원본은 아니다.

`z_adjusted_mm`는 사용자 구조 치수에서 추정한 팔 장착 높이와 실행 중 `base_link→link1` 높이 차이를 원본 z에 더한 비교용 값이다. 현재 원본 모델이면 +25mm다. 모델이 이미 수정되면 실행 중 TF에 따라 차이를 계산하므로 +25mm를 무조건 중복 적용하지 않는다. 장착 구조가 수직 평행 이동만 했다는 가정이며, 카메라 장착 보정을 대신하지 않는다.

비전 좌표는 관찰된 표면 위치다. 사용자 기준점인 물체 밑면 중심이나 기하학적 중심과의 차이를 모두 센서 오차로 해석하지 않는다. 이 도구는 좌표 반복성과 관측 조건을 기록하며 Pick 성공률을 측정하지 않는다.

## 검증

상자 높이 재측정에는 `capture_geometry.py`로 RGB·정렬 깊이를 20ms 이내로 짝짓고, 각 깊이 영상 시각의 TF를 함께 저장한다. 준비 상태와 관절 입력이 신선해야 하며 구동 명령은 보내지 않는다. 10쌍 수집에 실패하면 부분 데이터와 오류를 보존한다.

```bash
python3 tools/vision_eval/capture_geometry.py --output log/geometry_capture_NEW
```

출력 경로는 새 경로여야 한다. 2026-11-11 재측정의 선택 영역·계산 방법·결과는 [재측정 보고서](../../docs/measurements/box_remeasurement_2026-11-11.md)에 기록했다. 상자 자체의 높이는 윗면과 지지면 사이의 거리이며, 로봇 모델 기준 윗면 z와 구분한다.

ROS 환경과 install을 source한 뒤 노드 생성·로봇 동작 없이 실행한다.

```bash
PYTHONPATH="$PWD/tools/vision_eval:$PYTHONPATH" \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
python3 -m pytest tools/vision_eval -q -p no:cacheprovider
```
