# Jetson 지도 작성·Nav2 환경

이 패키지는 지도 작성, Nav2 준비, 가상 벽과 별도 실물 주행 시험 구성을 제공한다.
기본 `eateded`에서 물체를 집은 뒤의 이동은 F710 수동 주행이다.
하드웨어는 기존 `eated_hardware.sh`가 생성·검증하는 +25mm 보정 모델을 계속 사용한다.
새 launch는 하드웨어나 다른 robot_state_publisher를 추가 실행하지 않는다.

## 2026-11-17 설치 확인

젯슨에서 시스템 패키지 설치가 완료됐고, 필수 ROS 패키지 13개가 모두 검색됐다.
Nav2 1.1.20 / Cartographer ROS 2.0.9002 / ROBOTIS 지도·주행 설정 2.2.1 /
twist_mux 4.3.0을 확인했다. 주요 실행 파일 9개의 동적 라이브러리 누락은 없었다.
두 launch의 인수 조회가 성공했다. 설치 이후 실물 확인은 아래 날짜별 시험에 기록했다.
아래 설치 명령은 새 젯슨 환경을 구성할 때도 재사용할 수 있다.

## 설치와 확인

Ubuntu 22.04 arm64 / ROS2 Humble에서 다음 공개 패키지를 사용한다.
설치 명령은 로봇을 움직이지 않는다. sudo 비밀번호는 로컬 터미널에만 입력한다.

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ros-humble-navigation2 ros-humble-nav2-bringup \
  ros-humble-turtlebot3-manipulation-cartographer \
  ros-humble-turtlebot3-manipulation-navigation2 ros-humble-twist-mux
cd ~/2026project
source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-select eated_navigation
source install/setup.bash
ros2 run eated_navigation check_environment.py
```

`dependencies_installed`는 설치만 확인한다. 센서·TF·정지·자율주행 검증을 뜻하지 않는다.
실행 파일은 sudo 없이 조회하며, 노드를 시작하거나 목표를 발행하지 않는다.

## 지도 작성 준비

기존 보정 하드웨어를 준비한 뒤 아래를 사용한다. 이 launch 자체는 바퀴 명령을 보내지 않는다.
실물 실행·수동 이동은 바닥 배치와 주변 정리가 완료된 후 진행한다.

```bash
export ROS_DOMAIN_ID=37
ros2 launch eated_navigation mapping.launch.py start_rviz:=false
```

ROBOTIS Cartographer 설정을 사용하며 `/scan`, `/odom`과 기존 TF가 필요하다.
TF의 기대 연결은 `map → odom → base_footprint → base_link → base_scan`이다.
설정의 tracking_frame인 `imu_link` 변환도 필요하다. 2D 설정은 IMU 측정 사용을 끈다.
기존 하드웨어는 `odom → base_footprint`를 발행하고, Cartographer가 `map → odom`을 만든다.
현재 공급 바퀴 제어기의 `open_loop: true`는 주행 정확도 검증 시 별도로 점검할 항목이며
이번 환경 구성에서 바꾸지 않았다.

노트북에서는 같은 ROS 도메인·네트워크에서 RViz로 `/map`, `/scan`, TF를 확인한다.
노트북의 RViz 설치/통신 상태는 이 젯슨의 설치 결과와 별도로 확인해야 한다.

지도 저장은 지도 작성 중 다른 터미널에서 수행한다.

```bash
mkdir -p ~/2026project/maps
ros2 run nav2_map_server map_saver_cli -f ~/2026project/maps/lab
```

위 명령은 실제 지도가 생긴 뒤 사용한다. 빈 지도나 예제 지도를 실제 공간 지도로 취급하지 않는다.

## Nav2 준비 모드

```bash
ros2 launch eated_navigation nav2_standby.launch.py \
  map:=/home/fcsl/2026project/maps/lab.yaml
```

- 실제 저장한 지도 YAML을 요구한다.
- Nav2 라이프사이클 자동 활성화는 꺼져 있다 (`autostart=false`).
- 구성요소별 토픽 remap이 적용되도록 composition을 사용하지 않는다.
- controller/behavior의 속도는 `/navigation/cmd_vel_raw`, smoother 출력은
  `/navigation/cmd_vel`로 분리한다. 바퀴 입력 `/cmd_vel`로 연결하지 않는다.
- 기존 제어기·조이스틱이 `/cmd_vel`을 직접 사용하므로 이 단계에서 Nav2 출력을
  중계하지 않는다. `twist_mux` 설치만으로 명령 우선순위가 구현되지는 않는다.
- ROBOTIS 기본 footprint/속도 파라미터를 준비용으로 읽는다. 실제 HOME/CARGO 형상,
  하중, 센서 상태에 맞춘 주행 설정과 정지 시험 후에 활성화·바퀴 연결을 구현한다.
- Cartographer와 AMCL을 동시에 활성화해 `map → odom`을 중복 발행하지 않는다.

실물 주행은 별도 시험 제어기와 `twist_mux`를 사용한다. 시험 제어기는 Nav2 목표를
`_send_goal`에 등록하고 F710 Y와 음성 `stop`에서 취소한다. 일반 작업 상태 머신과
자율주행의 권한 연동은 후속 과제다.

## 2026-11-18 실물 단거리 시험 기록

바닥에서 F710으로 지도 작성 후 `maps/lab_20261118_1421.yaml`과 PGM을 저장했다.
빈 바닥에서 HOME 자세로 정면 0.50 m를 지정했다. 이 시험에서는 전용 제어기가
Nav2 목표를 등록하고, 주행 중 F710 조작이나 음성 stop이 들어오면 취소하도록 구성했다.
바퀴 입력의 유일한 발행자는 `twist_mux`였다.

- 첫 요청은 결과 대기 25초 후 취소됐다. 지도상 위치 변화는 없었다.
- 재시도에서는 오도메트리 0.262 m 지점에서 진행 실패로 종료됐다. DWB의
  `FollowPath.xy_goal_tolerance`는 0.25 m, 도착 검사 허용값은 0.065 m였다.
- 두 허용값을 0.065 m로 맞추고 `trans_stopped_velocity`를 0.02 m/s,
  진행 검사 반경을 0.05 m로 설정했다. 최고 전진 속도는 0.085 m/s였다.
  중단 지점에서 원래 지도 목표 (0.5304, 0.0119) m로 재개해 Nav2 성공을 수신했다.
- 최종 지도 추정 위치는 (0.4687, 0.0230) m로, 목표와의 거리는 0.0628 m였다.
  최종 바퀴 속도 명령은 전진·회전 모두 0이었고 시험 잠금이 다시 걸렸다.
  전체 0.50 m를 중단 없이 주행하는 반복 시험은 별도로 남아 있다.

## 연구실 통로의 주행 금지 구역

위치 추정에 쓰는 지도는 `maps/lab_20261118_145438.yaml`이다.
같은 폴더의 `lab_20261118_145438_keepout.yaml`은 주행 금지 구역을 담는다.
마스크는 원본 PGM에서 값이 254이고 GIMP 수정본에서 밝기가 192 이상인 칸의
상하좌우 연결 성분 중 가장 큰 영역을 흰색으로 저장했다. 흰색 3,666칸이 허용 통로이며
검은색은 금지 구역이다. 지도와 마스크의 크기, 원점, 해상도는 같다.
[GIMP 편집본](../../maps/lab_20261118_145438%20copy.pgm)

아래 명령은 연구실 마스크를 명시적으로 요구하며 기존 Nav2 준비 모드로 실행한다.
자동 검색을 사용할 때는 지도 YAML과 같은 폴더의 `<지도 이름>_keepout.yaml`을 찾는다.

```bash
cd ~/2026project
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=37
ros2 launch eated_navigation nav2_standby.launch.py \
  map:="$PWD/maps/lab_20261118_145438.yaml" \
  keepout_mask:="$PWD/maps/lab_20261118_145438_keepout.yaml" \
  params_file:="$PWD/src/eated_navigation/config/lab_home_nav2.yaml"
```

마스크 서버는 `/keepout_mask`를 제공한다. 전역·지역 비용지도는 금지 구역을
반영한 다음 로봇 프로필의 팽창 설정으로 경계 주변에 비용을 매긴다.
준비 모드에서는 위치 추정·주행 라이프사이클을 별도로 활성화해야 한다.
RViz 설정은 `config/keepout_review.rviz`이며 지도, 전역 비용지도, `/plan`을 표시한다.

11월 18일에는 하드웨어와 분리된 ROS 도메인에서 고정 TF와 실제 Nav2 서버로 확인했다.
지도 좌표 (−0.205, −0.025) m에서 (3.595, 2.325) m까지의 경로는 5.67 m였고,
금지 구역 안 (1.00, 2.10) m에 대한 경로는 생성되지 않았다.
검증 경로를 5 mm 이하 간격으로 검사했을 때 중심선에서 금지 칸까지의 보수적인
최소 거리는 0.45 m 이상이었다. 실물 위치 추정·추종 오차는 이 값에 포함되지 않는다.
[RViz 경로 화면](../../docs/measurements/lab_keepout_plan_20261118.png)

### 연구실 실물 주행 결과 (2026-11-18)

같은 지도를 사용해 빈 집게와 HOME 자세로 주행했다. 출발 전 정지 스캔을 원본
지도와 맞춰 (0.135, −0.122) m, 방향 6.4°에서 AMCL을 초기화했다.
F710 중립과 센서 수신을 확인하고 전진 속도를 최대 0.085 m/s로 제한했다.

| 구간 | 목표 (지도 x, y, m) | 정지 후 AMCL (m) | 목표와 거리 | 도착 후 스캔을 지도에 별도로 맞춘 결과의 목표 오차 |
|---|---|---|---|---|
| 정면 0.50 m | (0.632, −0.066) | (0.576, −0.101) | 6.6 cm | 10.4 cm |
| 왼쪽 코너를 거치는 계획 경로 4.87 m | (3.595, 2.325) | (3.591, 2.261) | 6.4 cm | 8.2 cm |

두 목표 모두 Nav2가 도착 성공을 반환했고 최종 바퀴 명령은 전진·회전 모두
0이었다. 코너 주행에서 기록한 AMCL 중심 위치 표본은 모두 허용 통로 안에
있었다. 코너 목표 방향은 90°에 가까운 1.57 rad였고 최종 AMCL 방향은
81.8°였다. AMCL과 별도 스캔 정합은 같은 라이다와 지도를 사용하므로
표의 값은 지도 기준 추정치다. 첫 실행 로그는
`log/lab_drive_HNNdNY/{straight,corner}.log`에 있다.
[코너 주행 계획과 실측 중 AMCL 위치 비교](../../docs/measurements/lab_keepout_drive_20261118.png)

11월 18일 시험에서는 출발점을 전달 후보로, 코너 너머 (3.595, 2.515)m를 픽업 후보로 잡았다.
현재 작업 위치 파일에는 11월 19일에 실제로 주차한 자리가 들어 있다. 25.4 cm 앞에 새 목표를 지정한 시험에서 실제 정차 후
AMCL 위치는 (3.586, 2.453) m였고, 목표와의 거리는 6.3 cm였다.
이 좌표는 베이스의 주행 목표다. 실제 상자를 놓은 뒤 팔의 파지 가능 거리와
방향을 확인해야 한다.

빈 집게·HOME 주행 프로필의 전진 상한은 0.14 m/s이며 회전 상한은 0.28 rad/s다.
25.4 cm 구간의 최고 전진 명령은 0.081 m/s였다. 4.87 m 코너 경로의
기존 69초 기록은 이전 0.085 m/s 상한에서 측정했다.

## 2026-11-19 작업 위치 등록과 양방향 실물 시험

F710으로 작업 자세를 직접 맞춰 주차하고, 바퀴 명령·오도메트리·양쪽 바퀴 관절값으로 정차를
확인했다. `map` 프레임의 AMCL 위치를 별도의 정지 라이다 정합과 비교한 뒤 x, y, yaw를
저장했다. 픽업 후보가 가상 벽에 가까웠을 때는 차체 반경과 비용지도를 확인하고 주차를
다시 조정했다. [등록 위치](../../maps/lab_20261118_145438_waypoints.yaml)

### 수동과 자동 명령 전환

- 수동 허용에는 F710 중립, HOME 관절 피드백, 최신 라이다·오도메트리·위치 추정을 사용했다.
- 등록 과정에서 권한을 켠 뒤에도 상속된 정지 타이머가 높은 우선순위로 0을 보내는 문제가 있었다.
  권한과 타이머가 같은 상태를 보도록 수정한 뒤 바퀴에 수동 명령이 전달됨을 확인했다.
- 별도 접속 문제에서는 F710 수신기가 USB와 ROS에서 인식됐지만 리눅스 입력 장치에 스틱 변화가
  들어오지 않았다. 패드 배터리를 다시 끼운 뒤 스틱 입력과 중립 복귀를 확인했다.
- 자동 시험에서 수동 노드가 발행하는 0 속도도 Nav2보다 우선했다. 자동 실행에 앞서 수동 속도
  발행을 종료하고 F710 입력 수신과 Y 정지, 음성 `stop`은 시험 제어기에 연결했다. 바퀴 입력은
  `twist_mux`가 단독 발행했다.

### 시험 설정과 결과

| 항목 | 값 |
| --- | --- |
| 빈 집게 직선 전진 상한 / 관측 최고 명령 | 0.18m/s / 0.13m/s |
| 벽 주변 또는 목표 45cm 이내 전진 상한 | 0.08m/s |
| 회전 상한 | 0.28rad/s |
| 로봇 모델 반경 | 0.26m |
| 전역·지역 차체 추가 여유 | 0.005m |
| 지역 비용지도 격자 | 0.025m |

원본 지도와 금지 구역은 같은 0.05m 격자를 사용한다. 픽업 지점에서 출발할 때 지역 비용지도가
장애물 경계로 판단했다. 정지 스캔으로 위치를 맞춘 뒤 차체 충돌 검사와 함께 가상 벽에서
벗어나는 약 25cm 이동과 방향 정렬을 실행했다. 그다음 주행 요청 시 Nav2 컨트롤러가
종료 코드 `-11`을 남겼다. 정확한 크래시 원인은 미확정이다. 비용지도 해상도와 차체 여유를
시작 설정에 넣고 Nav2를 재기동한 뒤 전달 주행에 성공했다.

| 구간 | 결과 | AMCL 정차 오차 |
| --- | --- | --- |
| 픽업 주변 → 전달 | 약 4.96m 계획, 약 48초 후 도착 | 위치 6.7cm, 방향 8.3° |
| 전달 → 픽업 첫 복귀 | 목표 약 34cm 앞에서 전진 진행 실패 | 정차 유지 |
| 픽업 마지막 접근 | 정차 위치에서 약 0.41m 경로 재계산, 약 13.4초 후 도착 | 위치 6.0cm, 방향 6.3° |

자료는 [11/19 실행 설정, 정차 결과, 경로·속도 CSV](../../docs/measurements/data/2026-11-19/navigation/)에 있다.
[시험 주행 그림](../../docs/measurements/lab_roundtrip_20261119.png)에서 AMCL의 이동 궤적과 재접근을 볼 수 있다.
런타임 시험 설정은 `runtime_nav2.yaml`에 보존했고, Nav2 준비 실행의 `params_file` 인수로 읽을 수 있다.
노트북에서 확인할 때는 [SSH와 RViz 실행](../../README.md#ssh에서-실행하고-노트북-rviz로-보기)을 따른다.

## 참고 구현

- [ROBOTIS 2.2.1 Cartographer launch](https://github.com/ROBOTIS-GIT/turtlebot3_manipulation/blob/2.2.1/turtlebot3_manipulation_cartographer/launch/cartographer.launch.py)
- [ROBOTIS 2.2.1 Nav2 설정](https://github.com/ROBOTIS-GIT/turtlebot3_manipulation/blob/2.2.1/turtlebot3_manipulation_navigation2/param/turtlebot3.yaml)
- [Nav2 Humble 실행 구성](https://github.com/ros-navigation/navigation2/blob/humble/nav2_bringup/launch/navigation_launch.py)

## 로봇에서 확인할 항목

- [ ] `/scan`, `/odom`, TF의 수신·시간·프레임 일치
- [ ] 바닥에서 수동 이동하며 지도 작성·저장
- [ ] `twist_mux` 연결 전 정지 잠금과 입력 유실 처리 구현·검증
- [ ] HOME/CARGO 형상·주행 제한 검증 후 Nav2 이동 시험
