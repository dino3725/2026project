# Local Voice Robot Command

Ubuntu 노트북에서 한국어 음성 명령을 완전히 로컬로 처리하고, 검증된 명령만
ROS2 `/voice/robot_command` topic으로 발행하는 서비스 로봇 명령 파이프라인입니다.

현재 Jetson과의 ROS2 DDS 통신 및 fetch 명령 전달까지 검증됐습니다. 노트북에서
음성 인식, 명령 분류 및 custom ROS2 message publish를 수행합니다.

## 처리 구조

```text
PulseAudio default microphone
        ↓
WebRTC VAD (speech start/end)
        ↓
16 kHz mono signed 16-bit PCM WAV
        ↓
faster-whisper small / CPU / int8 / Korean
        ↓
Ollama qwen3:4b-instruct
        ↓
structured JSON validation
        ↓
valid fetch 또는 stop만 eated_interfaces/RobotCommand publish
```

## 지원 명령

가져올 수 있는 물체는 네 종류입니다.

```json
{"action":"fetch","object":"coke"}
{"action":"fetch","object":"vaseline"}
{"action":"fetch","object":"tissue"}
{"action":"fetch","object":"airpod"}
```

플랫폼 정지 명령:

```json
{"action":"stop","object":""}
```

지원하지 않거나 모호한 명령:

```json
{"action":"unknown","object":""}
```

`unknown`은 ROS topic으로 발행하지 않습니다. `stop`은 `/voice/robot_command`에
발행하며, 실제 action 취소·베이스 정지·매니퓰레이터 hold는 stop을 지원하는
Jetson Control Node가 수행합니다.

## 의미 분류 기준

| 의미 | 결과 |
| --- | --- |
| 목·입안의 갈증, 마실 음료 필요 | `coke` |
| 입술·손·피부의 건조함, 보습 필요 | `vaseline` |
| 휴지 요청, 액체·소스·얼룩을 닦아야 함 | `tissue` |
| 에어팟·무선 이어폰·청취 도구 필요 | `airpod` |
| 로봇·플랫폼의 이동 또는 주행 정지 | `stop` |
| 미지원 물체, 다중 물체, 모호한 참조, 기타 제어 | `unknown` |

여러 지원 물체를 동시에 요청하면 한 물체를 임의로 선택하지 않고 `unknown`으로
처리합니다. 음악 재생을 멈추라는 명령은 플랫폼 정지가 아니므로 `stop`이 아닙니다.

## 패키지 구조

```text
src/voice_command_pkg/
  voice_command_pkg/
    voice_command_node.py   ROS2 노드: 파이프라인을 작업 스레드에서 돌리고 결과를 발행
    pipeline.py             VAD → WAV → Whisper → LLM 한 발화 처리 (ROS 의존 없음)
    audio_capture.py        PulseAudio 녹음, WebRTC VAD 상태 머신, VadConfig
    command_classifier.py   Ollama 분류기, 출력 스키마·허용 조합 검증
    prompts/system_prompt.txt  분류기 시스템 프롬프트 (바꾸면 회귀 테스트 필수)
    command_protocol.py     발행 허용 규칙 (fetch 4종, stop만 통과)
    parameters.py           ROS 파라미터 이름·기본값 → PipelineConfig
  config/voice_command.yaml 실험값 (코드 수정 없이 변경)
  launch/voice_command.launch.py
  test/                     pytest + ament lint
tools/voice_eval/           분류기 회귀·평가 스크립트, 평가셋, 결과 기록
voice_env.sh                노트북 실행 환경 (ROS + .venv)
```

## 준비 (노트북)

```bash
cd ~/2026project
python3 -m venv .venv                       # 처음 한 번
.venv/bin/pip install -r src/voice_command_pkg/requirements.txt
ollama list                                  # qwen3:4b-instruct 확인

source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-up-to voice_command_pkg
```

ROS 실행 파일은 `/usr/bin/python3`로 실행됩니다. Jetson의 `eated_pane.sh`처럼
`voice_env.sh`가 `.venv`의 site-packages를 `PYTHONPATH`에 추가합니다.
`pip install rclpy`는 하지 않습니다.

## 실행

```bash
source ~/2026project/voice_env.sh
ros2 launch voice_command_pkg voice_command.launch.py
```

`[LISTENING] 말씀해주세요.`가 보이면 말합니다. 종료는 `Ctrl+C`입니다.

- 발행 없이 확인만:
  `ros2 run voice_command_pkg voice_command_node --ros-args -p publish_commands:=false`
- 다른 실험값: YAML을 복사해 고친 뒤 `config_file:=/path/to/my.yaml`
- VAD만 확인: `ros2 run voice_command_pkg vad_monitor` (발화 시작·끝만 표시)
- 발화 WAV는 `~/2026project/recordings/`에 저장됩니다. 커밋하지 않습니다.

**주의:** Jetson이 켜져 있고 `ROS_DOMAIN_ID`가 같으면 `fetch`/`stop`이 실제
로봇에 전달됩니다. 로봇 없이 시험할 때는 `publish_commands:=false`를 쓰세요.

## 테스트

```bash
# ROS 없이 순수 로직 (노드 테스트는 자동으로 건너뜀)
.venv/bin/python -m pytest src/voice_command_pkg/test -q

# lint 포함 전체
source /opt/ros/humble/setup.bash && source install/setup.bash
colcon test --packages-select voice_command_pkg && colcon test-result --verbose
```

이 노트북에서는 `~/.local`의 anyio pytest 플러그인이 시스템 pytest와 맞지 않아
`colcon test` 앞에 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`이 필요합니다.

분류기 회귀 테스트 (Ollama 사용, ROS 발행 없음):

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/test_llm_pick_objects.py
```

`test_command_classifier.py`는 프롬프트 SHA256을 고정해 둡니다. 프롬프트를
바꾸면 이 테스트가 실패합니다. 회귀 테스트를 다시 돌려 결과를
`tools/voice_eval/results/`에 기록한 뒤 고정값을 갱신하세요.

## ROS2 메시지

- Topic: `/voice/robot_command`, Type: `eated_interfaces/msg/RobotCommand`, QoS depth 10
- Fields: `header`(stamp = 발행 시각), `action`, `target_class`, `transcript`
- 통과 조건: `fetch` + `coke|vaseline|tissue|airpod`, 또는 `stop` + 빈 문자열.
  `unknown`과 그 밖의 조합은 발행하지 않습니다.

## 파라미터 (기본값 = 2026-11-04 기준선)

전체 목록과 설명은 `config/voice_command.yaml`에 있습니다.

| 그룹 | 주요 값 |
| --- | --- |
| `vad.*` | mode 3, start window 300 ms 중 80%, RMS gate 200, pre-roll 450 ms, 종료 무음 900 ms, 최대 15 s, 스트림 시작 후 1.2 s 버림 |
| `whisper.*` | `small`, CPU, `int8`, `ko` |
| `llm.*` | `qwen3:4b-instruct`, timeout 120 s, `keep_alive` 10m (`think: false`, `temperature: 0`은 코드 고정) |

## YOLO 기반 객체 인식

음성 명령에서 선택된 물체를 카메라 영상에서 찾기 위해 별도 환경에서 YOLO11
객체 인식 모델을 학습하고 테스트했습니다. 하나의 모델이 다음 네 클래스를
탐지합니다.

```text
coke
vaseline
tissue
airpod
```

현재 음성 명령 시스템과 객체 인식 시스템은 각각 독립적으로 검증했으며, 아직
하나의 ROS2 실행 파이프라인으로 연결하지 않았습니다.

### 데이터셋

Roboflow에서 Object Detection 데이터셋을 구성하고 YOLO11 형식으로 변환했습니다.
학습, validation, 최종 test 데이터를 분리해 사용했습니다.

### 학습 환경

- Ubuntu 22.04
- NVIDIA GeForce RTX 5060 Ti 8 GB
- Python 3.11.16
- PyTorch 2.11.0 + CUDA 12.8
- Ultralytics 8.4.152
- Model: YOLO11n
- Input size: 640 × 640
- Epochs: 100
- Batch size: 16

학습 명령:

```bash
yolo detect train \
  model=yolo11n.pt \
  data=$HOME/yolo_ws/datasets/coke_tissue_airpod_vaseline/data.yaml \
  epochs=100 \
  imgsz=640 \
  batch=16 \
  device=0 \
  patience=20 \
  project=$HOME/yolo_ws/runs \
  name=coke_tissue_airpod_vaseline
```

학습된 weight 위치:

```text
~/yolo_ws/runs/coke_tissue_airpod_vaseline/weights/best.pt
```

### Test 결과

학습에 사용하지 않은 test 이미지 151장으로 평가했습니다.

| Class | Precision | Recall | mAP50 | mAP50-95 |
| --- | ---: | ---: | ---: | ---: |
| All | 0.952 | 0.924 | 0.949 | 0.823 |
| airpod | 0.959 | 1.000 | 0.995 | 0.796 |
| coke | 0.965 | 0.839 | 0.951 | 0.822 |
| tissue | 0.998 | 1.000 | 0.995 | 0.938 |
| vaseline | 0.884 | 0.857 | 0.855 | 0.734 |

전체 test 데이터 기준 `mAP50 = 0.949`, `mAP50-95 = 0.823`입니다.
`tissue`와 `airpod`은 높은 검출 성능을 보였고, `coke`와 `vaseline`은 일부
환경에서 미검출 또는 클래스 혼동이 발생할 수 있습니다.

### RealSense RGB 검출

```bash
yolo detect predict \
  model=$HOME/yolo_ws/runs/coke_tissue_airpod_vaseline/weights/best.pt \
  source=4 \
  conf=0.25 \
  show=True \
  device=0
```

RGB 영상에서 class, confidence, bounding box를 실시간으로 확인했습니다. 현재
확인된 대표적인 오분류는 다음과 같습니다.

- `airpod`을 `tissue`로 판단
- `vaseline`을 `airpod`으로 판단

향후 실제 로봇 카메라의 실패 사례를 데이터셋에 추가해 fine-tuning하고,
RealSense depth로 거리 또는 3차원 위치를 계산할 예정입니다.

## 저장소에서 제외되는 데이터

`.gitignore`는 다음 항목을 제외합니다.

- `.venv`와 Python cache
- 녹음 WAV 및 기타 오디오
- 로컬 model artifact
- 로그와 임시 파일
- `.env`와 credential 파일

실제 음성이 담긴 파일은 개인정보를 포함할 수 있으므로 공개 저장소에 commit하지
않습니다.

## 현재 범위와 다음 단계

현재 완료 범위:

```text
Laptop microphone
→ VAD
→ faster-whisper
→ Local LLM
→ validated fetch/stop command
→ eated_interfaces/msg/RobotCommand
→ ROS2 /voice/robot_command
```

아직 포함되지 않은 범위:

- Wi-Fi DDS 설정 및 검증
- TurtleBot/Nav2 이동
- OpenManipulator 물체 집기
- 음성 stop의 실제 하드웨어 종단 검증

다음 단계에서는 실제 로봇을 저위험 상태에 두고 음성 stop이 Jetson의 action 취소,
`cmd_vel = 0`, 매니퓰레이터 hold 및 `ERROR` 상태 전환까지 이어지는지 검증해야
합니다. 음성 stop만을 유일한 비상정지 수단으로 사용해서는 안 됩니다.
