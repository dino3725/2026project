# 2026-11-18 실제 연속 녹음의 로컬 active 정지 검증

## 목적과 범위

사용자가 다음 단계 진행을 요청해 `stop_diagnostic.8bAOVn`의 연속 녹음을
실제 시간 간격으로 재생했습니다. 앞선 shadow 시험의 9회 감지가 active 처리에서는
각 발화에 정확히 한 번씩 대응하는지 확인합니다. 총횟수만 같고 다른 발화를 놓치는
경우를 통과시키지 않도록 발화별 오디오 구간과 키워드도 대조합니다.

원본은 118.86초이며 마지막 일반 발화 종료 이후 생활 소음 구간 37.59초를 포함합니다.
WAV SHA256: `9c6cb57832281d7775e116817ab91eb6c75a57fadc391cca7e400de0c0e446bc`.

실제 VAD, Whisper small, ECAPA, FP32 KWS, 위치 보완과 gain=2.0을 사용합니다.
`--post-warmup`으로 저장된 입력 앞부분을 다시 버리지 않았습니다. ROS 노드/발행자,
마이크, Jetson은 실행하지 않으며 발행 함수를 성공을 반환하는 로컬 콜백으로 바꿉니다.
따라서 로그의 `published=True`는 **로컬 콜백 성공이며 로봇 수신이 아닙니다.**
LLM은 비활성화했고 일반 요청은 offline_no_llm으로 버립니다. 실제 fetch 허용,
화자 인증 정확도, 로봇 상태 게이트, cold warmup 부하를 검증하는 시험이 아닙니다.

## 첫 재생에서 발견한 종료 문제와 수정

첫 재생은 정지 대응 검사를 통과하고 JSON을 저장했지만, 그 직후
`terminate called without an active exception`으로 종료 코드 134가 발생했습니다.
따라서 첫 실행 전체를 정상 통과로 집계하지 않습니다. first_replay.json/log와
first_verification.json을 로컬에 별도로 남겼습니다.

검토 결과 화자 런타임의 close는 취소 신호만 전달하며, 재생 도구가 모델 스레드의
종료를 기다리지 않은 채 Python 인터프리터를 끝낼 수 있었습니다. 네이티브 오류의
정확한 발생 지점을 스택으로 확정한 것은 아니지만 이 종료 경합 가능성을 제거했습니다.
로컬 재생 도구만 모든 관련 스레드의 종료를 공유 10초 예산 안에서 기다리고,
`shutdown_workers_alive`를 기록합니다. 남은 스레드가 있으면 성공 종료하지 않습니다.
운영 음성노드의 stop 처리/취소 경로에는 대기를 추가하지 않았습니다.

도구 테스트 62개와 변경 도구/신규 테스트의 flake8 검사를 통과했습니다.
실제 대기 중인 스레드의 잔존 검출과 종료 확인, 비활성 모델/중복 스레드 처리를 검사했습니다.

## 수정 후 전체 재생 결과

수정한 도구로 같은 118.86초를 다시 실행했고 **프로그램 종료 코드 0**을 확인했습니다.
발화 구간별 검증 스크립트도 통과했습니다.

| 항목 | 결과 |
|---|---|
| 스톱 / 멈춰 / 정지 | 각각 3/3, 발화당 로컬 콜백 정확히 1회 |
| 정지 콜백 발생 경로 | 연속 7회 + 보완 재검사 2회 |
| 보완으로 처리된 발화 | 세 번째 스톱, 세 번째 정지 |
| 같은 발화의 후속 오디오 감지 | 7건 중복 억제 |
| 같은 발화의 후속 전사문 정지 | 9건 중복 억제, stop_bypass 9건 유지 |
| 일반 문장 3개 / 이후 생활 소음 37.59초 | 추가 콜백 0건 |
| VAD 시작/종료 | 실시간 원본과 24개 모두 일치 |
| 입력 생산 오류 / KWS 프레임 폐기 | 0 / 0 |
| 종료 때 남은 스레드 | 없음, shutdown_workers_alive=[] |

일반 발화 재검사 3건은 예산 만료로 중단됐습니다. 모델 오류/큐 초과는 없었습니다.
연속 감지 콜백의 프레임 읽기→결정은 12~18ms, 보완 콜백 2건의 VAD 종료→결정은
109/190ms였습니다. 실시간 마이크 읽기의 묶음 전달, 실제 LLM 부하와 로봇 통신을
재현하지 않으므로 이 값을 실제 말끝→로봇 정지 지연으로 쓰지 않습니다.

이번 확인으로 같은 녹음의 shadow 감지뿐 아니라 로컬 active 처리의 중복 억제까지
검증했습니다. 실제 ROS 송신 실패/수신, Jetson 제어기/물리 정지는 이 시험 범위 밖입니다.
노트북/Jetson 운영 설정과 공유 인터페이스는 변경하지 않고 기본 KWS off를 유지했습니다.

## 재현 명령

노트북에서 실행하며 약 2분 이상 걸립니다. Jetson에서 실행할 명령은 없습니다.
이 파일 재생은 마이크를 열거나 실제 명령을 발행하지 않습니다.

```bash
cd ~/2026project
PYTHONPATH=src/voice_command_pkg .venv/bin/python \
  tools/voice_eval/replay_audio_stop_pipeline.py \
  --wav recordings/stop_diagnostic.8bAOVn/continuous_l04t3vlf/continuous.wav \
  --post-warmup --mode active \
  --speaker-profile speaker_profiles/owner_ecapa_development.json \
  --allow-development-profile --recheck-phase-shifts --recheck-gain 2.0 \
  --output log/active_stop_repeat.json
```

정상 기준은 local callbacks=9, 정지 각 발화에 콜백 1회, 일반 발화/생활 소음에
추가 콜백 없음, dropped_frames=0, producer_errors=[], shutdown_workers_alive=[] 및
프로그램 종료 코드 0입니다. 파일 EOF는 도구가
예상하는 녹음 종료 오류이며 입력 장애 처리를 이용해 재생을 종료합니다.

## 판정 방법과 근거

원본 실시간 로그의 VAD 종료 시각과 각 발화 WAV 길이로 12개의 발화 구간을 계산합니다.
첫 9개는 스톱/멈춰/정지 각 3회, 나머지 3개는 일반 문장입니다. 콜백 오디오 위치를
해당 구간에 대응하고 정지 구간별 콜백 수와 키워드, 구간 밖 추가 콜백을 검사합니다.
원본/재생의 VAD 이벤트 일치, 프레임 폐기/입력 생산 오류와 모델 오류도 검사합니다.

로컬 근거는 `log/active_stop_live_20261118/`의 replay.json, replay.log,
verify.py, verification.json입니다. WAV/프로필/원시 결과는 Git 제외입니다.
같은 개발 녹음의 재생으로, 새로운 독립 녹음의 정확도나 실물 안전 인증을 뜻하지 않습니다.
