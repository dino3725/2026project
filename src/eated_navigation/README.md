# Jetson 지도 작성·Nav2 환경

이 패키지는 환경 준비 단계다. 기존 `eateded`의 팔/주행 실행을 자동으로 변경하지 않는다.
하드웨어는 기존 `eated_hardware.sh`가 생성·검증하는 +25mm 보정 모델을 계속 사용한다.
새 launch는 하드웨어나 다른 robot_state_publisher를 추가 실행하지 않는다.

## 2026-11-17 설치 확인

젯슨에서 시스템 패키지 설치가 완료됐고, 필수 ROS 패키지 13개가 모두 검색됐다.
Nav2 1.1.20 / Cartographer ROS 2.0.9002 / ROBOTIS 지도·주행 설정 2.2.1 /
twist_mux 4.3.0을 확인했다. 주요 실행 파일 9개의 동적 라이브러리 누락은 없었다.
두 launch의 인수 조회가 성공했다. 실제 센서 수신·지도·자율주행은 아직 검증하지 않았다.
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

다음 구현은 `twist_mux`의 정지 잠금·조이스틱·Nav2 입력 및 권한 연동이다.
Nav2 목표는 기존 취소 가능한 `_send_goal` 구조에 등록해야 한다.
현재는 기존 Y/음성 stop이 새 Nav2 목표까지 취소한다고 주장하지 않는다.

## 참고 구현

- [ROBOTIS 2.2.1 Cartographer launch](https://github.com/ROBOTIS-GIT/turtlebot3_manipulation/blob/2.2.1/turtlebot3_manipulation_cartographer/launch/cartographer.launch.py)
- [ROBOTIS 2.2.1 Nav2 설정](https://github.com/ROBOTIS-GIT/turtlebot3_manipulation/blob/2.2.1/turtlebot3_manipulation_navigation2/param/turtlebot3.yaml)
- [Nav2 Humble 실행 구성](https://github.com/ros-navigation/navigation2/blob/humble/nav2_bringup/launch/navigation_launch.py)

## 로봇에서 확인할 항목

- [ ] `/scan`, `/odom`, TF의 수신·시간·프레임 일치
- [ ] 바닥에서 수동 이동하며 지도 작성·저장
- [ ] `twist_mux` 연결 전 정지 잠금과 입력 유실 처리 구현·검증
- [ ] HOME/CARGO 형상·주행 제한 검증 후 Nav2 이동 시험
