# 화자 인증 1단계: CPU 모델 선택·등록 도구·fetch 인증 연결

## 범위와 현재 결론

사용자가 종단 지연 추가 시험을 종료하고 화자 인증 단계로 전환하도록 요청했습니다.
기존 지연 결과는 유지했습니다. 이번 변경은 노트북 전용이며 Jetson/공유 인터페이스 변경,
실제 로봇 명령 발행, 마이크 녹음, 기존 분류기 held-out 재실행은 하지 않았습니다.

Resemblyzer를 우선 선택했습니다. 등록·기준값 보정·독립 검증·런타임 연결은 구현했지만,
**실제 사용자 등록 및 본인/동의한 타인 데이터 평가가 남았습니다.**
따라서 기본 `speaker.mode: "off"`를 유지합니다. 현재 동작을 인증 활성 상태로 오해하면 안 됩니다.
과거 개발 WAV는 CPU 시간 측정에만 사용했고 운영 사용자 프로필로 등록하지 않았습니다.

## CPU 실측

환경: 이 개발 노트북, Python 3.10, CPU 전용 torch 2.6.0+cpu, NumPy 2.2.6,
Resemblyzer 0.1.4, librosa 0.11.0, SpeechBrain 1.0.3, torchaudio 2.6.0+cpu.
임베딩 추론은 torch CPU 1스레드, 모델 워밍업 이후 측정했습니다.
기존 `recordings/stt_dev_20261111_171138`의 이름순 첫 12개 개발 WAV를 사용했습니다.

| 모델 | 발화 처리 중앙값 | 범위 | 모델 파일 크기 |
|---|---:|---:|---:|
| Resemblyzer | 31.3ms | 22.1–42.8ms | 17,090,379 bytes |
| SpeechBrain ECAPA | 174.4ms | 151.9–245.5ms | 83,316,686 bytes |

Resemblyzer는 음량 정규화/무음 제거와 품질 검사를 포함하며, ECAPA는 공식 Fbank/
문장 정규화 설정으로 전체 PCM을 사용합니다. **동일 정확도의 모델 비교가 아닌 각 처리 경로의
CPU 비용 비교**입니다. 12개 모두 Resemblyzer 품질 검사를 통과했지만 화자 정확도는 평가하지 않았습니다.
모델 로드와 워밍업 기록은 각각 14.421초/0.651초였으나, 한 프로세스에서 순서대로 실행해
공유 라이브러리 캐시와 최초 JIT 비용이 달라 공정한 콜드 시작 비교로 사용하지 않습니다.

기존 개발 녹음 첫 4개에 대해 Whisper 단독과 Resemblyzer 병렬 처리를 번갈아 측정했습니다.
4/4에서 STT 종료 전에 화자 계산이 완료됐고 전사문은 같았습니다.
단독→병렬 총시간은 1.635→1.736, 1.759→1.725, 1.791→1.779, 1.714→1.665초였습니다.
추가 인증 대기는 없었지만 CPU 경합과 변동이 있어 ‘지연 0’으로 주장하지 않습니다.

저장소 도구 `benchmark_speaker.py`로도 4개 입력을 재실행했습니다.
임베딩 중앙값 Resemblyzer 24.6ms / ECAPA 166.0ms, STT 종료 전 화자 계산 완료 4/4,
전사문 동일 4/4였습니다. 단독→병렬 총시간은 1.583→1.728, 1.691→1.675,
1.733→1.782, 1.718→1.697초였습니다. 이 소규모 시험은 운영 지연 분포 검증이 아닙니다.

로컬 원본(커밋 제외):
- `log/speaker_auth_20261112/benchmark.json`: 12개 파일명·SHA256·시간.
- `log/speaker_auth_20261112/stt_parallel.json`: 첫 병렬 비교.
- `log/speaker_auth_20261112/tool_smoke.json`: 저장소 도구 재실행 결과.
- 같은 폴더의 설치 로그, 설치 전 패키지 목록, build.txt, tests.txt.

재현 예시(파일 목록은 명시적으로 개발 녹음만 선택):

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/benchmark_speaker.py \
  recordings/stt_dev_20261111_171138/vad_command_20261111_171151_001.wav \
  recordings/stt_dev_20261111_171138/vad_command_20261111_171200_002.wav \
  --with-stt --output log/speaker_benchmark_new.json
```

ECAPA를 다시 비교하려면 공식 가중치를 별도로 준비하고 `--ecapa-model`을 추가합니다.
런타임 운영에는 ECAPA 의존성이나 모델이 필요하지 않습니다.

출처와 고정 모델:
- [Resemblyzer 공식 저장소](https://github.com/resemble-ai/Resemblyzer)
  - 패키지 0.1.4의 `pretrained.pt`
  - SHA256 `39373b86598fa3da9fcddee6142382efe09777e8d37dc9c0561f41f0070f134e`
- [SpeechBrain ECAPA 공식 모델](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb)
  - revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`
  - `embedding_model.ckpt` SHA256
    `0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2`
  - 공식 hyperparams 구조를 로컬에서 명시적으로 생성하며 원격 YAML 실행은 하지 않음.

## 구현과 실패 처리

- `speaker_backend.py`: CPU 전용 임베딩, 모델 버전/해시 확인, 15초 상한,
  무음/클리핑/유효 음성 0.6초 미만 거부. 등록과 운영에 동일 전처리 적용.
- `speaker_profile.py`: 256차원 유한 벡터, 정규화 평균/코사인 점수, 등록 20개 이상,
  모델/전처리 일치와 분리된 개발·검증 근거 확인.
- `speaker_runtime.py`: 비동기 준비 및 STT 병렬 작업, 미완료 추론 최대 한 건,
  준비 실패/혼잡/시간 초과 시 가져오기 거부, 종료 시 추론 완료를 기다리지 않음.
- `pipeline.py`: 전사문 정지이면 인증 대기 생략. 비정지 요청은 인증 후 LLM으로 전달.
  승인 결과는 발화 한 건에만 적용하며 IDLE/명령 신선도/stop 무효화 검사를 유지.
  오디오 정지 처리 경로에는 인증 게이트를 추가하지 않음.
- `record_speaker.py`: Enter와 화면 안내로 20개 등록 문장/10개 평가 문장 녹음,
  장치 초기 1초 버림 후 6초 녹음, 품질 검사·사용자 재녹음·기존 프로필 보호.
- `calibrate_speaker.py`: 등록/개발/독립검증 간 PCM 해시 중복 거부, 동의한 타인만 평가.
  개발에서 FAR 0%·FRR ≤10%를 만족하는 임계값을 고정하고 새로운 검증에서 같은 기준 확인.
  품질 실패 본인 검증 음성도 FRR 분모·분자에 포함. 실패하면 활성화 불가.
- 원본 녹음과 프로필은 로컬 보관·Git 제외, 파일 권한 0600.

`shadow`는 가져오기를 차단하지 않는 관찰 모드입니다. `enforce`만 인증 제한을 적용합니다.
프로필 오류 시 기존 경로로 조용히 우회하지 않으며 정지 처리는 그대로 유지합니다.
운영 데이터가 없는 상태에서 등록 임계값 0.75를 확정값으로 사용하지 않습니다.

## 자동 검증

- `colcon build --symlink-install --packages-up-to voice_command_pkg`: 성공.
- `colcon test --packages-select voice_command_pkg`: **344 passed, 1 skipped**.
  기존 copyright skip 유지. flake8/pep257 통과.
- `colcon test-result --verbose`: 345 tests, 0 errors, 0 failures, 1 skipped.
- `tools/voice_eval/tests`: **57 passed**.
- 추가 도구 flake8 확인.
- 인증 거부/오류/미준비/시간 초과에서 LLM 호출·fetch 발행 차단,
  미완료 인증 중 스톱·멈춰·정지·수정 문장의 정지 처리, 이전 발화 승인 재사용 방지,
  프로필 없음/검증 전 강제모드 거부, 작업 수 제한·추론 중 종료를 자동 시험.
- 실제 마이크 대신 가짜 PCM/프로세스로 등록 안내→20개 저장→프로필 생성,
  덮어쓰기 방지 및 개발/검증 데이터 재사용 방지를 시험.
- 실제 모델 임베딩과 Whisper 병렬 계산은 위 개발 WAV로 실행.
- 실제 음성 화자 FAR/FRR 및 현장 인증 동작은 **미검증**.

## 다음 작업 및 로봇에서 확인할 항목

현재는 노트북 등록만 진행합니다. Jetson에서는 실행하거나 복사·빌드할 항목이 없습니다.
기존 음성 노드를 Ctrl+C로 종료한 뒤:

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/record_speaker.py
```

- [ ] 평소 말투로 안내되는 20문장 녹음. “지금 말씀하세요”가 뜬 뒤 말하기.
- [ ] 완료 시 `speaker_profiles/owner.json`과 manifest 경로 출력.
- [ ] 등록 프로필에 `calibrated: false`, 등록 수 20 확인.
- [ ] 이후 새 본인/동의한 타인 개발·검증 녹음과 실제 VAD 발화 점수 확인.
- [ ] 검증 통과 후 별도 안내에 따라 발행 없는 시험부터 진행.
- [ ] 마지막 Jetson 연동에서 본인 fetch 허용·타인 fetch 차단·누구의 stop이든 처리 확인.
  이 실물 단계의 명령/순서는 검증 데이터와 인증 모드를 확정한 뒤 제공.

과거 녹음의 자동 등록, 정지 약화, Jetson 수정, GitHub push는 이번 작업에 포함하지 않았습니다.


## 2026-11-16 실제 등록 및 development 보정 결과

사용자 등록 `speaker_enrollment_wmp_9_m4`의 20개 WAV는 각 6초/16 kHz/mono/PCM16이며,
등록 해시와 품질 검사를 통과했습니다. 실제 모델로 재계산한 임베딩 평균도 저장된
`owner.json`과 일치했습니다. 이는 등록 파일 일관성 확인이며 화자 구분 정확도 평가가 아닙니다.

본인 `speaker_genuine-dev_22sezaa1` 10개와 타인 `speaker_other-dev_1yv_ksp0` 10개로
기존 calibrate 도구를 실행했습니다. 타인 manifest의 consent=true 및 complete=true를 확인했고,
등록/본인 개발/타인 개발 녹음 간 PCM 중복과 변경을 검사했습니다. 전부 통과했습니다.
붙여넣은 터미널에는 동의 입력 앞에 글자가 보이지만 저장된 manifest의 동의 기록은 true입니다.

| 항목 | 결과 |
|---|---|
| 본인 허용 | 9/10 |
| 본인 거부율 FRR | 10% |
| 타인 허용 | 0/10 |
| 타인 수락률 FAR | 0% |
| 기준값 | 0.7646047983524492 |
| 본인 점수 범위 | 0.727413–0.892606 |
| 타인 점수 범위 | 0.603593–0.764605 |
| 독립 검증 완료 | 아니요 (`calibrated: false`) |

본인의 `01.wav`, “콜라 가져다 줘” 점수 0.727413이 기준보다 낮았습니다.
사용자가 이 문장을 한 번 재녹음했으며, 분석 대상은 최종 저장한 녹음입니다.
거부된 데이터를 삭제하거나 통과한 재녹음으로 대체해 성적을 수정하지 않았습니다.

기존 알고리즘은 관찰한 타인 최고 점수 바로 위의 표현 가능한 실수를 선택합니다.
따라서 개발 데이터에서 FAR 0%가 나온 것은 독립 정확도 증거가 아니며,
경계의 여유가 작고 본인 FRR도 허용 상한 10%입니다. 출력 반올림 값을 수동 입력하지 않습니다.
기준과 모델을 유지한 별도의 본인/동의한 타인 test 녹음이 필요합니다.

로컬 결과는 `speaker_profiles/owner_development.json`에 새로 저장했습니다.
SHA256: `2be0e4690c049cc2f8391b4eb995900c7cb068c4c6850400aaff0cd22ee47439`.
프로필과 원본 WAV는 Git에서 제외되는 것을 확인했습니다. 운영 설정은 off를 유지했고,
마이크 시작/ROS 발행/Jetson 변경/운영 임계값 변경은 하지 않았습니다.

다음 확인은 노트북에서 각각 새 10문장을 녹음하는 것입니다. 음성 노드가 실행 중이면
먼저 Ctrl+C로 종료합니다. Jetson에는 실행할 명령이 없습니다.

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/record_speaker.py --purpose genuine-test
PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/record_speaker.py --purpose other-test
```

첫 명령은 본인, 두 번째는 이번 개발 녹음을 제공했던 동의한 타인이 읽습니다.
화면 안내에 따라 각 10개를 저장하고 두 manifest 경로를 확보하면 녹음 단계 완료입니다.
이후 고정된 `owner_development.json`으로 독립 검증하며, 본인 허용 9/10 이상·타인 허용
0/10이 검증 기준입니다. 소수 표본 결과를 모든 사람·환경에서의 성능 보장으로 해석하지 않습니다.
