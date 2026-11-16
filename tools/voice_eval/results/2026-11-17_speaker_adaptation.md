# 기존 녹음을 이용한 본인 화자 거부 개선 — 2026-11-17

## 결론

사용자가 타인은 차단되지만 본인도 자주 거부된다며 기존 본인 녹음으로 학습/시험을 요청했습니다.
저장된 본인 발화에서 현상을 재현했습니다. 기존 Resemblyzer 프로필에 음성을 단순 추가해
평균을 내는 후보는 개선이 부족했습니다. SpeechBrain ECAPA + 무음 정리 후보가 나아
기존 등록 20개에서 새 화자 기준을 계산했습니다. 신경망 가중치는 공식 모델을 그대로 사용합니다.

| 등록에 넣지 않은 비교 자료 | 기존 본인 허용 | 새 본인 허용 |
|---|---:|---:|
| genuine-dev 새 등록 세션 10개 | 9/10 | 10/10 |
| 과거 STT 개발 가져오기 12개 | 8/12 | 12/12 |
| 과거 VAD/KWS 시험의 가져오기 12개 | 2/12 | 12/12 |
| 합계 | **19/34** | **34/34** |

타인 개발 음성 10개는 기존과 새 모델 모두 **0/10 허용**입니다.
모델 선택에 사용한 개발 비교이며 독립 검증 정확도는 아닙니다. 사용자는 앞서 추가 녹음
검증 생략을 선택했으므로 calibrated=false / development override를 유지합니다.
이번에 녹음을 추가 요청하거나 실제 로봇 명령을 발행하지 않았습니다.

최근 실시간 로그 `python3_22500_1794842665491.log`에는 speaker_rejected가 실제로 있으며,
robot_error에 의해 speaker_not_checked가 된 이전 로그와 구분됩니다. 다만 이 실시간 실행은
recording=disabled라 실패 음성을 재생할 수 없습니다. 어떤 줄이 본인/타인인지도 로그만으로
확정하지 않습니다. 따라서 방금 실시간 실패 사례 전체가 해결됐다고 주장하지 않습니다.

## 입력과 분리

- 등록: `speaker_enrollment_wmp_9_m4` 20개. 기존 사용자 기준을 새 모델로 재계산.
- 개발 본인: `speaker_genuine-dev_22sezaa1` 10개. 등록에 추가하지 않음.
- 동의한 타인: `speaker_other-dev_1yv_ksp0` 10개, manifest consent=true 확인.
- 과거 본인: `tools/voice_eval/speaker_checks_20261117.json`에 명시한 24개.
  기존 `stt_dev_20261111.json`의 fetch 12개와 사용자 시험 기록의 비정지 가져오기 12개.
- 총 64개, 모두 품질 검사 통과. 등록/개발/과거 파일 간 PCM SHA256 중복 없음.
- 정지, 인사, 무음 오전사/화자 불명 구간은 등록에 추가하지 않음.
  classifier held-out 데이터는 열거나 재평가하지 않음.
- 등록 20개는 성능 표의 34개 분모에서 제외.

## 후보 비교와 선택

동일 데이터에서 다음 후보를 개발 비교했습니다. 아래 표의 타인 허용은 모두 0/10을 맞춘
각 후보별 개발 임계값에서의 결과입니다. 서로 다른 모델의 점수 크기는 직접 비교하지 않습니다.

| 모델/등록 | 새 본인 10 | STT 과거 12 | 다른 세션 과거 12 |
|---|---:|---:|---:|
| Resemblyzer / 원래 20 | 9 | 8 | 2 |
| Resemblyzer / 원래 20 + 과거 8 | 9 | 9 | 2 |
| ECAPA 원본 PCM / 원래 20 | 10 | 12 | 10 |
| ECAPA 원본 PCM / 원래 20 + 과거 8 | 10 | 12 | 11 |
| ECAPA 무음 정리 / 원래 20 | 10 | 12 | 12 |
| ECAPA 무음 정리 / 원래 20 + 과거 8 | 10 | 12 | 12 |

과거 8개 추가 후보의 STT 과거 12 수치에는 등록에 추가한 8개가 포함되므로 평가 성능으로
해석하지 않습니다. 최종 선택은 원래 20개만 등록한 ECAPA 무음 정리입니다.
과거 8개를 추가하지 않아 최종 비교의 본인 34개는 모두 등록 밖의 자료입니다.
다만 후보 선택에는 사용됐으므로 최종 표 역시 개발 결과입니다.

선택 후 기준은 타인 개발 최고 점수 0.43471576083795826 바로 위 값에 고정 여유 0.01을 더한
**0.44471576083795833**입니다. 새 본인 개발 최저는 0.692178487013956,
과거 본인 최저는 0.458301515895349입니다. 최저 본인과 경계의 차이가 작아
새 장소/다른 화자/실시간 입력에서 일반화 성능은 아직 알 수 없습니다.
기준을 낮춰 기존 모델을 계속 사용하는 방식은 채택하지 않았습니다.

## 구현

- `speaker_ecapa.py`: 공식 Fbank(80 mel), sentence normalization, ECAPA 192차원 구조를
  명시적으로 생성. CPU 1스레드, 로컬 가중치 SHA256 확인 후 weights_only=True 로드.
- `speaker_backend.py`: 기존 품질 검사/RMS/클리핑/최소 발화 길이 0.6초 유지.
  같은 Resemblyzer 전처리로 음량 정규화/무음 정리 후 PCM16으로 양자화해 ECAPA 입력.
  등록과 런타임에 같은 경로를 사용.
- `speaker_profile.py`: 등록 모델 목록별 해시/차원/전처리 검증. 기존 Resemblyzer 파일도 읽음.
  192차원/256차원 기준을 혼용하거나 잘못된 전처리 기준을 쓰면 거부.
- `speaker_runtime.py`: 프로필에 지정된 모델을 로드하고 준비 로그에 backend/threshold 표시.
  STT 병렬 실행, 오류 시 fetch 차단, stop 예외, 로봇 IDLE 게이트는 유지.
- `rebuild_speaker_profile.py`: 기존 등록/본인/동의한 타인/과거 체크 파일만 읽고
  새 프로필과 비교 보고서를 생성. 기존 프로필 덮어쓰기 금지, 해시 중복 검사,
  비교 자료에서 본인 허용 감소 또는 타인 허용 증가가 있으면 저장하지 않음.
- `calibrate_speaker.py`: 프로필의 모델에 맞는 인코더를 선택하도록 확장.
- `speaker_development.yaml`: 새 owner_ecapa_development.json을 읽도록 변경.

모델: [SpeechBrain 공식 ECAPA 모델](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb),
revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`, SpeechBrain 1.0.3,
torch/torchaudio 2.6.0+cpu. 이전 CPU 비교에서 내려받은 로컬 모델을 사용했습니다.
모델 SHA256: `0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2`.
프로필 backend는 `speechbrain-ecapa-voxceleb-v1`, preprocessing은
`resemblyzer-trim-pcm16-v1`입니다. 런타임 다운로드/원격 YAML 실행은 없습니다.

## 검증

- 노트북 빌드 성공.
- 음성 패키지 **352 passed / 1 skipped**, colcon test-result 오류/실패 0.
- 평가 도구 **57 passed**, 변경 평가 도구 flake8 통과.
- 실제 `/usr/bin/python3` + voice_env에서 새 모델/프로필 준비 완료, backend와 기준값 확인.
- 실제 Whisper + 새 SpeakerRuntime에 기존 음성을 넣어 본인 4개 허용, 타인 2개 거부,
  스톱/멈춰 2개 stop_bypass를 확인. 로봇 상태/LLM/발행은 각각 가짜 IDLE/가짜 fetch 응답/
  로컬 콜백으로 대체했으며 마이크나 ROS 노드를 시작하지 않음.
- 위 통합 재생의 화자 계산은 0.187–0.533초, STT는 약 1.67–1.96초.
  처리 완료 시간은 로그 정밀도에서 STT 시간과 0–1ms 차이였습니다.
  별도 ECAPA 추론 64개 중앙값은 약 159ms(전처리 시간 제외)입니다.
  실제 실시간 종단 지연이나 CPU 경합 전체를 검증한 수치는 아닙니다.
- 모델 누락/프로필 불일치/인증 거부 시 fetch 차단 및 누구의 stop이든 우회하는 구조 유지.

## 로컬 산출물과 재현

프로필/녹음/임베딩 원본은 Git 제외이며 로컬 파일 권한 0600입니다.
- 기존 `owner_development.json` SHA256:
  `2be0e4690c049cc2f8391b4eb995900c7cb068c4c6850400aaff0cd22ee47439` (변경 없음).
- 새 `owner_ecapa_development.json` SHA256:
  `1a6048e7036b335493c20569eabf9fb4d5887de39bcdd4d2ebac8bc9957363a3`.
- `log/speaker_adapt_20261117/final_report.json`: 44개 개별 점수/판정 및 기준 파일 해시.
- 같은 폴더의 `embeddings.json`, `ecapa_embeddings.json`, `explore.log`,
  `ecapa_compare.log`: 후보 비교 원본. `pipeline_report.json`: 통합 재생 기록.

기존 파일을 덮어쓰지 않도록 새 출력 경로를 지정해 재현합니다.

```bash
cd ~/2026project
source voice_env.sh
/usr/bin/python3 tools/voice_eval/rebuild_speaker_profile.py \
  --baseline speaker_profiles/owner_development.json \
  --enrollment recordings/speaker_enrollment_wmp_9_m4/manifest.json \
  --genuine recordings/speaker_genuine-dev_22sezaa1/manifest.json \
  --other recordings/speaker_other-dev_1yv_ksp0/manifest.json \
  --checks tools/voice_eval/speaker_checks_20261117.json \
  --output speaker_profiles/owner_ecapa_repeat.json \
  --report log/speaker_adapt_20261117/repeat_report.json
```

## 로봇에서 확인할 항목

새 녹음을 필수 조건으로 요구하지 않습니다. 다음 실제 사용 때 기존 음성 노드를 종료하고
README의 두 params-file 실행 명령으로 다시 켜면 새 프로필을 읽습니다.
Jetson은 기존 eated/ROS_DOMAIN_ID=37을 유지하고 복사/빌드할 항목이 없습니다.

- [ ] 노트북 `[SPEAKER READY]`에 backend=speechbrain-ecapa-voxceleb-v1 표시.
- [ ] IDLE에서 본인 가져오기 요청은 speaker_verified 후 발행, 타인 요청은 speaker_rejected.
- [ ] stop은 stop_bypass로 처리. robot_error/busy이면 화자 결과와 별개로 fetch 차단.

오류 분석용 저장이 필요할 때만 `save_recordings:=true`를 명시합니다.
이번 변경에서 자동 녹음을 켜거나 이미 실행 중인 노드를 재시작하지 않았습니다.
