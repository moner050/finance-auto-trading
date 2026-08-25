# v6 명세 ↔ 코드 대조 감사

**대상 명세:** `David_Trullas_Vila_전략분석_자동매매용_v6.0_화면역공학통합판.md`
**대상 코드:** `main` 기준선 (`306d292`)
**감사일:** 2026-08-25
**수정 커밋:** `b0d41b7`

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

## 4. 남은 미구현 항목

아래는 이번 수정 범위에 넣지 않았다. 이유를 함께 적는다.

| 항목 | 명세 | 미구현 사유 |
| --- | --- | --- |
| 가중 점수제 | §4.4 regular +2 / hidden +1, §9.4 차단 -2 / 지지 +1 | 명세가 **가중치는 주지만 A·A후보 컷오프를 주지 않는다.** 현재 `grading.py`는 지표 개수(9개/7개)로 판정한다. 가중제로 바꾸려면 임계값을 새로 지어내야 하므로 근거 없는 추정이 된다 |
| 거래 빈도 상한 8회 | §12 `trade_frequency.hard_upper_bound: 8` | 세션 누적 거래 수라는 런타임 상태가 필요하다. 매매 루프에서 집계가 생길 때 연결한다 |
| 세션 사이즈 배수 | §7.1 개장 15분 0.5배, 프리마켓 마이크로 3계약 | `sessions.py`가 `entry_allowed` 불리언만 낸다. 사이즈 배수 필드 추가는 주문 경로가 생긴 뒤가 적절하다 |
| Método 청산 신호 | §12 `exit: cross_down(sma6, sma70)` | `sma_6_70_cross_down`이 계산은 되지만 아무도 쓰지 않는다. 청산 경로가 생길 때 연결한다 |
| Método signal_c | §2.3 MACD 0선 아래 교차 + 비관 극단 | `regime.pessimism_extreme`은 있으나 metodo 진입 판정에 연결되어 있지 않다 |
| 월요일 감점 | §7.2 `score_penalty: -1` | `calendar.monday_score_penalty`가 계산만 되고 등급에 반영되지 않는다. 가중 점수제와 함께 결정해야 한다 |
| KRX VI·단일가 제외 | §14.3 `filters` | `domain/toss_hlit_market_safety.py`와 `kis/domestic_vi.py`가 존재하나 전략 게이트에 연결되어 있지 않다 |
| 국가 강도 필터 | §2.1 [1] 강한 국가 | `universe.py`에 없다. 국장 전용 운용에서는 무의미하나 미장에는 필요하다 |
| Cyborg 계층 | §12 `cyborg.enabled: false` | 명세 자체가 비활성으로 지정 |

## 5. 검증

수정 후 상태다.

```
1428 passed, 17 skipped
ruff format --check : 372 files already formatted
ruff check          : All checks passed
pyright             : 0 errors
```

신규 테스트: `hlit.py` 12건, 엔진 게이트 9건, Big Trade 5건, 스프레드 2건.

Docker는 기동하지 않았다. DB·브로커·컨테이너가 필요한 검증은 여전히 미수행이다.
