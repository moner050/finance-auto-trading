# v6 명세 ↔ 코드 대조 감사

**대상 명세:** `David_Trullas_Vila_전략분석_자동매매용_v6.0_화면역공학통합판.md`
**대상 코드:** `main` 기준선 (`306d292`)
**감사일:** 2026-08-25
**수정 커밋:** `b0d41b7` … `9a156fa`

명세의 각 규칙을 코드에서 찾아 대조했다. 표의 §는 명세의 절 번호다.

## 1. 감사 전에 확인된 결함

### 1.1 HLIT 피보나치가 구현되지 않았다 (§3)

명세의 핵심 알고리즘 — "이것이 내가 세계 대회를 이기는 방식이다" — 인 STEP 3~5가
코드 어디에도 없었다.

- 앵커 A = 두 극점 사이의 절대 최고/최저, 앵커 B = 두 번째 극점
- 25% / 50% / 66% 되돌림 레벨
- 목표 = 66% (다우 2/3)

`zones.py`의 `build_hlit_zones`는 이름과 달리 §3이 아니라 §10(과거 고·저·시·종가
밀도 클러스터링)을 구현한다. 둘은 다른 알고리즘이다.

더 나쁜 것은, `operations/david_v6_position.py`가 `fib_25_price`, `fib_50_price`,
`fib_66_price`를 **입력으로 요구**하면서 66% 도달 시 전량 청산까지 구현해 두었는데
**이 값을 생산하는 코드가 없었다.** 포지션 관리는 존재하지 않는 입력을 기다리고
있었다.

### 1.2 진입 전제조건이 강제되지 않았다 (§3 STEP 2, §4.1, §22.2)

`evaluate_v6`는 divergence·exhaustion·zones 사실이 `AVAILABLE` 상태인지만 보고
**내용을 검사하지 않았다.** 실증 결과:

```
divergence.regular : ()          ← 다이버전스 0개
exhaustion.bullish : None        ← 소진 0개
zones.zones        : ()          ← 존 0개
------------------------------------------------------------
grade            : A             ← 최고 등급
blockers         : ()            ← 차단 없음
calculated_qty   : 6.206
risk_fraction    : 0.0050        ← 최대 리스크
ORDER INTENT PRODUCED: ENTRY BUY
```

이는 명세가 가장 강하게 못박은 두 불변식을 정면으로 위반한다.

> "❌ 불성립 시: 피보나치를 그리지 않는다. 매매하지 않는다." (§3 STEP 2)
> "존 도달 ≠ 진입" (§4.1)

엔진의 기존 테스트 픽스처 자체가 빈 다이버전스·빈 소진으로 tradeable 결정을
단언하고 있었다. 즉 결함이 테스트로 고착되어 있었다.

### 1.3 소비만 되고 생산되지 않는 입력 (§9.4)

`blocking_big_trade_ahead`에 해당하는 `blocking_big_trade` 플래그도 같은 문제였다.
`david_v6_position.py`가 이를 받아 전량 청산하지만, 계산하는 코드가 없고 테스트는
언제나 `False`를 넘겼다. 명세는 이를 **불변식**으로 규정한다.

> "never: Big Trade와 맞서 진입하지 않는다" (§9.4)

### 1.4 스프레드 가드 부재 (§7.1)

`spread_guard: {max_spread_ticks: 3}`이 구현되지 않았다. `risk/v6.py`는 스프레드를
손절 버퍼 계산에만 쓰고 상한을 검사하지 않았다.

## 2. 수정 내용

| 항목 | 조치 |
| --- | --- |
| §3 HLIT 앵커·레벨 | `strategies/david_v6/hlit.py` 신설. 정규 다이버전스에서만 작도하고 66%를 목표로 반환 |
| §3 STEP 2 | `REGULAR_DIVERGENCE_ABSENT` — 거래 방향의 정규 다이버전스가 없으면 차단 |
| §10 | `NO_MARKED_ZONE` — 사전 표시된 존이 없으면 차단 |
| §4.1 | `EXHAUSTION_ABSENT` / `EXHAUSTION_UNCONFIRMED` / `EXHAUSTION_DIRECTION_MISMATCH` |
| §9.4 | `blocking_big_trade_ahead()` 구현 + `BLOCKING_BIG_TRADE_AHEAD` 진입 차단 |
| §7.1 | `SPREAD_ABOVE_THREE_TICKS` — 3틱 초과 스프레드 차단 |

히든 다이버전스는 §12 `precondition: regular_divergence_required`에 따라 작도
자체를 하지 않는다. Método 계열은 HLIT 게이트 적용 대상이 아니다.

## 3. 명세를 충실히 따르고 있던 부분

대조 결과 아래는 정확했다. 이 부분들은 손대지 않았다.

| 모듈 | 명세 | 판정 |
| --- | --- | --- |
| `metodo.py` | §2.2 SMA 6/70/200, §2.3 MACD 12/26/9 | 정확. 추세 정의(200 양기울기 ∧ 70>200 ∧ 70 양기울기), 6/70 교차, MACD 0선 위 교차, 하락 대칭까지 일치. EMA·시그널 시드 계산도 표준 |
| `pivots.py` | §3 STEP 1~2 | 정규/히든 강세/약세 4종 판정 정확 |
| `exhaustion.py` | §4.1 | 연속 극점 갱신 + 계단식 거래량 감소, 3개 이상에서 확정 |
| `risk/v6.py` | §12 `risk`, `stop` | per_trade 0.0015 / A등급 0.0025, 일간 0.0075, 주간 0.0200, 연속손실 2회, 구조적 손절 ATR 0.40~1.50, 퍼센트 손절 금지 |
| `david_v6_position.py` | §9.1~9.3, §21.6 | 유리한 이동 후에만 추가, 가중평균 BE, 손절 확대 금지, 추가 후 총위험 증가 금지, fib25 기록만·fib50 연구만·fib66 청산 — §21.6 권한표와 정확히 일치 |
| `calendar.py` | §8 | 2·3성 차단, 서프라이즈 없음 10분, 강한 지표 120분, NFP 세션 전체 |
| `costs.py` | §14.2 | BE 오프셋을 왕복 수수료 + 슬리피지 + 틱으로 재계산 (NQ 1.5pt 하드코딩 아님) |
| `order_flow.py` | §22.5 | 동일 방향·150ms·근접가 묶음, 명목금액 백분위 정규화 |
| `regime.py` | §2.3 | 비관 극단을 put/call·변동성·breadth 분위수로 판정 |
| `universe.py` | §2.1 | 섹터 4위부터 폐기, 부동산·금융·에너지 제외, 시점 정확 편입 판정 |

특히 §14.2 "NQ 고유값 정규화"가 잘 지켜져 있다. `+30pt` 추가 규칙은
`max(진입가 0.10%, 0.35 × ATR(5m))`로, BE `+1.5pt`는 실제 비용 기반으로 재정의되어
KRX·Binance에 그대로 이식 가능하다. 이는 §14.3 `korea_v5.normalization`과 일치한다.

## 4. 2차 수정 — 남아 있던 규칙

1차 수정 이후 남겨두었던 항목을 모두 구현했다.

### 4.1 §21.3 가중 점수제

컷오프 7·9는 이미 코드에 있었으나 **가중치가 없었다.** 모든 지표가 1점으로 계산되어
`technical-00`~`technical-08` 같은 무의미한 키 9개로도 A등급이 나왔고, 정규
다이버전스가 히든과 같은 권한을 가졌다.

명세가 §21.3에 점수표와 컷오프를 모두 주고 있었다. 지표 어휘를 정의하고 가중치를
적용했다. §18.3 규율에 따라 역공학 항목은 이름에 출처를 넣었다 (`v1_secado`,
`v1_mig_reversal`). Cero osmótico는 §21.3이 +1을 주지만 §18.2가 telemetry 권한으로
묶어두므로 가중치 0으로 두고, walk-forward 승격 전까지 결정을 움직이지 못하게 했다.

§9.4는 방해 Big Trade를 -2로, §21.3은 -4로 적는다. 점수표와 컷오프가 짝을 이루는
§21.3을 따랐다. 다만 §9.4의 `never`와 §18.2의 거부권에 따라 진입 자체가 차단되므로
실제로는 점수보다 거부권이 먼저 작동한다.

### 4.2 나머지

| 명세 | 조치 |
| --- | --- |
| §7.2 월요일 감점 -1 | `monday_score_penalty`를 계산만 하고 아무도 쓰지 않았다. 등급 점수에 반영 |
| §2.3 signal_c | MACD 0선 아래 교차 + 비관 극단 조건부 진입. `regime.pessimism_extreme`이 metodo와 연결되어 있지 않아 아예 발화 불가능했다 |
| §12 `exit: cross_down(sma6, sma70)` | `sma_6_70_cross_down`을 계산만 하고 버렸다. Método 스윙에 청산 경로가 없었다. `EXIT_FULL_METODO_CROSS_DOWN` 추가 |
| §7.1 개장 15분 0.5배 | 세션이 `entry_allowed` 불리언만 냈다. `size_multiplier` 추가, 리스크 엔진이 스텝 내림 전에 적용 |
| §7.1 프리마켓 마이크로 3계약 | `pre_open`, `max_micro_contracts` 사실로 노출. 계약 수→수량 변환은 §14.2 시장별 정규화이므로 호출자 몫 |
| §6 거래 상한 8회 | `SESSION_TRADE_UPPER_BOUND`. 명세가 "David 공식값 아님"으로 표시한 안전 상한이라 상수명에 반영 |
| §14.3 KRX VI·단일가 | `TossHlitKrxMarketSafetyEvidence`가 두 사실을 정확히 담고 있었으나 아무도 읽지 않았다. 브로커 중립 타입으로 세션에 연결하고, KRX 세션에 안전 증거가 없으면 fail-closed |
| §2.1 국가 강도 | 유니버스에 없었다. `COUNTRY_NOT_STRONG` 추가 |
| §9.3 gift_points | "정확한 가격과 싸우지 않는다". 모든 전량 청산은 MARKET으로 나간다 |
| §9.3 exit_on_objective | `SESSION_OBJECTIVE_REACHED`. 목표 달성 후 신규 진입 차단 |

### 4.3 구현하지 않은 것

- **Cyborg 계층** — §12가 `enabled: false`로 지정
- **§7.1 `arrival_before_open_minutes: 75`, `active_productive_work_minutes: 60`** —
  사람의 작업 습관이지 자동매매 규칙이 아니다
- **§7.1 프리마켓 볼륨 비교** — "프리마켓 안의 두 구간끼리 비교"라는 방법만 있고
  판정 기준이 없다

### 4.4 반복된 패턴

이번 감사에서 같은 결함이 다섯 번 나왔다. **한쪽만 있고 연결되지 않은 코드**다.

- fib 25/50/66: 소비자만 있고 생산자가 없었다
- `blocking_big_trade`: 소비자만 있고 생산자가 없었다 (테스트는 항상 `False`)
- `monday_score_penalty`: 생산자만 있고 소비자가 없었다
- `sma_6_70_cross_down`: 생산자만 있고 소비자가 없었다
- `TossHlitKrxMarketSafetyEvidence`: 생산자만 있고 소비자가 없었다

각 조각은 테스트를 통과했다. 이어지지 않았을 뿐이다. 매매 루프를 먼저 만들기로 한
이유가 여기 있다 — 끝에서 끝까지 도는 경로가 없으면 이런 단절이 드러나지 않는다.

## 5. 검증

수정 후 상태다.

```
1471 passed, 17 skipped
ruff format --check : 372 files already formatted
ruff check          : All checks passed
pyright             : 0 errors
```

기준선 1400건에서 71건이 늘었다. HLIT 작도 12건, 진입 전제조건 9건, 가중 점수제
15건, Método signal_c·청산 6건, 세션 사이즈·프리마켓 7건, KRX 안전 5건, 거래 상한
3건, 익절 규율 4건 등이다.

Docker는 기동하지 않았다. DB·브로커·컨테이너가 필요한 검증은 여전히 미수행이다.
