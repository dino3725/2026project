# 카메라 장착 보정 준비 — 2026-11-11

**최신 재개 메모:** 사용자 별도 테스트를 위해 실험 프로세스를 모두 종료했다.
[종료 상태·2차 실험·실측 출력 크기·다음 단계](RESUME_AFTER_PAUSE_2026-11-11.md)를 먼저 읽는다.

최신 상태: 실제 16개 자세 촬영과 후보 계산을 완료했다. 상대 일관성은 통과했으나 절대 책상 높이
차이가 남아 카메라 TF 적용을 보류했다. [1차 실험 결과](handeye_experiment_2026-11-11.md)를 따른다.
아래 초기 준비 기록 이후의 변경은 문서 마지막 후속 절에 기록했다.

## 현재 결론과 완료 범위

사용자는 콜라 좌표 오차가 카메라 캘리브레이션에서 올 수 있으므로 먼저 보정하자고 제안했고,
준비·실행 가능한 부분을 진행하도록 요청했다. 프린터는 사용할 수 있고, 출력물을 붙일 판은 확인 중이다.

초기 준비 시점에는 **실측 모델 후보, 출력 표식판, 읽기 전용 촬영기, 오프라인 보정 계산기까지 준비한 상태**였다.
장착 후보는 이후 1차 실험에서 계산했다. 로봇 모델도 이후 후속 절의 절차로 적용했다.
카메라 내부 파라미터나 RealSense 공장 보정값을 변경하지 않았다.

기존 콜라 3개 위치의 총 9회 측정은 `experiment_285mm_2026-11-11.md`에 보존했다.
좌우 약 50mm 이동에서 관측 이동 48.73mm, 전방 약 50mm 이동에서 관측 이동 44.62mm였다.
후자를 깊이 스케일 오차로 바로 간주하지 않는다. 손으로 옮긴 캔과 YOLO 중심점은 동일한 표면점의
정확한 3D 기준이 아니다. 고정된 장착 평행이동 보정도 두 측정점의 변위를 바꾸지 못한다.

## 1. 실측 구조 모델

`tools/vision_eval/prepare_measured_model.py`는 설치된 ROBOTIS 모델을 읽고 다음 한 항목만 변경한다.

| 항목 | 공식 모델 | 준비한 모델 |
|---|---:|---:|
| `base_link → link1` x | −92mm | −92mm |
| `base_link → link1` z | 91mm | 116mm |
| 바퀴 접지 평면 → 어깨 중심 | 177.5mm | 202.5mm |

1–2단 +10mm, 2–3단 +15mm와 어깨 실측 202mm를 근거로 했다. 0.5mm 차이는 추가 보정으로 만들지 않았다.
생성된 XML을 비교해 `base_fixed` 높이 이외의 변경이 없음을 확인했다.

출력: `log/measured_model_20261111/`

- 원본 매크로·제어기 설정을 보존한 xacro와 확장 URDF
- 같은 xacro를 읽는 `base.launch.py`, `hardware.launch.py`, `move_group.launch.py`, `moveit_rviz.launch.py`
- 원본 파일 해시와 생성 모델 해시를 기록한 `manifest.json`

원본 `/opt/ros` 파일과 기존 `eated_hardware.sh`, `eated_pane.sh` 기본 실행은 변경하지 않았다.
후보 실행 파일을 쓰지 않으면 기존 모델이 실행된다. 혼용하면 안 된다.
팔 속도·가속 배율, 기존 자세, 그리퍼 값, 정지 경로는 변경하지 않았다.

**범위 제한:** 이 후보는 팔 장착 보정을 위한 기구학 모델이다. 층간 간격에 맞춘 차체 메시/충돌
형상의 재작성 및 라이다 대략 위치 반영은 포함하지 않는다. 완성된 차체 충돌 모델로 취급하지 않는다.
라이다 위치는 이번 카메라–팔 보정의 입력이 아니다.

실제 재시작은 출력물 준비와 팔 지지/재시작 절차 확인 뒤 진행한다.
현재 팔을 지지하는 하드웨어 드라이버를 이 준비 작업 도중 종료하지 않았다.

## 2. 출력물과 사용자가 준비할 것

파일: [EATED_ChArUco_A4_25mm.pdf](calibration_board_25mm/EATED_ChArUco_A4_25mm.pdf)

- A4 세로, 실제 크기/100%, 페이지 맞춤 해제, 흑백 출력 가능.
- 7×5칸, 한 칸 25mm, 마커 18mm, 전체 무늬 175×125mm.
- 아래 기준선이 100mm인지 확인. 가로 네 칸과 세로 네 칸도 각각 100mm인지 확인하면
  가로·세로 인쇄 비율 차이를 확인할 수 있다. 잰 값은 반올림해 맞다고 하지 말고 그대로 기록한다.
- A4 정도의 평평하고 단단한 판에 전체를 밀착시킨다. 무늬 위에 광택 테이프를 붙이지 않는다.
- 종이만 세워 두거나 휘어진 얇은 상자에 붙인 상태로 정밀 보정하지 않는다.
- 부착 후 카메라에 들어갈 위치를 정한다. **보정 촬영을 시작한 뒤 표식판과 로봇 베이스를 움직이지 않는다.**
  팔에 달린 카메라를 서로 다른 자세로 움직이는 방식이며, 사람이 표식판을 들고 움직이는 방식이 아니다.

별도 ChArUco 인쇄본과 섞지 않도록 `board.json`에 크기·사전·OpenCV 버전을 고정했다.
시스템 Python/OpenCV 4.5.4를 사용한다. 프로젝트 `.venv`의 다른 OpenCV를 섞으면 의도적으로 중단한다.

PDF는 A4 1페이지(595.276×841.89pt)이며, 렌더링한 PDF에서도 코너 24개가 인식됐다.
렌더링 영상의 재투영 RMS 0.015px는 **파일 생성 검증**일 뿐, 실제 카메라 보정 정확도가 아니다.

## 3. 실제 카메라 읽기 전용 점검

`capture_handeye.py --audit-only`로 현재 스트림과 영상 시각의 TF를 확인했다.

| 항목 | 결과 |
|---|---|
| RGB / aligned depth 해상도 | 모두 1280×720 |
| 광학 좌표계 | 모두 `d435i_color_optical_frame` |
| 저장한 쌍의 RGB–depth 시간 차이 | 0ms |
| RGB / aligned depth K | 서로 일치 |
| fx / fy | 911.7131 / 911.4947px |
| cx / cy | 632.2311 / 372.9809px |
| RGB 왜곡 모델 / D | `plumb_bob`, 모두 0 |
| 깊이 인코딩 | `16UC1` |
| 실행 중 `base_link → link1` z | 91mm, 기존 모델 확인 |

이것은 촬영·변환 설정의 일관성 점검이다. D가 0이라는 이유로 실제 렌즈 왜곡이 없다고 단정하지
않으며, 내부 파라미터 정확도나 깊이의 절대 정확도를 검증한 결과도 아니다.
숫자 기록은 `data/2026-11-11/calibration_camera_audit.json`에 보존한다.

## 4. 준비한 촬영·계산 도구

### 촬영

`capture_handeye.py`는 구독과 파일 저장만 하며 이동 목표·속도·TF를 발행하지 않는다.

- `/measurement/ready`, 최신 관절 상태, 0.5초 동안 팔 자세의 안정성을 확인한다.
- RGB와 depth를 20ms 이내로 짝짓고, RGB와 정확히 같은 stamp의 색상 CameraInfo를 사용한다.
- RGB 촬영 시각의 `T_base_ee`와 `T_camera_optical`을 기록한다. 기존 부정확한 카메라 장착 TF를
  관측 정답으로 사용하지 않는다.
- 실행 중 로봇 설명의 해시, 이미지 해시, 프레임, K/D, 표식 코너와 재투영 오차를 기록한다.
- 실제 보정용 수집은 실측 높이 모델이 로드되지 않으면 거절한다. `--audit-only`만 기존 모델을 허용한다.
- 코너 12개 미만, 일직선 코너, 재투영 RMS 1px 초과, 부적합한 왜곡 모델 등은 거절한다.
- 파일은 새 폴더에만 저장한다. 시간 초과·실패도 `observation.json`에 남는다.

### 계산

`handeye.py`, `solve_handeye.py`는 OpenCV Park 방식으로 다음 관계를 푼다.

`T_base_ee × T_ee_optical × T_optical_board = 고정된 T_base_board`

계산된 광학 좌표계 변환을 카메라 본체 장착 변환으로 바꾸는 방향도 테스트했다.

`T_ee_camera = T_ee_optical × inverse(T_camera_optical)`

- 학습 자세 최소 8개, 별도 검증 자세 최소 3개. 실제로는 학습 12–15개와 검증 3–5개를 목표로 한다.
- 같은 자세에서 여러 프레임만 찍어 수를 채우면 거절한다.
- 서로 거의 평행한 축의 회전만 있으면 거절한다. 다른 회전축을 포함하는 자세가 필요하다.
- 서로 다른 보드 세션, K/D 또는 로봇 모델을 섞으면 거절한다.
- 검증 자세는 보정 계산에 넣지 않고, 학습에서 추정한 표식판 위치와 비교한다.
- 내부 일관성의 임시 선별 기준은 최대 위치 오차 5mm·각도 오차 1°다. 이를 통과해도 실제 절대
  정확도나 파지 성공률을 보장하지 않는다. 후보 검토와 독립적인 물리 측정이 필요하다.
- 출력은 후보 JSON이며 TF나 ROS 파라미터에 자동 적용하지 않는다.

**남은 실행 준비:** 현재 `measurement_control.py`는 기존 인식 자세 한 개만 허용한다.
여러 자세의 실제 이동 계획은 표식판 위치·시야와 주변 간섭을 확인한 뒤 별도로 정해야 한다.
이번 작업에서 임의의 다자세 자동 이동 명령을 추가하거나 실행하지 않았다. 전원이 들어온 팔을
손으로 억지로 움직여 자세를 만들지 않는다.

## 5. 검증과 재현 명령

새 테스트 14개 + 기존 측정 도구 테스트 11개 = 25개 통과.
알려진 보정값 복원, 광학↔본체 변환 방향, 검증 데이터 분리, 표식판 이동 검출, 평행축/중복 자세
거절, 잘못된 변환 거절, 보드 인식, 모델의 단일 변경을 확인했다.
실제 수정 모델의 하드웨어·MoveIt launch는 `--show-args`로 구문과 로딩 경로를 확인했고 실행하지 않았다.
추가 코드의 flake8(99자 제한), 실행 도구의 pydocstyle, `git diff --check`도 통과했다.

측정 원본과 후보 모델은 `log/backups/calibration_preparation_20261111.tar.gz`에 로컬 백업했다.
파일 142개를 원본과 SHA256으로 대조했으며 모두 일치했다. 백업 파일은 70,907,196바이트다.
백업 해시와 수치 증거 목록은 `data/2026-11-11/calibration_preparation_*.json`에 기록했다.
이번 추가 변경은 `jetson/development`의 로컬 작업 파일이며 아직 커밋·push하지 않았다.

```bash
source /opt/ros/humble/setup.bash
source /home/fcsl/2026project/install/setup.bash
# 새 출력 폴더 이름을 사용한다.
python3 tools/vision_eval/prepare_measured_model.py --output log/measured_model_NEXT
python3 tools/vision_eval/make_calibration_board.py --output log/calibration_board_NEXT
python3 tools/vision_eval/capture_handeye.py --audit-only --output log/camera_audit_NEXT
python3 -m pytest tools/vision_eval -q
```

아래는 모델 적용·표식판 고정·각 자세 이동과 정지가 확인된 **이후** 사용할 수집 예시다.
명령 자체는 팔을 움직이지 않는다. 한 폴더는 한 자세이며 검증 역할은 촬영 전에 정한다.

```bash
python3 tools/vision_eval/capture_handeye.py \
  --session-id fixed_board_01 --role train --output log/handeye_01/train_01
python3 tools/vision_eval/capture_handeye.py \
  --session-id fixed_board_01 --role validation --output log/handeye_01/validation_01
python3 tools/vision_eval/solve_handeye.py \
  log/handeye_01/*/observation.json --output log/handeye_01/candidate.json
```

수정 모델로 재시작할 때는 동일한 생성 폴더의 hardware와 move_group을 사용하며 MoveIt의
`use_sim:=false`를 명시한다. 기존 모델로 종료하기 전후 팔 지지와 정지 입력 상태를 확인한다.
기존 평가 도구의 +25mm 사후 비교값을 새 모델 데이터에 중복으로 더하면 안 된다.
`record_vision.py`는 실측 기준 높이와 live TF의 차이를 계산하므로 새 모델에서는 이 차이가 0이 된다.

## 다음 순서

1. 출력 크기·판 재질/평탄도 확인 → 표식판 위치와 시야 확인.
2. 팔이 안전하게 지지되는 재시작 절차로 실측 모델 적용 → TF와 정지 입력 재확인.
3. 표식판이 고정된 상태에서 간섭 없는 서로 다른 회전축의 촬영 자세 계획.
4. 학습·검증 촬영 → 장착 후보 계산 → 실제 위치로 검증 → 통과한 값 적용.
5. 콜라·상자 좌표 재측정. 그 후 남은 깊이/팔/표면점 오차를 분리한다.

참고: [OpenCV 4.5.4 ChArUco](https://docs.opencv.org/4.5.4/d9/d6a/group__aruco.html),
[OpenCV hand–eye 정의](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html).

## 후속: 실제 출력물 배치 확인

사용자는 인쇄 경계, 무늬 누락/번짐 여부, 100mm 기준선 확인을 모두 통과했다고 알렸다.
별도 받침판 대신 평평한 책상에 종이를 직접 놓는 방법으로 진행했다.
`log/vision_baseline_20261111/board_placement_152103/`의 최신 RGB 10장을 구독해 확인했다.
팔·바퀴 명령이나 모델 변경은 수행하지 않았다.

- 10/10장에서 마커 17개, ChArUco 코너 24개 검출.
- 재투영 RMS 0.209–0.238px. 현 자세에서 인쇄·배치의 기본 인식 점검을 통과했다.
- 종이 아래쪽 여백 일부는 화면 밖이지만, 보정 무늬의 코너는 모두 보인다.
- 현 단계에서 로봇을 옮기거나 재출력할 필요는 보이지 않는다. 다른 팔 자세의 시야는 아직 미확인이다.
- 현 위치에서 종이를 평평하게 테이프로 고정하도록 요청했으며, 사용자 확인을 기다린다.
- 단일 자세 결과는 장착 보정 또는 내부 파라미터 정확도 검증 완료를 의미하지 않는다.

수치 요약은 `data/2026-11-11/board_placement_152103.json`에 보존했다.
영상에는 사람이 보여 원본과 표시 영상은 Git 제외 경로인 `log/`에만 보관했다.

## 후속: 표식 고정, 실측 모델 적용과 촬영 경로 준비

사용자가 표식판을 현재 위치에 테이프로 고정했다고 확인했다.
최초 계획의 하드웨어 재시작 대신, 설치 버전의 지원 경로를 확인하고 고정 장착 위치만 실행 중 갱신했다.
공식 [Humble robot_state_publisher 구현](https://github.com/ros/robot_state_publisher/blob/humble/src/robot_state_publisher.cpp)의
`onParameterEvent`는 robot_description 변경 시 모델을 다시 읽고 고정 TF를 갱신한다.

- 기존 RSP·controller_manager 모델이 서로 같음을 먼저 확인했다.
- 후보와의 차이는 `base_fixed`의 z=0.091→0.116m 하나뿐이었다.
- `ros2_control` 블록 및 모든 가동 관절·제어 설정이 동일함을 XML 구조 비교로 확인했다.
- controller_manager의 보관 파라미터와 RSP의 모델을 후보로 갱신하고, 파라미터 readback과 실제
  `base_link→link1` TF z=0.116m를 확인했다. 실행 중인 MoveIt은 없었다.
- 하드웨어 인스턴스는 재초기화하지 않았다. 이미 로드한 모터 제어 정보는 그대로 유지한다.
  이 변경에 필요한 기구학 계산은 RSP가 맡으며, 다음 MoveIt 시작에는 후보 launch를 사용해야 한다.
- 적용기는 `apply_measured_model.py`, 변경 전 모델과 결과는
  `log/vision_baseline_20261111/model_application/`에 보존했다.
- 기존 기본 실행 스크립트는 아직 공식 모델을 가리킨다. 다음 재시작 때는 생성된 공통 모델 실행
  파일을 사용해야 한다. 실행 중 갱신이 영구 설치 변경을 의미하지 않는다.

실측 모델 적용 후 `log/handeye_desk_fixed_20261111_01/train_00/`에 첫 학습 관측을 저장했다.
이는 현재 자세 한 개의 관측이며 아직 보정값 계산은 하지 않았다.

`calibration_plan.py`는 실제 첫 관측을 바탕으로 12개 추가 학습 자세와 3개 검증 자세를 준비한다.
현재 자세에서 joint1은 ±12°, joint2는 +0~12° 범위만 바꾸며 joint3·joint4는 고정한다.
기존 작업용 자세와 속도·가속 배율은 바꾸지 않았다. 각 이동 시간은 기존 4초다.
후보 경로의 중간 25개 지점을 검사했으며, 전체 표식 예상 화면 여유 최소 약 93px,
end_effector_link 높이 최소 약 320mm(base_link 기준)였다. 이 검사는 메시 충돌 검사를 대신하지 않는다.
초기 손목 각도 변경안은 표식이 화면 밖으로 나갈 수 있어 계산 단계에서 제외했고 실행하지 않았다.

`calibration_control.py`는 기존 측정 제어기를 상속한다. 새 액션은 기존 등록·취소 경로를 사용하며,
Y/음성 stop, 조이스틱·관절 신선도, 수동 주행 금지, 자동 HOME 금지를 유지한다.
명시적인 번호별 Trigger 하나로 자세 하나만 실행하며, 순서 오류·중복·다른 모델·예상과 다른 시작
자세를 거절한다. 현재 제어기로 교체했지만, 촬영 자세 이동 목표는 아직 발행하지 않았다.
새 제어기 시작으로 물리 Y 확인이 초기화되어, 주변 정리와 Y 재확인을 사용자에게 요청했다.

추가 검사 포함 테스트 37개 통과. 신규 실행 도구 flake8·pydocstyle 통과.
