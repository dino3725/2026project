# 2026-11-12 오디오 정지 안정화 2차 — 발화 구간 재검사

## 구현과 채택 범위

FP32 실시간 시험에서 마지막 “정지”가 KWS/Whisper 양쪽에서 누락됐지만 저장된 WAV를 새 스트림으로 재생하면 감지됐습니다. 기존 연속 KWS를 유지하고, VAD가 완성한 PCM을 독립된 감지기에 전달하는 보완 경로를 추가했습니다. 기본 mode=off는 유지합니다. 이번 결과는 개발 녹음의 오프라인 검증이며 실제 마이크/로봇에서 안정화를 완료한 결과가 아닙니다.

- `audio_stop.utterance_recheck=true`: mode=shadow/active일 때만 추가 감지기를 생성합니다. false로 이전 연속 감지 구조를 재현합니다. mode=off는 둘 다 끕니다.
- 마이크는 기존 한 스트림입니다. VAD가 끝낸 발화를 Whisper 대기열에 넣기 전에 보완 스레드에도 전달합니다. 디스크 WAV 저장 없이 메모리 PCM을 사용합니다.
- 보완 감지기는 발화마다 새 스트림을 만들고, 원래 PCM만 검사합니다. 앞/뒤 합성 무음, 입력 증폭, 단어 별칭, 전사 힌트는 추가하지 않았습니다. 첫 정지 감지에서 검사를 끝냅니다.
- 별도 모델 인스턴스/스레드이므로 Whisper·LLM이 처리 중이어도 검사합니다. 추가 모델 메모리와 CPU 비용이 발생합니다. 큐는 처리 중 1개 + 대기 1개로 제한하고, 넘치면 오래된 대기 발화를 버립니다.
- VAD 종료의 원래 로컬 시각으로부터 0.5초를 초과하면 재검사 결과를 버립니다. 큐 대기·추론 중·발행 직전 모두 적용하며, 종료 후 늦은 발행도 차단합니다. 기존 연속 감지/전사문 stop은 유지합니다.
- `active`에서는 기존 stop 발행 경로를 사용합니다. 로봇 상태나 LLM 준비와 무관하게 stop을 처리하고 과거/겹친 fetch를 무효화합니다. `shadow`는 두 감지 모두 기록만 하며 fetch를 무효화하지 않습니다.
- 성공적으로 발행한 동일 VAD 구간의 정지만 중복 억제합니다. 연속 감지→보완 감지, 보완 감지→연속 감지, 전사문→늦은 보완 감지의 순서 모두 검사했습니다. 발행 실패/dry-run, 이후 다른 발화, 중간 fetch 이후 정지는 막지 않습니다. 전역 cooldown은 없습니다.
- 보완 모델의 오류는 보완 경로만 종료합니다. 연속 감지와 전사문 정지는 계속합니다. LLM/Whisper 설정·정지 어휘·공유 인터페이스·Jetson 코드 변경은 없습니다.

이 경로는 **VAD 종료 무음 900ms를 기다린 뒤** 동작합니다. 연속 감지 자체의 시작 위치 민감성을 해결하거나 모든 stop을 즉시 처리하는 변경이 아닙니다. VAD가 발화로 만들지 않은 음성은 이 보완 경로에서 검사할 수 없습니다. 장시간 CPU 경합에서의 0.5초 초과 폐기 여부는 실시간 확인이 필요합니다.

## 저장된 분리 녹음 평가

기존 개발 manifest의 61개 분리 파일과 새 FP32 live의 13개, 합계 74개를 검사했습니다. 연속 50초는 별도 통합 재생으로 평가합니다.

| 구분 | 결과 |
|---|---:|
| 정지 40개 | 40/40 감지 |
| 비정지 32개 | 정지 오탐 0/32 |
| 범위 밖 정지 1개(그만 움직여) | 오디오 감지 대상 외, 별도 기록 |
| 사용자가 말하지 않은 “고맙습니다” 전사 구간 1개 | 정지 검출 없음; 음향 정답 미확정으로 정확도 분모 제외 |
| 추가 검사 시간 중앙값 / 최대 | 54ms / 158ms |
| 500ms 초과 검사 | 0/74 |

합성 무음 보충 없이 실제 런타임 `recheck_audio`를 사용했습니다. 자료는 모두 개발용이며 held-out이나 새 화자 평가가 아닙니다. 새 녹음의 정답은 안내한 발화 순서를 전제로 합니다. 새 manifest는 `audio_stop_fp32_live_dev_20261112.json`이며 WAV 해시를 검증합니다.

## 실제 VAD/Whisper/KWS의 50초 파일 통합 재생

실시간 속도 PCM 파이프로 기존 50초 연속 WAV를 공급했습니다. 초기 1200ms 폐기, VAD 시작 0.4/끝 900ms, Whisper small CPU int8, KWS FP32를 사용했습니다. LLM은 비활성이고 발행 함수는 로컬 기록 함수입니다. ROS/마이크를 사용하지 않았습니다.

| 원본 정지 순번 | 연속 KWS | 보완 KWS | 실제 로컬 stop 콜백 출처 |
|---:|---|---|---|
| 1 | 누락 | 감지 | 보완 |
| 2 | 누락 | 감지 | 보완 |
| 3 | 감지 | 감지 | 연속, 보완 중복 억제 |
| 4 | 감지 | 감지 | 연속, 보완 중복 억제 |
| 5 | 감지 | 누락 | 연속(Whisper는 슈톡) |
| 6 | 누락 | 감지 | 보완 |
| 7 | 감지 | 감지 | 연속, 보완 중복 억제 |
| 8 | 누락 | 감지 | 보완 |
| 9 | 감지 | 감지 | 연속, 보완 중복 억제 |

- 연속 KWS 5/9, 보완 KWS 8/9, 오디오 두 경로 합집합 9/9.
- 최종 로컬 stop 콜백 9회 = 연속 경로 5회 + 보완 경로 4회. 중복 발행 0회.
- 기존 구현도 전사문을 포함하면 이 파일에서 9/9였습니다. 이번 개선은 전사문을 기다리던 4회를 보완 오디오 경로에서 먼저 처리한 것입니다.
- 입력 프레임 손실 0, producer 오류 0, 보완 경로 만료/큐 폐기 0. 실제 ROS 발행 0건.
- 보완 검사 시간은 약 53~174ms. VAD 종료 이후 처리 시간이며 말끝→로봇 정지 지연이 아닙니다.
- 보완 경로도 이 VAD 구간의 5번째 발화는 놓쳤습니다. 분리 저장 파일 40/40 결과와 혼동하지 않습니다.
- 파일 EOF는 의도한 입력 종료입니다. 최종 로그 필드는 연속 감지 `frame_to_decision`, 보완 감지 `vad_end_to_decision`입니다. 이 50초 재생은 필드명 정리 전 실행돼 보완 시간에도 이전 필드명이 남아 있으며 `source=utterance_recheck`로 구분합니다.

## 최근 실시간 실패 “정지” 구간의 파이프라인 확인

`audio_stop_fp32.AYnvxO/vad_command_20261112_135734_010.wav`를 완성 발화로 주입하고 실제 보완 스레드와 Whisper를 함께 실행했습니다. 보완 경로가 VAD 종료 기준 약 136ms에 “정지”를 검출해 로컬 stop을 1회 기록했습니다. Whisper는 다시 “총지”로 읽었지만 기존 fetch 무효화가 적용됐습니다. 실제 ROS 발행은 없습니다. 이는 저장 구간 주입이며 원래 연속 마이크 세션을 복원한 실험이 아닙니다.

말하지 않은 “고맙습니다”의 STT 출력 자체는 이번 변경으로 해결하지 않았습니다. 해당 저장 구간에서 정지 오탐이 없다는 것까지만 확인했습니다. 이 문제를 막기 위해 전사문 정지 경로에 새로운 필터를 넣지 않았습니다.

## 자동 검증

- 노트북 패키지 2개 빌드 성공.
- 음성 패키지 312 passed / 1 skipped. flake8/pep257 통과, 기존 copyright skip.
- 평가 도구 기존 테스트 27 passed.
- 신규 검증: 독립 검사 중 LLM 차단과 늦은 fetch 무효화, shadow의 관찰 전용 동작, 성공/실패별 양방향 중복 처리, 전사문 우선 완료, 새로운 stop 허용, 큐 상한, 오래된 입력/추론 중 만료, 종료 중 추론, 모델 오류 격리, off/기능 스위치, 원본 오디오 좌표.
- 검사 과정의 줄 길이 오류를 수정한 후 전체 패키지 검사를 통과했습니다. 모델/프롬프트 변경 및 held-out 실행은 없습니다.

## 재현과 다음 실시간 확인

오프라인 재현(노트북, 로봇 연결 불필요):

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/evaluate_audio_stop_recheck.py \
  --manifest tools/voice_eval/audio_stop_stability_dev_20261112.json \
  tools/voice_eval/audio_stop_fp32_live_dev_20261112.json \
  --output log/audio_stop_recheck_confirm.json
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/replay_audio_stop_pipeline.py \
  --wav recordings/stop_probe.oIcb6b/continuous.wav --precision fp32 \
  --output log/audio_stop_recheck_pipeline_confirm.json
```

정상 기준: 분리 정지 40/40, 비정지 오탐 0/32. 통합 재생은 정지 원본 9개와 대응한 stop 콜백 9회, 입력 손실/보완 폐기 0. 로컬 callback 성공은 실제 로봇 수신 성공이 아닙니다. 이전 구조 비교는 두 번째 명령에 `--no-utterance-recheck`를 추가합니다.

새 보완 경로의 마이크 동작은 아래 shadow로 사람이 확인합니다. 기존 음성 노드는 Ctrl+C로 종료하고 하나만 실행합니다. 비교 시 CPU 부하를 맞추기 위해 `[LLM READY]` 후 시작하되, 정지 기능 자체가 LLM 준비를 기다리는 구조는 아닙니다.

```bash
cd ~/2026project
source voice_env.sh
export ROS_DOMAIN_ID=37
mkdir -p recordings
KWS_RECHECK_DIR=$(mktemp -d "$PWD/recordings/audio_stop_recheck.XXXXXX")
ros2 run voice_command_pkg voice_command_node --ros-args \
  --params-file src/voice_command_pkg/config/voice_command.yaml \
  -p publish_commands:=false \
  -p audio_stop.mode:=shadow \
  -p audio_stop.precision:=fp32 \
  -p audio_stop.utterance_recheck:=true \
  -p save_recordings:=true \
  -p recordings_dir:="$KWS_RECHECK_DIR" 2>&1 | tee "$KWS_RECHECK_DIR/session.log"
```

순서: “스톱” 3회 → “멈춰” 3회 → “정지” 3회 → “콜라 가져다줘” → “휴지 가져다줘” → “안녕하세요” → 말하지 않고 생활 소음 30초. 발화 사이는 5초 정도 둡니다. 끝나면 Ctrl+C 후 같은 터미널:

```bash
grep -E '\[(AUDIO STOP[^]]*|STT|COMMAND|ROS|DROPPED|ERROR|LLM READY|TIME)\]' \
  "$KWS_RECHECK_DIR/session.log"
echo "결과 위치: $KWS_RECHECK_DIR"
```

정상 기준: RECHECK READY, 정지 9개 각각에 stream 또는 utterance_recheck 감지, 일반 문장/생활 소음에서 오디오 정지 오탐 없음, 모든 published=False와 ROS skipped, RESET/RECHECK DROP/ERROR 없음. shadow는 두 경로 이벤트를 모두 기록하므로 로그 줄 수만 합산하지 않습니다. “총지” 전사와 COMMAND unknown이 남아도 같은 발화에 보완 정지 로그가 있는지 확인합니다. 새 마이크 결과 전에는 안정화 완료나 active 운영 채택으로 표시하지 않습니다.

### 로봇에서 확인할 항목

- [ ] 이번에는 노트북 shadow 확인만 수행합니다. Jetson 실행/빌드 명령은 없습니다.
- [ ] 실제 명령 발행과 팔/바퀴 정지 시험은 이번 범위에서 실행하지 않았습니다.
- [ ] 후속 통합 시험에서 실제 수신·모든 액션 취소·새 fetch 허용·물리적 지연은 사람이 별도로 확인합니다.

## 로컬 증거 파일 SHA256

| 파일 | SHA256 |
|---|---|
| log/audio_stop_recheck_20261112/clips.json | `c02713c56737f89e55de7310316260457d5aa69e3a7a6c4a82d7960dd0d61e99` |
| log/audio_stop_recheck_20261112/pipeline.json | `c5fcfb8fd3aa0e34f23c6f02c40d1a79077a75050f2bb413cbe1015b1d0e57af` |
| log/audio_stop_recheck_20261112/last_stop_pipeline.json | `db7ed0c5c8d50f2cfe45e8ee955ee53e926cdfe520cc78f07de9f0c2ab6a69ac` |
| log/audio_stop_recheck_20261112/colcon_test.txt | `342c1f672fc7300aec68baf18da9ac302499a5439ef36b204234c97c6bc368b5` |
