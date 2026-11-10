# LLM 워밍업·모델 유지·장애 격리 구현 결과 — 2026-11-11

## 범위와 구현

노트북의 `voice_command_pkg`에 프로젝트 계획 5.3 ②·③을 구현했다.
작업 브랜치는 `feature/voice-llm-readiness`이며 앞선 상시 녹음·메모리 전사 작업을
유지했다. 이번 작업에서 커밋·push는 수행하지 않았다.

- `llm_runtime.py`: 클라이언트 HTTP 호출을 맡는 스레드 하나를 추가했다.
  실제 분류와 동일한 프롬프트·모델·스키마·생성 옵션으로 더미 문장을 처리한다.
  워밍업 결과는 반환/발행하지 않는다. 성공 후에만 준비 상태와 준비 완료 시각을 설정한다.
- `pipeline.py`: 시작 시 Ollama 연결 검사를 통과해야만 녹음할 수 있던 의존을 제거했다.
  Whisper 로드 뒤 `[STT READY]`를 표시하고, LLM은 별도로 준비한다.
  stop 규칙은 LLM 준비 상태와 무관하게 전사 후 가장 먼저 적용한다.
- 일반 요청은 준비 상태뿐 아니라 발화 시작 시각(pre-roll 포함)을 확인한다.
  준비 중 요청 및 준비 전에 시작한 대기 발화는 버린다. 복구 뒤 다시 실행하지 않는다.
- `keep_alive` 기본값을 정수 `-1`로 바꿨다. 일반 요청 제한은 10초이며,
  남은 fetch 유효시간이 더 짧으면 그 시간을 적용한다. 준비 요청은 별도 120초다.
- 일반 요청의 대기 기한이 지나면 즉시 그 결과를 포기한다. HTTP 호출이 늦게 반환해도
  발행 경로로 보내지 않는다. 실패한 클라이언트 호출이 끝난 뒤 최소 5초를 기다리고
  워밍업으로 복구한다. 워커나 요청을 계속 추가하는 방식은 사용하지 않았다.
- 준비·완료·실패 안내는 `messages.llm_*` YAML 템플릿으로 관리한다.
  `[LLM TIME]`에 요청 전체/load/prompt evaluation/generation 시간을 남긴다.
- 기존 `measure_baseline.py`는 cold 비교를 재현할 수 있도록 과거 120초/10m를 명시했다.
  현재 동작의 측정 도구는 `measure_llm_readiness.py`다.

모델·프롬프트·출력 스키마·Whisper 설정·VAD 기준·정지 키워드는 바꾸지 않았다.
공유 ROS 인터페이스 및 Jetson 코드는 변경하지 않았다.

## 자동 검증

노트북 패키지 빌드:

```bash
source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-up-to voice_command_pkg
source install/setup.bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 colcon test --packages-select voice_command_pkg --event-handlers console_direct+
colcon test-result --verbose
```

- `eated_interfaces`, `voice_command_pkg`: 빌드 성공.
- 패키지 테스트 188개: **187 passed, 1 skipped, 0 failures**.
  기존 copyright 테스트가 skip이다. flake8/pep257는 통과했다.
- 새 `test_llm_readiness.py`: 매개변수화 포함 26개 통과.
  준비 요청의 발행 차단, 준비 전 발화 거절, 연결/JSON 오류 복구,
  재시도 간격, 기한 후 성공 결과 폐기, 단일 클라이언트 요청 유지,
  남은 발화 유효시간 적용, 종료 중 대기 해제, 종료 후 준비 완료 표시 차단,
  오래 걸린 워밍업의 준비 상태 차단, 장애/워밍업 중 stop 처리를 확인했다.
- 파이프라인 통합 테스트에서 가짜 publish callback을 사용하여
  준비 요청·준비 전 fetch·기한 초과 fetch는 발행되지 않고 stop만 전달되는 것을 확인했다.
- 새 실측 도구도 flake8 통과. Python 3.10 문법 및 `git diff --check` 확인.

처음 저장소 루트에서 직접 pytest를 호출했을 때 lint가 build/install 생성 코드까지
검색해 실패했다. 패키지 범위에서 재확인했고, 새 테스트의 빈 줄 오류 1개를 수정한 후
위 colcon 검증을 통과했다. 기능 테스트 실패는 없었다.

## 실제 Whisper/Ollama 측정

실제 노트북에서 기존 개발용 WAV 3개를 사용했다. held-out 평가는 재실행하지 않았다.
Ollama 0.33.3, `qwen3:4b-instruct` Q4_K_M, CPU, context 2048,
Whisper small/CPU/int8/ko 조건이다.

모델 digest:
`0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`

프롬프트 SHA256(기존 동결 값과 동일):
`b184e9bc9b5c26ef822a79d2d18025aaecbde07f014325f2515817b653ff8df6`

첫 실행 직전 `/api/ps`는 `models: []`였다. 모델을 강제로 unload하거나 정상
Ollama 서비스를 중단하지 않았다. 워밍업이 시작된 뒤 실제 WAV를 메모리로 전사했다.
`publish=None`이며 ROS 노드·DDS 발행·마이크 캡처를 실행하지 않았다.

첫 실행의 cold 워밍업은 **49.536초**였다. Ollama 지표는 load 5.894초,
prompt evaluation 42.230초, generation 1.382초다. 이 비용은 사용자 fetch가 아니라
준비 단계에서 소비됐다. 준비 중에는 전사문 stop을 계속 처리했다.

| 상황 | STT | LLM 대기 | PCM 처리 시작→판정 | 결과 |
| --- | ---: | ---: | ---: | --- |
| cold 워밍업 중 “멈춰” | 2.466 s | 0 | 2.467 s | rule:멈춰 / stop |
| 준비 완료 후 첫 콜라 | 1.823 s | 1.944 s | 3.767 s | fetch/coke |
| 다음 휴지 | 1.816 s | 2.139 s | 3.956 s | fetch/tissue |
| 60초 유휴 후 콜라 | 1.757 s | 2.131 s | 3.889 s | fetch/coke |
| 연결 실패 중 “멈춰” | 1.957 s | 0 | 1.958 s | rule:멈춰 / stop |
| 연결 실패 중 콜라 | 1.883 s | 0 | 1.883 s | llm_not_ready / 폐기 |

시간은 녹음 파일 PCM을 처리하기 시작한 시각부터이며 발화 시간·실제 VAD 종료 무음·
마이크 대기열·DDS·로봇 반응을 포함하지 않는다. 실제 사용의 종단 정지 시간과 같지 않다.
준비 과정과 STT가 CPU를 공유하므로 워밍업 중 STT가 평소보다 느려질 수 있다.

60초 후 `/api/ps`에도 모델이 남았고 `expires_at`은 2319년으로 표시됐다.
유휴 후 요청의 load는 약 0.001초였다. 이는 `keep_alive: -1` 수용 및 60초 유지 확인이다.
10분 이상 방치 시험이나 재부팅 후 유지, 메모리 압박 상황까지 검증한 것은 아니다.

연결 실패는 `http://127.0.0.1:1`로 분리해 실제 connection refused를 발생시켰다.
정상 Ollama 서비스에는 영향을 주지 않았다.

추가 실행에서는 fetch를 stop보다 먼저 처리하도록 측정 순서를 바꿔,
준비 중 일반 요청이 `llm_not_ready`로 폐기되는 것을 실제 전사로 확인했다.
이미 모델이 올라온 상태의 워밍업은 3.336초였다. 이후 콜라/휴지는 각각
3.614초/3.876초에 판정됐다. 이 실행은 cold 측정이나 유휴 시험으로 세지 않는다.
준비 완료 후 대기열에서 꺼낸 옛 발화의 `llm_before_ready` 차단은 자동 테스트로 검증했다.

원본 자료(로컬 log/, 커밋 제외):

- `log/llm_readiness_20261111/measurement.json`: cold + 60초 유휴 + 연결 실패.
  초기 측정 도구가 stop을 먼저 처리했으므로 `fetch_recorded_before_ready` 행은
  `before_stop`으로 폐기됐다. 이 행을 LLM 준비 상태 검증 증거로 사용하지 않았다.
- `log/llm_readiness_20261111/measurement_warm.json`: 순서를 수정한 현재 도구의 실행.
  일반 요청의 `llm_not_ready` 폐기를 별도로 확인했다. 유휴 설정은 0초다.
- JSON에는 파일 SHA256, 실제 전사문, 세부 시간, 발행 여부와 전체 로그가 포함된다.
  모든 행의 `published`는 false이며 WAV를 추가 저장하지 않았다.

현재 도구 재현 예시(다른 출력 파일명을 사용할 것):

```bash
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/measure_llm_readiness.py \
  --stop-wav recordings/vad_command_20261111_001252_006.wav \
  --fetch-wavs recordings/vad_command_20261111_001212_002.wav recordings/vad_command_20261111_001239_005.wav \
  --output log/llm_readiness_repeat.json --idle-seconds 60
```

## 남은 범위와 로봇에서 확인할 항목

일반 요청의 결과 대기는 제한하지만 서버 측 추론 취소를 보장하지 않는다.
urllib의 소켓 제한은 무응답 시간 기준이므로 HTTP 호출 자체가 더 오래 남는 경우도 있다.
이 경우 음성 처리는 계속하고 추가 클라이언트 호출을 겹쳐 만들지 않으며 늦은 결과를 버린다.
LLM 일반 요청 뒤에 녹음된 stop은 앞선 요청의 제한 시간 및 자신의 STT를 기다릴 수 있다.
오디오 정지 감지, 화자 인증, 상태 구독/IDLE 게이트는 이번 구현 범위에 포함하지 않았다.

- [ ] 사람이 노트북에서 `publish_commands:=false`로 실행해 `[STT READY]`,
  `[LLM WARMUP]`, `[LLM READY]`와 실제 마이크의 상시 수집을 확인한다.
- [ ] 준비 중 “콜라 가져다줘”는 폐기되고, “멈춰”는 규칙으로 판정되는지 확인한다.
- [ ] 준비 완료 후 새 콜라/휴지 요청의 `[TIME]`을 확인하고, Ctrl+C 후 늦은 발행이 없는지 확인한다.
- [ ] 노트북 dry-run 후 사람이 Jetson과 연결해 준비 중/실패한 fetch가 전달되지 않고,
  stop의 액션 취소·베이스/팔 정지가 유지되는지 실물 확인한다.

## 사용자 확인 및 작업 마무리 — 2026-11-11

사용자가 안내된 노트북 시험과 Jetson 실물 연동 시험이 모두 정상적으로
진행됐다고 보고했다. 이 항목은 사용자 확인 기록이며, 에이전트가 직접
실물 동작을 실행하거나 추가 로그를 분석한 결과와 구분한다.

- 노트북: 워밍업 중 요청 폐기, 준비 완료 후 정상 분류, 모델 유지,
  연결 실패·타임아웃 처리 및 종료 확인.
- Jetson: fetch 수신과 작업 시작, 음성 stop에 따른 실제 정지,
  ERROR 상태 및 X 버튼을 통한 HOME 복구 확인.
- 자동 검증: 노트북 대상 패키지 2개 빌드 성공,
  음성 패키지 테스트 187개 통과·기존 copyright 테스트 1개 건너뜀.

다음 개발은 프로젝트 계획 5.4의 상태 구독·결과 피드백·fetch 발행 조건이다.
`/control/state`와 `/control/command_status`를 구독하고, IDLE에서만
새 fetch를 허용하며 stop은 로봇 상태와 관계없이 발행 가능하도록 한다.
이 다음 기능은 아직 구현하지 않았다.
