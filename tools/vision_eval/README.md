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

## 카메라 장착 보정 준비

### 저장 자료로 자세별 오차 진단

2026-11-12 후속 작업에서는 아래 RGB-D 후보 비교까지 구현했다. 자세별 진단 결과를
실제 보정값으로 자동 적용하지 않는 원칙은 동일하다.

`diagnose_pose_residuals.py`는 ROS 실행이나 추가 촬영 없이 두 장착 보정 세션을 분석한다.
URDF FK와 기록 TF를 대조하고, 공통 카메라 장착값 및 어깨·팔꿈치 영점 가설을 비교한다.
원본 해시·관측 역할·세션 순서·모델이 다르면 중단한다. 이번 177×125mm 표식과 지지면 위
base_link 높이 10mm를 전제로 하며 다른 구성에 그대로 사용하는 범용 보정기가 아니다.

```bash
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 \
python3 tools/vision_eval/diagnose_pose_residuals.py \
  --sessions log/handeye_desk_fixed_20261111_01 log/handeye_desk_fixed_20261111_02 \
  --model log/measured_model_20261111/measured.urdf \
  --intrinsics-session log/intrinsics_box_20261111_01 \
  --output log/pose_diagnosis_NEW
```

시스템 Python 3.10, OpenCV 4.5.4, NumPy, SciPy를 사용한다. 출력 경로는 새 경로여야 한다.
`analysis.json`에는 원본·계산 코드 해시, 자세별 잔차, 변수 추정 가능성을 저장한다.
`pose_residuals.csv`는 공장 내부값·공통 장착 가설의 관절각(°)과 xyz 잔차(mm)다.
`decision.json`은 경계 도달·추정 모호성을 기록하며 항상 진단 전용으로 표시한다.
관절 영점 가설의 ±5°는 동작 명령이 아니며 실제 보정값으로 복사하지 않는다.
검증 관측은 학습에 넣지 않지만 이미 확인한 자료의 재사용이므로 새 독립 검증은 아니다.
이 도구는 실제 카메라·제어 설정을 내보내거나 변경하지 않는다.

[2026-11-12 결과 및 해석](../../docs/measurements/pose_residual_diagnosis_2026-11-12.md).

### 저장 RGB-D로 고정 팔 모델의 장착 후보 개선

후속으로 사용자가 후보를 채택했다. 현재 두 vision launch는
`src/vision_pkg/config/camera_mount.yaml`을 읽고, `eated_hardware.sh`와 `eated_moveit.sh`는
`ensure_calibrated_model.py`를 통해 동일한 +25mm 팔 받침 모델을 검증·사용한다.
[실물 좌표 시험 결과](../../docs/measurements/accepted_calibration_test_2026-11-12.md).
아래 후보 생성 도구와 과거 후보 JSON 자체는 계속 미적용 분석 자료로 보존한다.

`refine_saved_rgbd.py`는 관절 영점을 고정하고, RGB 무늬와 정렬 깊이의 표식 평면을 사용한다.
가까운 자세를 같은 그룹으로 묶은 5분할 교차검증으로 후보를 선택한다. 각 계산의 초기화도
그 계산에 포함한 학습 영상만 사용한다. 비교 계획 → 선택값 고정 → 기존 검증 평가 순으로 저장한다.
`rgbd_refinement.py`는 ROS 의존성이 없는 계산 모듈이다.

```bash
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 \
python3 tools/vision_eval/refine_saved_rgbd.py \
  --sessions log/handeye_desk_fixed_20261111_01 log/handeye_desk_fixed_20261111_02 \
  --model log/measured_model_20261111/measured.urdf \
  --intrinsics-session log/intrinsics_box_20261111_01 \
  --output log/rgbd_refinement_NEW

OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 \
python3 tools/vision_eval/audit_saved_surface.py \
  --capture log/vision_baseline_20261111/geometry_resume_01 \
  --profile log/rgbd_refinement_NEW/candidate_profile.json \
  --model log/measured_model_20261111/measured.urdf \
  --output log/rgbd_refinement_NEW/surface_audit.json
```

시스템 OpenCV 4.5.4·NumPy·SciPy를 사용한다. 두 장착 세션의 당시 `.tar.gz` 백업이
`log/backups/`에 있어야 깊이 원본을 대조할 수 있다(`--backups`로 지정 가능).
출력 경로·파일은 덮어쓰지 않는다. 이 도구는 이번 177×125mm 표식·10mm base 높이·2세션을
위한 비교 도구다. 과거 상자 장면 대조는 그 촬영 때의 +25mm 모델 차이를 검증하며 사용한다.

저장 깊이는 기존 내부값으로 이미 정렬돼 있다. 새 radial2 내부값으로 깊이 광선을 바꿔
정렬까지 보정했다고 가정하지 않는다. 실제 사용 검토 후보는 공장 내부값의 네 방식 중 선택한다.
`candidate_profile.json`은 미적용 분석 자료이며 launch가 읽는 설정 파일이 아니다.
최소 점수 선정은 후보군·가중치에 따른 것이고, 절대 정확도나 파지 성공을 보증하지 않는다.

[쉬운 원리 설명과 최종 비교 결과](../../docs/measurements/rgbd_refinement_2026-11-12.md).

### 별도 카메라 내부 파라미터 검증용 수집

`capture_intrinsics.py`는 RGB와 같은 시각의 CameraInfo만 구독한다. 팔 TF나 하드웨어
드라이버 없이 실행 가능하며 로봇 명령·카메라 보정값을 발행하지 않는다. 카메라가 고정되고
표식판을 움직이는 새 세션에 사용한다. 카메라 고정 여부는 이 영상 수집기만으로 검증하지 않는다.

`intrinsics_observation.py`는 기존 K/D를 넣지 않는 국소 호모그래피 방식으로 코너를 검출한다.
[OpenCV 설명](https://docs.opencv.org/4.5.0/df/d4a/tutorial_charuco_detection.html)을 따른다.
픽셀과 ID, 실제 인쇄 치수에 따라 축별 보정한 물체점을 함께 보존한다. 이번 출력 실측은
177×125mm이며 각 축 내부의 인쇄 배율이 균일하다고 가정한다. 기존 장착 보정 검출기는 그대로 둔다.

```bash
python3 tools/vision_eval/capture_intrinsics.py \
  --output log/intrinsics_NEW/audit_00 --session-id intrinsics_NEW \
  --role audit --width-mm 177 --height-mm 125
```

`audit`는 표식 검출 실패도 영상과 함께 저장하는 시야 확인용이다. `train`/`validation`은
12개 이상 비공선 코너가 필요하며, 촬영 후 흔들림·화면 분포·기울기 다양성을 별도로 검토한다.
한 장을 저장한 뒤 종료하고 기존 디렉터리는 덮어쓰지 않는다. 관측 저장은 보정 완료가 아니다.
내부 파라미터 후보 계산·독립 검증 및 실제 적용은 별도 단계다. 원본 영상은 `log/`에만 보관한다.

`solve_intrinsics.py`는 `training_frozen.json`의 해시와 검토 기록을 확인한 학습 영상만으로
왜곡 0, 방사 왜곡 2계수, 왜곡 5계수 후보를 계산한다. 앞의 두 후보는 한 자세씩 제외한
민감도도 기록한다. 결과 파일은 덮어쓰지 않으며 ROS나 카메라 설정을 변경하지 않는다.
`evaluate()`는 고정 K/D에 대해 영상별 보드 자세와 코너 분할 예측 오차를 구한다.
검증 영상을 `fit()`에 넣으면 거절한다. [실험 기록](../../docs/measurements/intrinsics_box_2026-11-11.md).

`evaluate_intrinsics.py`는 학습·검증 원본과 후보·계산 코드의 고정 해시를 대조한 뒤
새 검증 영상만 평가한다. `validation_protocol.json`에 지정된 촬영 수가 충족되어야 하며
K/D 재학습·설정 적용 없이 별도 JSON으로 기록한다.

`compare_handeye_intrinsics.py`는 고정된 내부값 후보를 기존 장착 보정 영상에 대입한다.
원래 코너 고정 비교와 내부값 없는 재검출 비교를 모두 수행하고, 각 세션의 학습 관측으로만
장착·표식 위치를 추정한다. 기존 검증 영상은 재사용한 진단이며 새 독립 실물 검증이 아니다.
RGB만 처리하며 정렬 깊이나 실제 설정을 변경하지 않는다.

2026-11-11부터 콜라 반복 비교 다음 단계로 ChArUco 기반 eye-in-hand 보정을 준비했다.
[준비 결과·출력물·촬영 절차](../../docs/measurements/calibration_preparation_2026-11-11.md)를 먼저 읽는다.

- `prepare_measured_model.py`: 팔 장착 높이 +25mm만 반영한 후보 모델과 같은 모델을 읽는
  하드웨어·MoveIt 실행 파일 생성. 기존 설치 파일은 수정하지 않고 로봇도 실행하지 않는다.
- `make_calibration_board.py`: A4 실제 크기 출력용 25mm ChArUco 표식판.
- `capture_handeye.py`: 준비 상태·정지 자세를 확인하고 영상 시각의 TF와 표식을 기록한다.
  `--audit-only`는 표식판 없이 RGB-D 메타데이터를 점검한다. 구동 명령은 없다.
- `handeye.py`, `solve_handeye.py`: 서로 다른 자세의 관측으로 후보를 계산하고 별도 자세로 검증한다.
  결과는 JSON 파일로 저장하며 카메라 TF를 자동으로 바꾸지 않는다.

시스템 Python의 OpenCV 4.5.4를 사용한다. 출력 보드의 좌표 규약이 바뀌지 않도록 다른 버전이면
거절한다. 현재 측정 제어기는 기존 인식 자세 한 개만 허용하므로, 다자세 이동 계획은 실제 표식판
배치 확인 후 마련한다. 동일 자세의 반복 촬영만으로 장착 보정이 완료되었다고 해석하지 않는다.
