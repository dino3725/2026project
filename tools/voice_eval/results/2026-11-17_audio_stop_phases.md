# 2026-11-17 정지 발화 재검사의 입력 위치 보완

## 구현과 범위

기존 실패 녹음을 사용해 보완 감지의 입력 시작 위치 민감성을 재현했습니다.
`audio_stop.recheck_phase_shifts=true`일 때 원본을 먼저 검사하고, 실패하면 앞에
120/300/240/180/60ms의 합성 무음을 붙인 새 스트림을 차례로 검사합니다. 실제 PCM을 자르거나
증폭하지 않으며 뒤에 무음을 보충하지 않습니다. 최초 성공에서 종료합니다.

- 기본 `audio_stop.mode=off`, `recheck_phase_shifts=false` 유지. 새 옵션은 실험용입니다.
- 모든 시도는 원래 VAD 종료 시각부터 같은 0.5초 제한을 공유합니다. 재시도별로 시간을
  연장하지 않으며 늦게 끝난 추론 결과는 폐기합니다. native 추론 한 번의 실행 자체를
  강제 중단하는 것은 아니므로 함수가 돌아오는 시간은 0.5초를 조금 넘을 수 있습니다.
- 추가한 무음을 빼고 원래 오디오 좌표로 이벤트를 만들며, 합성 앞 무음 안의 감지는
  실제 발화 성공으로 인정하지 않습니다. 기존 같은 발화 중복 억제·오래된 fetch 차단 유지.
- 연속 KWS와 Whisper 전사문 정지 경로, 화자 인증 예외, 로봇 상태 검사는 유지합니다.
- VAD가 발화를 완성한 뒤 실행하는 보완입니다. **종료 무음 900ms 대기를 제거하거나
  연속 KWS의 위치 민감성 자체를 해결한 것이 아닙니다.**
- 실험은 파일 입력과 로컬 콜백만 사용했습니다. ROS 명령/마이크/로봇 실행은 없습니다.

## 자료와 비교 결과

기존 stability/FP32 manifest에 최신 recheck 시험 12개와 화자 등록·개발 자료의
일반 명령 40개를 추가했습니다. WAV 및 PCM 해시를 확인하고 중복 PCM을 거부합니다.
화자 등록 20개·본인 개발 10개·동의한 타인 개발 10개는 정지 오탐 대조군으로만 사용합니다.
새 manifest는 `audio_stop_phase_dev_20261117.json`입니다. held-out은 사용하지 않았습니다.

| 구분 | 기존 원본 재검사 | 입력 위치 보완 | 보완 + 0.5초 제한 |
|---|---:|---:|---:|
| 정지 49개 | 46/49 | 49/49 | 49/49 |
| 비정지 75개 | 오탐 0/75 | 오탐 0/75 | 오탐 0, 60개 검사 만료 |
| 범위 밖 정지 ‘그만 움직여’ 1개 | 감지 0 | 감지 0 | 감지 0 |
| 말하지 않은 ‘고맙습니다’ 전사 구간 1개 | 감지 0 | 감지 0 | 감지 0 |

마지막 두 행은 정지 대상/비정지 정확도 분모와 분리합니다. ‘그만 움직여’는 기존
전사문 규칙의 대상이고, KWS 어휘를 확장하지 않았습니다. ‘고맙습니다’ 전사 자체의
음향 원인은 미확정입니다. 10초 합성 무음에서 오디오 정지 검출은 없었습니다.

최근 recheck 녹음의 1·2·7번을 원본에서는 다시 놓쳤습니다. 원본에 앞 무음만 붙이면
1번은 300ms, 2번은 120ms, 7번은 300ms에서 감지됐습니다. 후보는 이 실패 자료를
보고 선택했으므로 모든 수치는 **개발 결과**이며 독립 정확도나 새 환경 보장이 아닙니다.

첫 0/120/300ms 후보에서는 정지 49개에서 제한을 적용한 보완 검사 시간 중앙값
약 41ms, 최대 약 179ms였습니다. 6초 일반 명령 파일 40개에서는 무제한 보완 검사에
약 0.7초가 걸려 제한 적용 시 40개가 만료됐습니다. 만료는 정상적인 자원 제한 동작이며, ‘모든 검사가 완료됐다’고
해석하지 않습니다. 비정지 오탐 0/75는 별도의 무제한 완주 비교에서도 확인했습니다.

최종 여섯 위치 후보도 원본 정지 49/49를 제한 안에 감지했습니다(중앙값 약 41ms,
최대 약 262ms). 비정지 무제한 검사의 중앙값은 약 1.48초로 늘었고,
0.5초 제한 적용에서는 비정지 75개 중 60개가 만료됐습니다. **위치 보완의 대가는
추가 CPU 사용이며, 이 비용 때문에 기본 옵션을 켜지 않았습니다.** 검사 만료가 STT나
기존 정지 처리를 중단시키지는 않지만, 임의의 더 긴 정지 음성에서도 모든 위치를
검사할 수 있다는 보장은 없습니다.

## 입력 위치를 바꾼 추가 비교와 최종 후보

49개 정지 각각의 입력 앞에 0/60/120/180/240/300ms를 붙여 총 294회를 비교했습니다.
이는 동일 녹음의 변형 294회이며 독립 발화 294개가 아닙니다.

| 재검사 방식 | 감지 / 294 | 제한 적용 시 만료 |
|---|---:|---:|
| 원본 1회 | 279/294 | 제한 없는 기준 비교 |
| 첫 후보 0/120/300ms | 291/294 | 0 |
| 최종 후보 0/120/300/240/180/60ms | 294/294 | 0 |

첫 후보는 최신 첫 스톱에 추가 앞 무음 60/120/240ms가 있는 조건을 놓쳤습니다.
남은 고정 위치도 예산 안에서 확인하도록 추가했습니다. 최종 후보의 제한 적용 검사
시간 중앙값 약 54ms, 최대 약 396ms였습니다. 위치 시험은 KWS 단독 실행이며
이 최댓값을 STT/화자 동시 실행 또는 실제 로봇 지연 수치로 해석하지 않습니다.
추가 위치 역시 실패 자료를 보고 정한 개발 선택이므로 독립 검증으로 주장하지 않습니다.

## 첫 후보의 STT·화자 인증 동시 실행

`check_audio_stop_concurrency.py`로 실제 Whisper small + ECAPA 화자 런타임 +
재검사 스레드를 실행했습니다. 입력은 완성된 WAV 주입이며 IDLE/LLM은 모의,
명령 발행은 로컬 기록 콜백입니다. 실제 연속 마이크나 LLM CPU 부하 시험은 아닙니다.

- 최신 정지 9개: 보완 감지 6/9 → 9/9. 기존에서 놓친 3개는 전사문 규칙이 처리했습니다.
- 최종 로컬 stop 콜백은 두 설정 모두 9회, 각 발화 1회. 추가 오디오 감지의 의미는
  기존 전사문을 기다리던 3개를 먼저 처리한 것이며, 최종 성공 6→9라는 뜻이 아닙니다.
- 새 보완의 VAD 종료→로컬 정지 콜백 약 78~291ms. 정지 9개 모두 0.5초 안에 처리.
- 본인 가져오기 2개는 허용, 타인 가져오기 2개는 `speaker_rejected`로 차단.
- 6초 비정지 4개는 기존에서 3개, 새 설정에서 4개 보완 검사 만료. STT/화자 처리와
  본인 허용·타인 차단은 계속 정상 수행됐습니다.
- STT 시간은 기존 정지 약 1.62~1.75초, 새 설정 약 1.77~1.94초였습니다.
  두 조건을 순차 실행한 단일 비교이므로 증가 원인을 단정하지 않습니다. 추가 CPU
  비용은 존재하며 ‘STT 지연 영향이 없다’고 주장하지 않습니다.

최종 여섯 위치 후보로 같은 동시 시험을 다시 수행했습니다. 보완 감지는 기존
6/9 → 최종 9/9, 정지 콜백 발화당 1회, 본인 2개 허용/타인 2개 차단이 유지됐습니다.
정지 보완 계산 약 54~315ms이며 이전 누락 1·2·7번의 VAD 종료→로컬 콜백은
각각 약 239/194/316ms였습니다. 일반 명령 4개는 두 설정 모두 보완 검사 만료였습니다.
최종 비교의 STT는 기존 약 1.70~1.78초, 최종 약 1.77~1.86초입니다.

## 첫 후보의 50초 연속 입력과 실제 VAD 통합

`stop_probe.oIcb6b/continuous.wav`를 실제 속도 PCM 파이프로 넣고 초기 1.2초 폐기,
실제 VAD/연속 KWS/보완 KWS/Whisper/ECAPA를 함께 실행했습니다. LLM은 비활성입니다.

- 기존/새 설정 모두 연속 감지 5/9, 최종 로컬 stop 콜백 9회(연속 5 + 보완 4).
- 보완 자체 감지는 8/9 → 9/9. 5번째 발화가 추가 감지됐지만 이미 연속 경로가
  처리한 정지이므로 중복 콜백을 만들지 않았습니다.
- 새 보완 계산 약 49~328ms. 보완 만료/큐 폐기/모델 오류/입력 프레임 손실 0.
- producer 오류 0. 파일 EOF는 의도한 입력 종료이며 마이크 장애 해결을 뜻하지 않습니다.
- 화자 모델은 준비 후 실제 추론을 실행했지만, 여기 입력은 정지 발화여서 화자 통과율
  평가가 아닙니다. 실제 화자 허용·차단 확인은 위 완성 발화 동시 시험과 구분합니다.

최종 여섯 위치 후보로 50초 연속 입력도 다시 실행했습니다. 보완 9/9, 로컬 정지
콜백 9회, 입력 프레임 손실/producer 오류/보완 만료 0을 확인했습니다. 최종 보완
계산은 약 57~290ms였습니다. 합성 Gaussian PCM 대조군도 seed=20261117,
RMS 0/100/500/2000, 각 10초를 무제한 완주 검사해 오디오 정지 0/4를 확인했습니다.
이는 실제 생활 소음 데이터가 아니며 실제 소음 오탐률로 해석하지 않습니다.

## 자동 검증

- 노트북 패키지 2개 빌드 성공.
- 음성 패키지 361 passed / 1 skipped. colcon test-result 오류/실패 0.
- 평가 도구 60 passed, 변경 평가 도구 flake8 통과.
- 새 회귀 검사: 원본 성공 즉시 반환, 모든 고정 위치의 실제 PCM 보존, 합성 앞 무음
  감지 제외, 부분 마지막 프레임의 좌표, 재시도 중 취소, worker의 원래 시간 구간,
  옵션 기본 off, 평가 만료 결과 제외와 같은 음성의 반복 분모 구분.
- 기존 큐 상한/종료 중 추론/시간 초과/오류 격리/정지 중복 처리/화자 예외 검사 유지.
- 실험 중 전체 검사도 실행한 일반 데스크톱 환경이며, 전용 무부하 CPU 벤치마크는 아님.

## 재현 명령

노트북에서만 실행하며 Jetson은 필요 없습니다. 아래 도구는 마이크와 ROS를 사용하지 않습니다.

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/evaluate_audio_stop_phases.py \
  --manifest tools/voice_eval/audio_stop_stability_dev_20261112.json \
  tools/voice_eval/audio_stop_fp32_live_dev_20261112.json \
  tools/voice_eval/audio_stop_phase_dev_20261117.json \
  --output log/audio_stop_phase_repeat.json

PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/check_audio_stop_concurrency.py \
  --manifest tools/voice_eval/audio_stop_phase_dev_20261117.json \
  --speaker-profile speaker_profiles/owner_ecapa_development.json \
  --output log/audio_stop_concurrent_repeat.json

PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/evaluate_audio_stop_phases.py \
  --manifest tools/voice_eval/audio_stop_stability_dev_20261112.json \
  tools/voice_eval/audio_stop_fp32_live_dev_20261112.json \
  tools/voice_eval/audio_stop_phase_dev_20261117.json \
  --targets-only --prefix-ms 0 60 120 180 240 300 \
  --output log/audio_stop_offsets_repeat.json

PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/replay_audio_stop_pipeline.py \
  --wav recordings/stop_probe.oIcb6b/continuous.wav \
  --speaker-profile speaker_profiles/owner_ecapa_development.json \
  --allow-development-profile --recheck-phase-shifts \
  --output log/audio_stop_continuous_repeat.json
```

## 로봇에서 확인할 항목

이번 후속은 **노트북 shadow만** 확인합니다. Jetson에서 실행할 명령이나 빌드는 없습니다.
기존 음성 노드는 Ctrl+C로 종료하고 아래를 실행합니다. 모든 명령 발행은 꺼져 있습니다.

```bash
cd ~/2026project
source voice_env.sh
export ROS_DOMAIN_ID=37
export ROS_LOCALHOST_ONLY=1
mkdir -p recordings
KWS_PHASE_DIR=$(mktemp -d "$PWD/recordings/audio_stop_phases.XXXXXX")
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file src/voice_command_pkg/config/voice_command.yaml \
  --params-file src/voice_command_pkg/config/speaker_development.yaml \
  -p publish_commands:=false \
  -p audio_stop.mode:=shadow \
  -p audio_stop.utterance_recheck:=true \
  -p audio_stop.recheck_phase_shifts:=true \
  -p save_recordings:=true \
  -p recordings_dir:="$KWS_PHASE_DIR" 2>&1 | tee "$KWS_PHASE_DIR/session.log"
```

1. `[AUDIO STOP RECHECK READY] ... phase_shifts=True`, `[SPEAKER READY]`, `[LLM READY]` 대기.
2. 평소 속도로 스톱 3회, 멈춰 3회, 정지 3회. 발화 사이는 5초 정도 둡니다.
3. 콜라 가져다 줘 / 휴지 가져다 줘 / 안녕하세요 각 1회, 이후 생활 소음만 30초.
4. Ctrl+C 후 같은 터미널에서 아래 결과와 폴더 위치를 확인합니다.

```bash
grep -E '\[(AUDIO STOP[^]]*|STT|COMMAND|ROS|DROPPED|ERROR|SPEAKER READY|LLM READY)\]' \
  "$KWS_PHASE_DIR/session.log"
echo "결과 위치: $KWS_PHASE_DIR"
```

- [ ] 정지 9개 각각에 stream 또는 utterance_recheck 감지. 이벤트 줄 수를 단순 합산하지 않음.
- [ ] 일반 명령과 소음 구간에 오디오 정지 오탐 없음.
- [ ] 정지 발화에서 RECHECK DROP/ERROR 없음, 모든 `published=False`와 `[ROS] skipped`.
- [ ] Jetson 미연결의 `robot_unknown`/`speaker_not_checked`는 이번 감지 시험 실패가 아님.
- [ ] 실제 생활 소음 연속 음원은 현재 자료에 충분히 남아 있지 않아 이번 자동 시험으로
  생활 소음 안정화 완료를 주장하지 않음. 실제 마이크 결과 전에는 active 운영을 권하지 않음.

## 로컬 증거

`log/audio_stop_phase_20261117/` 아래 파일은 로컬에 보관하며 Git에 올리지 않습니다.
첫 후보: `clips.json`, `offset_stress.json`, `concurrent.json`, `continuous_phases.json`.
최종 후보: `clips_six.json`, `offset_stress_six.json`, `concurrent_six.json`,
`continuous_six.json`, `synthetic_controls.json`. 기존 연속 입력 기준은
`continuous_baseline.json`입니다. 전체 테스트 기록은 `colcon_test_final.log`,
`tools_tests.log`에 있습니다. 모델 가중치·음성·화자 프로필은 변경/커밋하지 않았습니다.

## 후속 실제 마이크 시험 — CUEVO5

사용자가 중간에 옆사람 소음이 들어왔다고 보고했습니다. 원본은
`recordings/audio_stop_phases.CUEVO5/session.log`와 WAV 15개입니다. 사용자는 후속 답변에서
스톱 3회 → 멈춰 3회 → 정지 3회를 순서대로 말했다고 확인했습니다. 아래 집계는 이 순서와
로그의 대응을 기준으로 합니다. 추가 VAD 구간의 실제 발화 내용·화자는 여전히 미확정입니다.

| 저장 발화 순번 | 실시간 STT | 연속 KWS | 보완 KWS | 전사문 정지 |
|---|---|---|---|---|
| 1 | 스톱 | 감지 | 감지 | 감지 |
| 2 (두 번째 스톱에 대응) | 빈 전사 오류 | 없음 | 만료 | 없음 |
| 3 | 어이구 스톱 | 없음 | 만료 | 감지 |
| 4 | 감사합니다. | 없음 | 만료 | 없음 |
| 5 | 빈 전사 오류 | 없음 | 만료 | 없음 |
| 6·7·8 | 멈춰 | 각 감지 | 각 감지 | 각 감지 |
| 9 | 왜 이렇게 집중되는 거야? 정지 | 감지 | 감지 | 감지 |
| 10 | 정지 | 감지 | 감지 | 감지 |
| 11 | 정지 | 없음 | 감지 | 감지 |
| 12 | 콜라 가져다 줘 | 없음 | 만료 | 없음 |
| 13 | 빈 전사 오류 | 없음 | 만료 | 없음 |
| 14 | 휴지 가져다 줘 | 없음 | 만료 | 없음 |
| 15 | 안녕하세요 | 없음 | 만료 | 없음 |

- 연속 이벤트 6건, 보완 7건, 오디오 감지된 서로 다른 발화는 7개입니다.
  두 경로의 13줄을 정지 13회 성공으로 합산하지 않습니다. 전사문 stop은 8개입니다.
- 사용자 확인 순서 기준 오디오 합집합은 스톱 1/3, 멈춰 3/3, 정지 3/3으로 7/9입니다.
  전사문 경로까지 합치면 스톱 2/3, 멈춰 3/3, 정지 3/3으로 8/9입니다. 두 번째 스톱은
  두 경로 모두 누락, 세 번째는 전사문만 감지했습니다. shadow 관찰 결과이며 실제 로봇
  정지 성공률을 뜻하지 않습니다. 전체 시험은 통과로 판정하지 않습니다.
- 보완 만료 8건(2·3·4·5·12·13·14·15), STT 빈 전사 오류 3건(2·5·13).
  입력 프레임 폐기로 인한 AUDIO STOP RESET, 보완 큐 초과, 오디오 모델 오류는 없습니다.
- 보완이 마지막 정지(11번)를 연속 경로에 추가했습니다. 실시간 보완 결정 지연은
  VAD 종료 기준 약 97~220ms입니다. 실제 말끝→물리 정지 지연이 아닙니다.
- `shadow`, 전 이벤트 published=False, ROS skipped 12건입니다. 실제 명령 발행 0건.
- 일반 명령 12·14·15는 robot_unknown으로 차단됐습니다. speaker_not_checked는
  로봇 상태에 의한 차단이며 타인 인증 거부로 해석하지 않습니다.
- 6번의 speaker_audio_too_short에도 stop_bypass로 정지가 처리됐습니다.
- ‘감사합니다’가 실제 옆사람의 말인지 STT 오전사인지는 미확정입니다. 앞선 시험의
  ‘고맙습니다’ 오전사와 같은 원인이라고 단정하지 않습니다.

### LLM 준비와 겹친 초반 구간

LLM 준비는 22:55:01.787부터 22:56:08.126까지 66.338초였습니다.
첫 스톱의 VAD 시작은 22:55:12.950으로 준비 완료 이전입니다. 이 구간의 성공 전사
6건 STT 중앙값은 3.260초(3.134~3.561), 준비 완료 이후 성공 전사 6건은 2.0305초
(1.825~2.129)입니다. 문장 길이·소음도 달라 통제된 속도 비교는 아니지만 CPU 경합
가능성이 있습니다. 정지 기능은 LLM 준비 여부와 무관하게 처리되어야 하므로,
시험 시작 시점만 바꾸면 문제가 해결된다고 보지 않습니다.

### 같은 15개 WAV 재생

실시간 녹음이 끝난 뒤 실제 KWS로 원본 1회/여섯 위치 무제한/여섯 위치 0.5초 제한을
각각 재생했습니다. 세 조건 모두 1·6·7·8·9·10·11번에서만 감지됐습니다.
2번은 무제한 검사 약 394ms, 3번은 약 762ms까지 완주해도 감지되지 않았습니다.
따라서 **시간 제한을 늘리는 것만으로 이 두 입력을 해결할 근거가 없습니다.**
사용자 순서 확인에 따라 2번을 두 번째 스톱에 대응하는 실패 구간으로 기록합니다.
3번은 실시간 전사문에 스톱이 있으나 KWS가 놓친 사례로 별도 분석합니다.

일반 요청·인사와 추가 불명 구간에서 재생 중 정지 감지는 없었습니다. 다만 생활 소음
30초 전체의 연속 원음/종료 시각이 로그에 없어 전체 소음 시험 통과로 판정하지 않습니다.
새 녹음 반복이나 active 전환을 요청하지 않고 현재 실패 WAV를 후속 분석에 사용합니다.
이번에는 설정·임계값·코드를 변경하지 않았습니다. mode=off/위치 보완 false를 유지합니다.

근거: `log/audio_stop_phase_live_20261117/replay.json`(15개 WAV SHA256 및 개별 결과 포함).
원본 session.log SHA256: `21fa80257023b126fd2743d454276c39e9ae46ff42c0ffb0bce832c177a16e18`.
