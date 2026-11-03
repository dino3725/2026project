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

## 주요 파일

| 파일 | 역할 |
| --- | --- |
| `voice_command_test.py` | VAD → STT → LLM → optional ROS2 통합 실행 |
| `vad_test.py` | PulseAudio/WebRTC VAD 단독 테스트 |
| `llm_test.py` | Ollama 명령 분류, JSON schema 및 Python 검증 |
| `robot_command_publisher.py` | 검증된 fetch/stop 명령의 ROS2 publisher |
| `src/eated_interfaces` | Jetson과 공유하는 ROS2 custom interface 패키지 |
| `test_llm_pick_objects.py` | STT 없는 현재 명령 분류 회귀 테스트 |
| `test_robot_command_publisher.py` | local ROS2 publisher/subscriber 안전 테스트 |
| `text_command_ros_test.py` | 텍스트 → LLM → ROS2 통합 테스트 |
| `test_llm_classifier.py` | 이전 classifier core 회귀 테스트 |
| `dev_cases_v1.json` | 이전 classifier 개발용 데이터 |
| `final_heldout_v2.json` | classifier v2의 완료된 평가 데이터 |

`dev_cases_v1.json`과 `final_heldout_v2.json`은 과거 평가 기록입니다. 현재 물체
구성의 검증에는 `test_llm_pick_objects.py`를 사용합니다.

## 확인된 환경

- Ubuntu 22.04
- Python 3.10.12
- Intel Core i7-10510U, 4코어/8스레드
- RAM 15 GiB
- NVIDIA GPU 없음
- ROS2 Humble
- faster-whisper 1.2.1
- Whisper model: `small`
- Ollama 0.33.3
- LLM model: `qwen3:4b-instruct`
- WebRTC VAD: `webrtcvad-wheels==2.0.14`

## 준비

Python 가상환경은 프로젝트의 기존 `.venv`를 사용합니다.

```bash
cd /home/sh/llm_robot_test
.venv/bin/python --version
.venv/bin/pip install -r requirements.txt
```

Ollama 서비스와 모델을 확인합니다.

```bash
ollama --version
ollama list
curl http://127.0.0.1:11434/api/tags
```

필요한 모델 이름은 `qwen3:4b-instruct`입니다.

ROS2 명령을 실행할 터미널에서는 환경을 먼저 불러옵니다.

```bash
source /opt/ros/humble/setup.bash
```

ROS2 setup을 source하면 기존 `.venv/bin/python`에서도 `rclpy`와 `std_msgs`를
불러올 수 있습니다. `pip install rclpy`는 사용하지 않습니다.

## 빠른 테스트

### 단일 텍스트 분류

```bash
cd /home/sh/llm_robot_test
.venv/bin/python llm_test.py "로봇 멈춰"
```

기대 결과:

```json
{"action":"stop","object":""}
```

다른 예시:

```bash
.venv/bin/python llm_test.py "목마른데 콜라 가져다줘"
.venv/bin/python llm_test.py "입술이 터서 바를 게 필요해"
.venv/bin/python llm_test.py "커피를 쏟아서 닦을 게 필요해"
.venv/bin/python llm_test.py "에어팟 가져다줘"
.venv/bin/python llm_test.py "오늘 날씨 어때"
```

### 전체 텍스트 LLM 테스트

마이크와 STT를 사용하지 않고 48문장을 실행합니다.

```bash
.venv/bin/python test_llm_pick_objects.py
```

현재 확인된 결과:

```text
class.coke=8/8
class.vaseline=8/8
class.tissue=8/8
class.airpod=8/8
class.stop=8/8
class.unknown=8/8
overall=48/48
```

### ROS2 publisher 단독 테스트

```bash
source /opt/ros/humble/setup.bash
.venv/bin/python test_robot_command_publisher.py
```

이 테스트는 fetch 4종과 stop을 local subscriber로 수신하고, unknown 및 잘못된
action/object 조합이 차단되는지 확인합니다.

### 텍스트 → LLM → ROS2 테스트

```bash
source /opt/ros/humble/setup.bash
.venv/bin/python text_command_ros_test.py
```

기대 결과:

```text
[SUBSCRIBER]
received=5 expected=5
RESULT: PASS
```

## 실제 음성 실행

ROS publish 없이 기존 로컬 음성 파이프라인만 실행:

```bash
cd /home/sh/llm_robot_test
.venv/bin/python voice_command_test.py
```

ROS publish 활성화:

```bash
cd /home/sh/llm_robot_test
source /opt/ros/humble/setup.bash
colcon build --packages-select eated_interfaces
source install/setup.bash
.venv/bin/python voice_command_test.py --ros
```

다음 안내가 표시되면 명령을 말합니다.

```text
[LISTENING]
말씀해주세요.
```

프로그램은 명령 처리가 끝나면 자동으로 다시 `[LISTENING]` 상태로 돌아갑니다.
종료하려면 `Ctrl+C`를 누릅니다.

다른 터미널에서 ROS 메시지를 확인할 수 있습니다.

```bash
source /opt/ros/humble/setup.bash
source /home/sh/llm_robot_test/install/setup.bash
ros2 topic echo /voice/robot_command eated_interfaces/msg/RobotCommand
```

## ROS2 메시지 설계

- Topic: `/voice/robot_command`
- Type: `eated_interfaces/msg/RobotCommand`
- Fields: `header`, `action`, `target_class`, `transcript`
- QoS depth: 10

publisher는 다음 조건만 통과시킵니다.

- `action == "fetch"`이며 object가 `coke`, `vaseline`, `tissue`, `airpod` 중 하나
- 또는 `action == "stop"`이며 object가 빈 문자열

그 밖의 action/object 조합과 추가 필드가 있는 dict는 발행하지 않습니다.

LLM의 기존 `object` 값은 publish 직전에 `target_class`로 매핑하며, Whisper가
생성한 한국어 문장은 `transcript`에 보존합니다. `header.stamp`는 publisher의
ROS clock으로 채웁니다.

ROS 계층만 단독으로 검사하려면 다음을 실행합니다.

```bash
cd /home/sh/llm_robot_test
source /opt/ros/humble/setup.bash
source install/setup.bash
python3 test_robot_command_publisher.py
```

CLI로 custom message를 직접 보낼 수도 있습니다.

```bash
ros2 topic pub --once /voice/robot_command \
  eated_interfaces/msg/RobotCommand \
  "{action: fetch, target_class: coke, transcript: '목마른데 마실 거 가져다줘'}"
```

## VAD 및 STT 설정

- PulseAudio default source (`pactl get-default-source`)
- PCM: 16 kHz, mono, signed 16-bit
- WebRTC VAD mode: 3
- Frame: 30 ms (`480 samples`, `960 bytes`)
- Stream warm-up: 1.2초
- Start window: 300 ms 중 speech 80% 이상
- Start RMS gate: 200
- Pre-roll: 450 ms
- End silence: 900 ms
- Maximum utterance: 15초
- Whisper: `small`, CPU, `int8`, `language="ko"`

WebRTC VAD는 화자 식별 기능이 없습니다. 주변 사람의 목소리도 음성으로 감지할 수
있으므로 다중 사용자 환경에서는 wake word나 화자 인식이 추가로 필요합니다.

## LLM 설정

- Model: `qwen3:4b-instruct`
- `think: false`
- `temperature: 0`
- `keep_alive: 10m`
- JSON schema structured output
- Python-side action/object allowlist 검증

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
