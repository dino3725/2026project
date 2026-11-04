# 매니퓰레이터 오차 기준선 — YYYY-MM-DD

- 측정자:
- 커밋: `git rev-parse --short HEAD` =
- 측정 면: 바닥 / 책상 (표면 z = −0.010 m 사용 여부: )
- 격자: `print/grid_a4_x4.pdf`, 100 mm 기준선 실측 가로 ___ / 세로 ___ mm, 실제 바퀴 간격 ___ mm (URDF 288)
- 카메라 장착값: x −0.065, y 0.0195, z 0.052, roll/pitch/yaw 0 (보정 전)
- 사진: 격자 전체, 로봇 위치 테이프, 물체 놓는 방향

## A. 그리퍼와 물체 폭

### A-1. 그리퍼

| 항목 | 값 | 비고 |
|---|---|---|
| 열림 그리퍼 값 q (`gripper_left_joint`) | | 0.019 예상 |
| 손끝 안쪽 폭 W_open (mm) | | 끝에서 5 mm |
| 뿌리 안쪽 폭 (mm) | | 손끝과 다르면 평행 아님 |
| 손목 축 → 손끝 중앙 길이 (mm) | | URDF 126 |
| 잡힘 판정 최소 폭 = W_open − 54 (mm) | | `grasp_position_threshold` −0.008 |

### A-2. 물체 폭과 여유

| 물체 | 가로 (mm) | 세로 (mm) | 높이 (mm) | 닫히는 방향 폭 w (mm) | 여유 W−w (mm) | 한쪽 여유 s (mm) | 잡힘 판정 최소 폭보다 큼? |
|---|---|---|---|---|---|---|---|
| airpod | | | | | | | |
| coke | | | | | | | |
| tissue | | | | | | | |
| vaseline | | | | | | | |

### A-3. 잡았을 때 그리퍼 값 (Pick 시험 때 채움)

| 물체 | 잡힘 position q | 계산 폭 = W_open − 2(0.019 − q)·1000 | 실측 w | 차이 |
|---|---|---|---|---|
| | | | | |

## B. 비전 좌표 vs 실제 위치

- 원본: `results/raw/vision_YYYY-MM-DD.csv`
- 시험 수: 물체 __ × 표식 __ × __회

(여기에 `summarize_results.py --vision ...` 출력 붙여넣기)

관찰:
-

## C. 팔 목표 vs 실제 도착 위치

- 원본: `results/raw/arm_YYYY-MM-DD.csv`
- 계획 실패한 목표:

(여기에 `summarize_results.py --arm ...` 출력 붙여넣기)

관찰:
-

## D. 오차 예산 (물체별)

| 물체 | 한쪽 여유 s (mm) | 비전 수평 RMS (mm) | 팔 실측−목표 수평 RMS (mm) | 합 (mm) | 합 < s ? |
|---|---|---|---|---|---|
| airpod | | | | | |
| coke | | | | | |
| tissue | | | | | |
| vaseline | | | | | |

## E. 결론과 다음 단계

- 가장 큰 오차 원인:
- 2단계(카메라 장착 보정)에서 확인할 것:
- 3단계(팔 정확도)에서 확인할 것:
- `docs/PROJECT_CONTEXT.md` 7장 "비전 위치 오차" 기준선 값: ___ mm
