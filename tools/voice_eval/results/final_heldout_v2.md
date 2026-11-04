# Classifier v2 final held-out evaluation

- Classifier SHA256 before: `ef1f7db88a45c4b48040e3d9039d792b3a9cf29ef7e47d7004b647d70576bd4d`
- Classifier SHA256 after: `ef1f7db88a45c4b48040e3d9039d792b3a9cf29ef7e47d7004b647d70576bd4d`
- This dataset was executed exactly once and is no longer held-out data.

## Base (120)

| Class | Result |
| --- | ---: |
| coke | 28/30 (93.3%) |
| tissue | 29/30 (96.7%) |
| snack | 28/30 (93.3%) |
| unknown | 28/30 (93.3%) |
| **Overall** | **113/120 (94.2%)** |

## Hard (40)

| Class | Result |
| --- | ---: |
| coke | 7/10 (70.0%) |
| tissue | 9/10 (90.0%) |
| snack | 9/10 (90.0%) |
| unknown | 10/10 (100.0%) |
| **Overall** | **35/40 (87.5%)** |

## Combined (160)

- Overall: **148/160 (92.5%)**
- coke: 35/40 (87.5%)
- tissue: 38/40 (95.0%)
- snack: 37/40 (92.5%)
- unknown: 38/40 (95.0%)
- Confusion: `coke -> {coke: 35, unknown: 5}`
- Confusion: `tissue -> {tissue: 38, unknown: 2}`
- Confusion: `snack -> {snack: 37, unknown: 3}`
- Confusion: `unknown -> {unknown: 38, coke: 2}`

## Latency

- First/cold request: 52.751 seconds
- Warm mean: 2.898 seconds
- Warm min: 2.463 seconds
- Warm max: 3.511 seconds

## Failures

| Suite | Type | Input | Expected | Actual |
| --- | --- | --- | --- | --- |
| base | indirect_request | 내 목을 시원하게 해 줄 무언가를 챙겨 줄래 | coke | unknown |
| base | object_omission | 한 모금 넘기면 좀 살 것 같은데 | coke | unknown |
| base | indirect_need | 젖은 선반을 훔칠 만한 것이 필요하다 | tissue | unknown |
| base | indirect_state | 공복이 길어지니까 힘이 쭉 빠진다 | snack | unknown |
| base | indirect_state | 빈속으로 계속 있으니 정신이 없네 | snack | unknown |
| base | multi_intent | 마실 음료랑 먹을 간편식을 함께 준비해 | unknown | coke |
| base | multi_intent | 허기도 채우고 목도 적실 준비를 해 줘 | unknown | coke |
| hard | object_omission | 혀가 입천장에 붙겠어 어떻게 해 봐 | coke | unknown |
| hard | colloquial | 캬 시원한 한 모금 땡기는 타이밍이네 | coke | unknown |
| hard | stt_typo | 갈쯩 나니까 음뇨 좀 가저와 | coke | unknown |
| hard | stt_typo | 손에 소스 무더서 다글 게 필요해 | tissue | unknown |
| hard | colloquial | 배에서 밥 내놓으래 한입만 콜 | snack | unknown |
