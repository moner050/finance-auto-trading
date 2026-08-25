# David Trullàs Vila 전략 완전판 v6.0 — 마스터클래스·화면 역공학·자동매매 명세 통합

> **v6.0의 성격**: 기존 v5.0에서 복원한 **A0 핵심 알고리즘**은 유지하고, 공개 영상·홍보 화면·ATAS 기능 구조·공식 교육과정의 배치 순서를 근거로 기존의 **11개 미공개 항목을 화면 역공학 가설(V1)**로 보완했다.
>
> **가장 중요한 원칙**: **확정 규칙과 추정 규칙을 절대 섞지 않는다.** A0/A1은 전략 코어로 고정할 수 있지만, V1/C/X1은 반드시 설정 파일에서 별도 파라미터 세트로 관리하고 백테스트·섀도 거래를 통과하기 전에는 주문 권한을 주지 않는다.
>
> **증거강도**
>
> - **(A0)** David 본인 발화·자막·실거래 화면에서 직접 확인
> - **(A1)** David 공식 사이트·공식 교육과정·공식 대회·공식 플랫폼 문서
> - **(B)** 신뢰 가능한 제3자 인터뷰·출판물·플랫폼 교육자료
> - **(C)** A0/A1/B를 연결한 분석자 논리 추론
> - **(V1)** 영상·홍보 화면의 배치·색·수치·캔들·Footprint를 바탕으로 한 시각 역공학
> - **(X1)** NQ/MNQ 규칙을 Binance·한국시장 등 다른 시장에 이식하기 위한 정규화 규칙
>
> **신뢰도 표기**: **HIGH**(다수의 독립 단서가 일치) · **MEDIUM**(가장 유력하지만 대안 존재) · **LOW**(연구용 가설)
>
> **작성일**: 2026-08-24 · **버전**: 6.0

---

## 문서 사용법

이 문서는 세 층으로 읽어야 한다.

1. **전략 복원층**: Método Trullás, HLIT, Agotamiento, 존, 포지션 구축 등 A0 규칙
2. **화면 역공학층**: Big Trades, MIG, Ceros osmóticos, Secados, GOLD, Cyborg 등 V1 가설
3. **자동매매 명세층**: 상태 머신, 불변식, 데이터 요건, 백테스트·섀도·실거래 승격 기준

> **운영 권고**: 첫 실전 버전은 A0 코어만 활성화하고, V1 모듈은 `telemetry_only` 또는 `score_only`로 시작한다.

## 목차

- [0. v6.0에서 새로 밝혀진 것](#-0-v60에서-새로-밝혀진-것)
- [1. 확보 자료 총괄](#1-확보-자료-총괄)
- [2. Método Trullás](#2--método-trullás--완전-알고리즘-a0)
- [3. HLIT](#3--hlit--완전-알고리즘-v50-핵심)
- [4. 진입 실행과 Agotamiento](#4--진입-실행--소진agotamiento의-정체)
- [5. 실제 트레이드 사례](#5--실제-트레이드-3건--완전-해부-a0)
- [6~11. 빈도·세션·뉴스·리스크·존·플랫폼](#6-거래-빈도--고정-규칙의-거부-a0)
- [12. A0 코어 설정](#12-a0-코어-설정-v50-v60의-고정-기반)
- [13. 백테스트 설계](#13-백테스트-설계-v60)
- [14. 시장 이식 원칙](#14-시장-이식-원칙--한국가상화폐)
- [15~17. 확정 사실·한계·실행 로드맵](#15-확정-사실-최종-목록-a0)
- [18. 미공개 11개 항목의 통합 판정](#18-v60-미공개-11개-항목의-통합-판정)
- [19. Order Flow 화면 역공학](#19-order-flow-화면-역공학--big-tradesmigcerossecadosgold)
- [20. 손절·리스크·성과 역산](#20-손절리스크성과-역산)
- [21. Cyborg와 255066 레벨](#21-cyborg의-큰-구간-판정과-255066-레벨)
- [22. 자동매매 통합 명세와 Binance 이식](#22-자동매매-통합-명세와-binance-이식)
- [23. 출처](#23-출처)
- [24. 주의사항](#24-주의사항)

---

## 🎯 0. v6.0에서 새로 밝혀진 것

### 0.1 최대 공백 해소 — 피보나치 앵커 알고리즘

v4.0 §17.2에서 이렇게 썼다:

> ⭐ **"가장 큰 남은 공백은 '피보나치 앵커 선택'이다. 어느 스윙에 피보나치를 긋느냐가 전체 시스템의 출력을 지배하는데, 이것만은 어떤 영상에서도 명시되지 않았다."**

**이제 완전히 밝혀졌다 (A0).** 마스터클래스에서 그는 이 알고리즘을 **9개 종목·8개 타임프레임에 걸쳐 실시간으로 시연**했다. §3에 전문을 복원했다.

핵심은 한 문장이다:

> **"나는 이 비율을 그냥 쓰는 게 아닙니다. 나는 이 비대칭·불일치·다이버전스를 요구합니다. 없으면 그리지 않고, 없으면 매매하지 않습니다(Si no, no dibujo, si no no opero)."**

→ **다이버전스가 피보나치를 그릴 자격 조건이다.** 이 인과 순서를 뒤집으면 완전히 다른 시스템이 된다.

### 0.2 v4.0 대비 신규/정정 항목

| # | 항목 | v4.0 | v5.0 |
|---|---|---|---|
| 1 | **피보나치 앵커** | ❌ 미해결 공백 | ✅ **완전 규명** (§3) |
| 2 | **핵심 되돌림 레벨** | 0.382/0.5/0.618/0.786 추정 | ✅ **25% / 50% / 66%** (다우이론 2/3) |
| 3 | **Método Trullás 이평** | SMA 50/100/200 추정 | ✅ **SMA 6 / 70 / 200** |
| 4 | **피벗 확정 대기** | right=3봉 확정 필수 (C) | ⚠️ **그는 확정을 기다리지 않는다** (§4.3) |
| 5 | **정규 vs 히든 선호** | 용도별 배분 (C) | ✅ **"정규를 선호한다"** 명시 |
| 6 | **BE 손절 위치** | 정확한 진입가 | ✅ **진입가 +1.5포인트** |
| 7 | **2번째 계약 추가 시점** | TP1 이후 (C) | ✅ **+30포인트 도달 시** |
| 8 | **개장 직후 진입** | 5분 안티스파이크 (C) | ⚠️ **개장 22초 후에도 진입** |
| 9 | **거래 횟수** | 하루 최대 3~4 (C) | ✅ **고정 숫자 없음. 17분에 5거래 실례** |
| 10 | **뉴스 필터** | 3-star 중심 | ✅ **2-star + 3-star 모두. 지속시간 규칙** |
| 11 | **Big Trades** | 확인 신호 (+1점) | ✅ **회피 대상. 맞서 싸우지 않는다** |
| 12 | **월요일** | 배제 | ⚠️ **기록 경신 트레이드 2건이 월요일** |
| 13 | **브로커** | ATAS/NT/IB | ✅ **IB 선호 명시. MT5 비판** |

### 0.3 v6.0 신규 통합 항목

| # | 항목 | v5.0 상태 | v6.0 처리 |
|---|---|---|---|
| 1 | Big Trades 임계값 | 미공개 | **고정 계약 수보다 `Cumulative Trades + Auto Filter` 가능성이 높다는 V1 가설** |
| 2 | MIG 캔들 | 미공개 | **대형 체결의 노력과 가격 결과가 불일치하는 기관 흡수·반전 캔들 가설** |
| 3 | Ceros osmóticos | 미공개 | **Footprint의 0/near-zero 체결 셀 또는 극단 비대칭 셀 가설** |
| 4 | Secados | 의미 불명확 | 공식 과정의 **갇힌 롱·숏** 표현과 Delta/CVD 배치를 결합해 판정식 후보 작성 |
| 5 | GOLD | 1.618 추정 | 화면의 `61.8%` 템플릿을 확인해 **61.8% 되돌림 / 161.8% 확장**으로 분리 |
| 6 | 초기 손절 | 구조적 배치만 확인 | 미세 구조 극점 + 틱/ATR/스프레드 버퍼의 연구식 작성 |
| 7 | 실제 위험률 | 미공개 | 일반계좌·대회계좌·자동매매 안전값을 분리 |
| 8 | 대회 MDD | 미공개 | 공식치가 아닌 **조건부 시나리오 범위**로만 제시 |
| 9 | 승률 | 미공개 | 승리/BE/손실의 **성과 분포 가설**로 재정의 |
| 10 | Cyborg 큰 구간 | 도구 목록만 확인 | 필수조건 + 점수표의 연구용 결정 트리 작성 |
| 11 | 25%/50% | 용도 미공개 | 25%=첫 반응, 50%=균형·관리, 66%=최종 목표 가설 |
| 12 | Binance 이식 | 미포함 | 체결·Delta·CVD·가상 Footprint·ATR 정규화 명세 추가 |

> **v6.0의 성과는 “공백을 확정값으로 덮은 것”이 아니다.** 각 공백마다 가장 유력한 가설·대안 가설·검증법·실전 권한 수준을 분리했다.

---

## 1. 확보 자료 총괄

### 1.1 v5.0 신규 (A0)

| 자료 | ID | 분량 | 핵심 내용 |
|---|---|---|---|
| ⭐ **마스터클래스 (라이브 92분)** | `bm1SPttrRbM` | **11,850단어** | **3개 방법론 전체 시연 + Q&A** |
| **41년 기록 경신 트레이드 해설** | `ljT5a-OY-Qo` | 2,199단어 | 2,222% 달성 트레이드 전 과정 |
| **거래 빈도 규칙 + 5거래 실례** | `NGvqdsF6dj4` | 1,975단어 | 하루 몇 번 매매하는가 |
| **+1501% 트레이드 해설** | `mAlsGaEyRRI` | 1,415단어 | 개장 2분 전 진입 사례 |
| **개장 패턴 + 반론 답변** | `-dhwafVA6ac` | 2,340단어 | 개장 22초 후 진입, 사전 존 구축 |
| 선물 계약·롤오버 | `2AYbNirqqUE` | 2,628단어 | NQ/MNQ 승수·증거금·만기 |
| 역대 TOP3 트레이드 | `fp7k2JYMAv8` | 2,086단어 | — |
| 4연속 우승의 진실 | `yD7zFLwQ7u8` | 2,263단어 | — |
| 플랫폼 비교 2026 | `lllApwKl-8A` | 2,347단어 | TradingView/NinjaTrader/ATAS |
| Delta 양수 ≠ 상승 | `KhD206ckyDI` | 1,559단어 | — |
| 볼륨 클러스터 | `TJyBHPTuNio` | 1,678단어 | — |
| 볼륨의 실제 압력 | `XV44zg_x4vk` | 2,466단어 | — |
| Bid/Ask 풋프린트 | `i-121CJtLps` | 1,877단어 | — |
| MOTILIDAD(캔들 내부 볼륨) | `OR_tzWD_B7o` | 1,843단어 | 정골의학 용어 차용 |
| 대회 실전 매매 57분 | `Rm53qKZZG8o` | 6,238단어 | — |

### 1.2 v4.0에서 이월 (A0)

장시간 인터뷰(`fTcDHSDPv4g`, 15,952단어) · 작업환경 공개(`qmlqz7bAx6k`, 1,874단어)

### 1.3 마스터클래스 시점 추정 (C)

강연 중 **"미국 금리 결정이 스페인 시간 20:00"**, **"다음 금요일 6일이 실업 지표"**, **"세계 챔피언 3회 우승"**이 언급된다. 2026년 2월 6일이 금요일이고 3회 우승은 2025 Q2·Q3·Q4이므로 → **2026년 1월 말(FOMC 주간) 개최**로 추정된다. 동일 제목 라이브가 **총 5회**(92분~123분) 아카이브되어 있다.

### 1.4 v6.0 보강 자료 (A1/V1)

| 자료 | 확인된 내용 | 증거 |
|---|---|---|
| Live in Trading 공식 과정 | HLIT의 GOLD·피보나치 클러스터·확장, Cyborg의 Delta/CVD·Secados·Ceros osmóticos·Big Trades·MIG·기관 움직임 | A1 |
| Live in Trading 공식 소개 | HLIT는 피보나치 기반 되돌림, Cyborg는 시장 전환·대형 기관 포지션·3R 초과를 목표 | A1 |
| ATAS Big Trades 공식 문서 | `Cumulative Trades`와 `Separate Trades`, Auto Filter·Intensity·Min/Max Volume·Price Location 구조 | A1 |
| ATAS Footprint 공식 문서 | Bid×Ask, Delta, Imbalance, 0 값 포함 여부, 위치·볼륨 필터 구조 | A1 |
| WCTC 공식 참가 조건 | 분기 Futures 최소 시작 잔액 $2,500, NQ·MNQ 당일 증거금 예시 | A1 |
| David 홍보 화면 캡처 | 표준 피보나치 `0 / 23.6 / 38.2 / 50 / 61.8 / 76.4 / 89 / 100%` | V1 |

#### 화면 자료

![David 홍보 화면의 표준 피보나치 레벨](David_Trullas_Vila_v6.0_assets/david_promo_labels_crop.png)

> 위 이미지는 **HLIT의 25/50/66 템플릿이 아니라 표준 피보나치 화면**이다. 따라서 `61.8`이 보인다는 사실은 GOLD의 후보를 강화하지만, HLIT의 66%를 61.8%로 바꾸는 근거가 되지 않는다.

---

## 2. ⭐ Método Trullás — 완전 알고리즘 (A0)

**용도**: 일봉 스윙, 주로 주식. **"항상 일봉. 절대 인트라데이가 아니다(jamás es intradía)."**

### 2.1 4단계 톱다운 필터

```
[1] 강한 국가(país fuerte)      → 현재: 미국
     ↓
[2] 강한 섹터(sector fuerte)    → 상위 3개만. 4위부터는 폐기
     ↓
[3] 강한 종목(valor fuerte)     → 섹터 안에 있다고 자동 진입 아님
     ↓
[4] 메소드 적용                  → SMA / MACD 신호
```

**마스터클래스 시점 섹터 상태 (A0)**

| 강함 (진입 대상) | 약함 (회피) |
|---|---|
| ① 기술 (NVDA, AAPL) | 부동산(Real Estate) |
| ② 커뮤니케이션 서비스 (NFLX) | 금융(은행·보험) |
| ③ 산업재 (CAT) | 에너지(원유) |

> **"BBVA가 굉장한 상승 추세여도, 내 성향은 자본을 강한 섹터에 분산하는 것이다."** — 종목이 좋아도 섹터가 약하면 배제한다.

### 2.2 이동평균 규칙 ⭐ (A0)

**단순이동평균(SMA) 3개 — 6 / 70 / 200**

```python
SMA_FAST = SMA(close, 6)     # 녹색
SMA_MID  = SMA(close, 70)    # 적색
SMA_SLOW = SMA(close, 200)   # 청색

# 상승 추세 정의 (필수 전제)
uptrend_regime = (
    slope(SMA_SLOW) > 0                # 200이 양의 기울기
    and SMA_MID > SMA_SLOW             # 70이 200 위
    and slope(SMA_MID) > 0             # 70이 양의 기울기
)

# 매수 신호
buy_signal = uptrend_regime and cross_up(SMA_FAST, SMA_MID)   # 6이 70을 상향 돌파

# 매도(청산) 신호
sell_signal = cross_down(SMA_FAST, SMA_MID)

# 하락 추세 = 완전 대칭 (거울 효과)
downtrend_regime = (
    slope(SMA_SLOW) < 0 and SMA_MID < SMA_SLOW and slope(SMA_MID) < 0
)
short_signal = downtrend_regime and cross_down(SMA_FAST, SMA_MID)
```

> ⚠️ **SMA 6/70/200은 흔한 조합이 아니다.** 50/100/200이나 20/50/200이 아니다. 이 값은 그의 화면에서 직접 확인된 것이며, **최적화 대상이 아니라 고정값으로 취급**해야 한다.

### 2.3 MACD 규칙 (A0)

**설정: 표준 12 / 26 / 9. 일봉·인트라데이 모두 동일.**

```python
# 전제: uptrend_regime 성립
macd_buy_primary = uptrend_regime and cross_up(MACD, SIGNAL) and MACD > 0

# 0선 아래 교차는 조건부 허용
macd_buy_conditional = (
    uptrend_regime
    and cross_up(MACD, SIGNAL)
    and MACD < 0
    and market_sentiment == "PESSIMISM_EXTREME"     # ⭐ 필수 조건
)
```

**비관 극단 판별법 (A0)** — 놀랍도록 단순하다:

> **"신문과 TV가 '시장이 조정 중이고 피바다다'라고 말하면, 그건 매수 시점이 아니라 우리 방법론을 놓고 진입이 나오는지 볼 시점이다."**

실제 적용 사례: **2026년 4월 트럼프 관세 급락** 당시. "우리 지표가 비관 극단을 보여줬고 TV가 대서특필했다. 트럼프가 헛소리를 해서가 아니라 **우리 방법론이 진입을 줬기 때문에** 매수 신호였다."

```yaml
sentiment_extreme_detection:
  quantitative: [put_call_ratio, vix_percentile, breadth]   # 커리큘럼 M3
  qualitative: media_panic_signal                            # (A0) 실제로 사용
  action_on_extreme: "메소드 적용, 자동 진입 아님"
```

### 2.4 마스터클래스 실시간 검증 (A0)

청중이 종목을 부르면 그가 즉석에서 판정했다. **판정 논리가 그대로 드러난다.**

| 종목 | 판정 | 근거 |
|---|---|---|
| Cisco (CSCO) | ✅ **좋다** | 200 양기울기, 70이 200 위 양기울기, 6이 매수신호 + MACD도 신호 |
| 금(Gold) | ✅ **환상적** | 2025 초 6/70 교차 진입 → **+66~70%** |
| Indra | ✅ 좋다 | 200 양기울기 스펙터클, MACD 0선 돌파 반복 |
| IBEX 35 | ✅ 좋다 | 교과서적 상승 |
| Microsoft | ❌ **싫다** | 200은 양기울기지만 **70이 음기울기**, 6이 70 아래 |
| Coca-Cola | ❌ **싫다** | 횡보. 소비섹터 약함. "시간 낭비" |
| UNH | ❌ 싫다 | 하락 추세, 이평 3개가 뭉쳐있음 |
| Nike | ❌ 싫다 | 소비섹터 약함 + 하락 추세 |
| Baxter | 숏 대상 | 완전 거울 패턴 |
| EURUSD | ❌ **변동성 부족** | "1.16~1.18에 7~8개월. 2센트 움직임은 너무 적다" |
| Bitcoin | ❌ 싫다 | "거대한 횡보. 젊은 사람들에게 쿨하지만 나는 SP500·NASDAQ 같은 추세를 선호" |
| 천연가스(NG) | ✅ 시그널 사례 | 갭 상단 돌파 시 제시 → **2~3일에 60~70%** |

⭐ **핵심 판정 기준 (C, 위 사례에서 역산)**

```python
def trullas_daily_screen(sym):
    if country_strength(sym) != "STRONG": return "REJECT"
    if sector_rank(sym) > 3:              return "REJECT"    # 4위부터 폐기
    if slope(sma200) <= 0:                return "REJECT"
    if sma70 <= sma200:                   return "REJECT"
    if slope(sma70) <= 0:                 return "REJECT"    # ← MSFT 탈락 사유
    if is_sideways(price, lookback=180):  return "REJECT"    # ← KO 탈락 사유
    if realized_vol_pct < MIN_VOL:        return "REJECT"    # ← EURUSD 탈락 사유
    if cross_up(sma6, sma70) or (cross_up(macd, signal) and macd > 0):
        return "CANDIDATE"
    return "WATCH"
```

> **"지표가 쓸모없다고 들었다고요? 맥락 밖에 놓으면 쓸모없습니다. 맥락 안에 놓으면 쓸모 있습니다."** (A0)

---

## 3. ⭐⭐⭐ HLIT — 완전 알고리즘 (v5.0 핵심)

**"이것이 내가 세계 대회를 이기는 방식이다(así gano los campeonatos de esta forma)." (A0)**

### 3.1 알고리즘 전문

**강세 셋업 (매수)**

```
STEP 1  연속된 두 저점 min#1, min#2 식별 (min#2 < min#1, 가격은 저점 갱신)

STEP 2  동일 지점의 MACD 비교
        MACD(min#2) > MACD(min#1)  →  정규 강세 다이버전스 성립
        ❌ 불성립 시: 피보나치를 그리지 않는다. 매매하지 않는다.

STEP 3  앵커 확정
        앵커 A = min#1과 min#2 사이의 절대 최고점 (máximo absoluto entre esos dos mínimos)
        앵커 B = min#2 (절대 최저점)

STEP 4  A→B로 피보나치 되돌림 작도
        표시 레벨: 25% / 50% / 66%

STEP 5  목표 = 66% (다우 이론의 2/3)

STEP 6  진입은 존 도달만으로 하지 않는다 → 소진(agotamiento) 확인 필요 (§4)
```

**약세 셋업 (매도) — 완전 대칭**

```
STEP 1  연속된 두 고점 max#1, max#2 (max#2 > max#1, 가격은 고점 갱신)
STEP 2  MACD(max#2) < MACD(max#1)  →  정규 약세 다이버전스
STEP 3  앵커 A = max#1과 max#2 사이의 절대 최저점
        앵커 B = max#2
STEP 4  A→B 피보나치, 25/50/66%
STEP 5  조정 목표 66%
```

### 3.2 구현 코드 (C, A0 규칙의 직역)

```python
FIB_LEVELS = (0.25, 0.50, 0.66)      # ⭐ 25 / 50 / 66 — 그가 화면에 띄운 값
TARGET_LEVEL = 0.66                   # 다우 이론 2/3

def hlit_bullish_setup(bars, macd_hist):
    """
    A0: "나는 이 비대칭을 요구한다. 없으면 그리지 않고 매매하지 않는다."
    """
    lows = find_swing_lows(bars)
    if len(lows) < 2:
        return None

    m1, m2 = lows[-2], lows[-1]

    # ── STEP 1: 가격은 저점 갱신 ──
    if not (m2.price < m1.price):
        return None

    # ── STEP 2: 지표는 저점 상승 (다이버전스가 자격 조건) ──
    if not (macd_hist[m2.idx] > macd_hist[m1.idx]):
        return None                       # ← 여기서 끝. 작도조차 하지 않는다.

    # ── STEP 3: 앵커 ──
    seg = bars[m1.idx : m2.idx + 1]
    anchor_a = max(b.high for b in seg)    # 두 저점 사이의 절대 최고
    anchor_b = m2.price                    # 절대 최저

    # ── STEP 4: 피보나치 ──
    rng = anchor_a - anchor_b
    levels = {r: anchor_b + rng * r for r in FIB_LEVELS}

    return {
        'direction': 'long',
        'anchor_high': anchor_a,
        'anchor_low':  anchor_b,
        'levels':      levels,
        'target':      levels[TARGET_LEVEL],
        'divergence':  'regular_bullish',
        'div_strength': macd_hist[m2.idx] - macd_hist[m1.idx],
    }


def hlit_bearish_setup(bars, macd_hist):
    highs = find_swing_highs(bars)
    if len(highs) < 2:
        return None
    x1, x2 = highs[-2], highs[-1]
    if not (x2.price > x1.price):
        return None
    if not (macd_hist[x2.idx] < macd_hist[x1.idx]):
        return None
    seg = bars[x1.idx : x2.idx + 1]
    anchor_a = min(b.low for b in seg)     # 두 고점 사이의 절대 최저
    anchor_b = x2.price
    rng = anchor_b - anchor_a
    levels = {r: anchor_b - rng * r for r in FIB_LEVELS}
    return {'direction': 'short', 'levels': levels,
            'target': levels[TARGET_LEVEL], 'divergence': 'regular_bearish'}
```

### 3.3 마스터클래스 실시간 검증 목록 (A0)

그는 청중 요청에 따라 **즉석에서 8개 조합**을 시연했고 전부 66% 도달을 보였다.

| 종목 | 타임프레임 | 방향 | 결과 |
|---|---|---|---|
| EURUSD | 일봉 | 강세 | 66% 도달 |
| ETHUSD | 1시간 | 강세 | 66% 도달, "MACD가 유로달러보다 훨씬 명확" |
| BTCEUR | 4시간 | 약세 | 66% 이상 |
| AAPL | 15분 | 약세 | "엄청난 MACD 다이버전스" |
| BTCUSD | 15분 | 강세 | 66% 테스트 중 (90,800) |
| SP500 | **2분** | 약세 | 66% 도달. "내가 절대 쓰지 않는 타임프레임" |
| Gold(COMEX 선물) | 15분 | 약세 | 50~66% 사이 |
| DAX 선물 | 5분 | 약세 | 66% 도달 |

> ⭐ **"이 전략은 주식만이 아니다. 주식·외환·원자재·지수 — 특히 세계 시장의 광범위한 참여가 있는 것들에. Clínica Baviera나 Bodegas Riojanas에 적용하지 말고, Apple·Amazon·Netflix·Tesla·금·은·팔라듐·EURUSD·GBPUSD·USDJPY에 적용하라."** (A0)

### 3.4 마트료시카 트레이드오프 재확인 (A0)

> **"더 큰 마트료시카일수록 효과성이 높고 이익이 크지만, 손절이 걸리면 손실도 크다. 더 작은 마트료시카는 성공 확률이 낮아지지만 이익도 손실도 작다 — 자본을 과하게 넣지 않는 한. 전부 머니매니지먼트의 문제다."**

```yaml
hlit_scale_tradeoff:
  larger_swing:  {reliability: higher, profit: larger, loss: larger}
  smaller_swing: {reliability: lower,  profit: smaller, loss: smaller}
  rule: "동일 명목가로 가면 큰 스윙이 이익도 손실도 크다 → 사이즈로 조절"
```

### 3.5 신뢰도 참고치 (A0/B)

한 수강생이 **2025년 11월부터 이 방법으로 승률 86%, 3개월간 포트폴리오 +40%**를 보고했다.
그의 반응이 중요하다: **"조심해라, 그 비율은 내려간다. 브레이크를 걸어라, 자만하면 얻어맞는다."**

→ **86%를 시스템 목표로 삼지 말 것.** 본인조차 지속 불가능하다고 판단했다.

---

## 4. ⭐ 진입 실행 — "소진(Agotamiento)"의 정체

### 4.1 존 도달 ≠ 진입 (A0)

> **"나는 그 존이 표시되어 있었지만, 그렇다고 가격이 거기 도달하기를 기다렸다가 BUY LIMIT 주문을 넣고 사겠다는 뜻은 아니었습니다. 아닙니다. 내가 찾고 있던 것은 가격의 소진(el agotamiento del precio)이었습니다."**

**소진의 조작적 정의 (A0)** — 세 영상에서 동일하게 서술된다:

> **"가격이 새로운 저점을 만드는데 거래량은 줄어든다(el precio marca nuevos mínimos con menor volumen de negociación). 이렇게나 단순한 것."**

실제 사례에서 그는 **연속 3~4개 저점 갱신 + 계단식 거래량 감소**를 지목했다.

```python
def exhaustion_confirmed(bars, direction, min_legs=3):
    """
    A0: 새 저점 + 거래량 감소의 연쇄. "volume divergence como una catedral"
    """
    if direction == 'long':
        legs = consecutive_lower_lows(bars, n=min_legs)
    else:
        legs = consecutive_higher_highs(bars, n=min_legs)
    if len(legs) < min_legs:
        return False
    vols = [leg.volume for leg in legs]
    # 계단식 감소: 각 구간이 직전보다 작아야 함
    return all(vols[i] < vols[i-1] for i in range(1, len(vols)))
```

### 4.2 실행 스케일 — "5분 매크로 안의 5초" (A0)

> **"어떤 타임프레임에서 작업하냐고요? 전부입니다. 내 진입은 어디 있냐고요? 아마 5분 매크로의 5초 안에 있을 겁니다."**

실제 1501% 트레이드에서의 순서:

```
[1] 5분봉 — 거대한 상승 추세 + 조정 국면 확인
[2] 사전 표시된 존(빨간 사각형)에 접근
[3] 1분 + 30초 분할 화면으로 하강
[4] 30초에서 "성당 같은" 거래량 다이버전스 확인  ← 캔들 수가 2배라 더 잘 보임
[5] 풋프린트(검은 차트)에서 매수 체결 위치 확인 → 409.50 지목
[6] 진입
```

### 4.3 ⚠️ 피벗 확정 문제 — v4.0을 정정한다 (A0)

v3.0·v4.0은 **"피벗은 오른쪽 N봉 확정 후에만 사용. 어기면 백테스트 무효"**를 최상위 원칙으로 삼았다. 그런데 그가 직접 답했다.

**질문**: "새로운 고점이나 저점이 만들어졌다는 걸 어떻게 아나요?"

**답변 (A0)**:
> **"이미 조정하거나 반등하고 있을 때, 그게 이 거래에 대한 고점이라는 걸 압니다. 하지만 그게 66%를 주고 나서 돌아서서 또 다른 저점을 만들면, 다시 또 다른 진입을 줍니다. 나는 그것이 절대 고점·저점이 될지 모릅니다. 다만 그 순간 가능한 거래를 만들어준다는 건 압니다. 그래서 실행합니다. 또 다른 고점을 만들지는 고민하지 않습니다. 만들면 더 좋고, 또 기회를 주는 거니까요."**

**자동화 함의 (C) — 두 원칙을 분리해야 한다**

| 구분 | 원칙 |
|---|---|
| **백테스트 무결성** | ✅ **여전히 look-ahead 금지.** 봉 마감 시점에 알 수 있는 정보만 사용 |
| **셋업 생성 철학** | ⚠️ **잠정 피벗을 폐기하지 않는다.** 각 잠정 피벗은 후보 셋업을 만들고, 새 극점이 나오면 **새 셋업으로 갱신** |

```python
def generate_setups(bars, macd_hist):
    """
    ⭐ 확정 대기 대신 '잠정 피벗 → 후보 셋업 → 무효화/갱신' 모델
    look-ahead는 여전히 금지: 판정 시점은 항상 봉 마감
    """
    setups = []
    for i, bar in enumerate(bars):
        if not bar.is_closed:
            continue
        # 잠정 극점: 직전 N봉 대비 극값이며 반전이 시작된 상태
        prov = provisional_extreme(bars[:i+1], lookback=PIVOT_LEFT)
        if prov is None:
            continue
        s = hlit_bullish_setup(bars[:i+1], macd_hist[:i+1])
        if s:
            s['provisional'] = True
            s['invalidated_by'] = prov.price   # 이 값 돌파 시 무효, 새 셋업 생성
            setups.append(s)
    return setups
```

> **핵심**: "잠정 피벗이 깨졌다 = 손실"이 아니라 **"새 셋업이 생성된다"**로 처리한다. 이것이 그의 "두 번째 기회(segundas oportunidades)" 모듈의 정체다.

### 4.4 정규 vs 히든 — 선호 확인 (A0)

**질문**: "정규 다이버전스와 히든 중 선호가 있나요?"

**답변**:
> **"네, 정규 쪽에 성향이 있습니다. 의심의 여지 없이 정규를 선호합니다. 다만 오늘 개장에서 표시한 거래들을 보셨다면, 그것들은 정규가 아니라 히든이었습니다. 히든에서 나와서 그 안에서 움직였습니다."**

```yaml
divergence_preference:
  primary: regular          # ⭐ A0 명시
  secondary: hidden         # 개장 국면에서 실제 사용
  scoring:
    regular: +2
    hidden: +1
```

---

## 5. ⭐ 실제 트레이드 3건 — 완전 해부 (A0)

### 5.1 CASE A — 2,222% 달성, 41년 기록 경신 (2026-06-15 월요일)

**맥락**
- 전날 **미국-이란 평화협정**
- 금요일 만기가 미국 공휴일 → **목요일로 앞당겨진 이례적 롤오버**
- TradingView가 이미 9월물로 전환 → **개장 갭 상승** (빨간 사각형)
- **두 계약 간 200포인트 이상 괴리** → "그 갭은 조만간 메워진다. 항상 제자리로 돌아온다"

**전제 조건 (predisposición)**
```
① 가격이 아시아 세션 개장 저점(스페인 자정) 위 유지  ← 검은 수평선
② 가격이 1시간봉의 특정 존 위 유지                    ← 녹색 수평선
   (과거 고점=저항 → 저점=지지로 전환된 플립 존)
→ 둘 다 지켜지는 동안 롱만 찾는다
```

**진입**
- 스페인 15:30(=ET 09:30) **직전**, 1분봉
- ⚠️ **"매우 늦게 들어갔다"고 본인이 인정.** 심리적으로 **얼어붙어(atenazado)** 있었음
- 정상 진입 지점: **거래량 밀집 구역 이후 2~3개 연속 저점 + 거래량 감소** 지점의 **타이트한 자리**
- 실제로는 그 자리를 놓치고 **평소 청산하는 자리**에서 진입

**⭐ 포지션 구축 — 정확한 기계적 절차**
```
1. 1번 계약 진입 → 되돌림 전혀 없음
2. 가격이 정확히 +30포인트 상승
3. +30 지점에서 2번 계약 진입
   (이 시점: 2번은 브레이크이븐, 1번은 +30 확보)
4. ⭐ 손절을 두 계약의 가중평균 브레이크이븐(ponderación de ambos)으로 이동
5. → 포지션 전체가 무위험 상태
```
> **"이걸 정말 많이 합니다. 브레이크이븐에서 손절이 걸려도 아무 문제 없습니다."**

**관리**
- **Big Trades 2개 출현** → 항상 경계 태세
- 큰 조정 발생 → 가중 BE 손절 덕에 "완전히 고통스럽진 않았지만 상황이 험해졌다"
- 가격 재출발 → **직전 고점 부근에서 1번 계약 청산**
- 또 다른 Big Trade 출현 → 경계
- **바로 위에 새 Big Trade가 나타나는 지점에서 2번 계약 청산**
- 직후 가격이 **몇 분 만에 50~60포인트 급락**

**결과**: 약 **2,222%** → **대회 중단 결정** ("사실상 우승 확정이니 더 건드리지 않는 게 낫다")

**기록**: 1985년 Ralph Casadone의 **1,283%를 41년 만에 경신**. 전체 역사 2위(1위 Larry Williams). 그는 **역대 TOP 10 안에 3개 기록**(2위 2222%, 6위 930%, 9위)을 보유.

### 5.2 CASE B — +1501% 달성 (2026-06-01 월요일)

| 항목 | 값 |
|---|---|
| 진입 | 스페인 **15:28 = 개장 2분 전** |
| 청산 | 15:32~15:34 |
| 보유 | **약 4분** |
| 방향 | 롱 |

**절차**
1. **5분봉**: 거대한 상승 추세 + 조정 국면
2. **사전 표시 존**(빨간 사각형) — 과거 고점/저점 반복 지점
3. ❌ **BUY LIMIT 대기 아님** → **소진** 탐색
4. **1분 + 30초 분할**: 하락 구간에서 **거래량 감소 = 강세 다이버전스 "성당처럼 뚜렷"**
5. 풋프린트에서 매수 체결 밀집 지점 확인 → **409.50** 지목
6. 청산은 **403** — **"보통 시장에 몇 포인트를 선물한다. 정확한 그 가격과 싸우지 않는다."**

**⭐ 목표 미달 고백**: 다우 2/3 목표는 **89포인트 위**였다. 그는 **1500%라는 예쁜 숫자**를 만들려고 조기 청산했다. **"인정합니다. 나가야 할 때보다 먼저 나갔습니다."**

### 5.3 CASE C — 17분 5거래 세션 (일자 미상)

**5거래 전부의 진입·청산 (A0)**

| # | 진입 | 청산 | 결과 |
|---|---|---|---|
| 1 | 10.75 | 12 | **+1.5pt** (사실상 BE) |
| 2 | 30,040 | 40.25 | 사실상 BE |
| 3 | 36.25 | 27.50 | **손실** |
| 4 | 30.10 | 16.25 | 직전 손실 회복, BE |
| 5 | 31 | 95 | **목표 달성. 순 +100pt대** |

**총 소요: 15:45~16:02 = 17분**

⭐ **이 분포가 그의 실제 손익 구조를 보여준다: 5거래 중 4거래가 브레이크이븐 언저리, 1거래가 수익을 만든다.**

**⭐ BE 손절 규칙 — 정확한 값 (A0)**
> **"보통 항상 손절을 진입가보다 1.5포인트 위에 둡니다. 왜냐면 ① 수수료를 커버하고 ② 손절이 슬리피지로 더 나쁜 가격에 체결되더라도 최소한 완전한 브레이크이븐이 되도록 계산하기 때문입니다."**

NQ 1.5pt = **$30/계약**.

**⭐ 언제 BE로 옮기는가 (A0)**
> **"이미 100~200 정도의 이익이 있을 때. 계속 뚫릴 수 있는 위험한 구역에 있으니 포지션을 보호합니다. BE로 쫓겨나도 상관없습니다. 수수료 내고 끝."**

**⭐ Big Trades 회피 (A0)**
> **"이 크리스마스 방울들? 저는 그것들과 맞서는 걸 좋아하지 않습니다. 여기서 시도해봤는데 시도할 때마다 잘못됐습니다. 그래서 '이제 저기서 안 싸운다, 이미 4거래를 싸웠고 1,000 몇백의 이익이 있으니 그만 간다'고 했습니다."**

### 5.4 CASE D — 마스터클래스 당일 실거래 (A0)

**7 MNQ 마이크로 + 2 NQ 미니**

| 상품 | 내역 | 결과 |
|---|---|---|
| NQ 미니 2건 | 1차 롱 → **-$500** / 2차 롱(두 번째 기회) → 회복 +$180 | 순 **-$320 → +$180** |
| MNQ 마이크로 7건 | 5마이크로 패키지를 **저점 부근 진입, 고점 부근 청산** | **+$570** |

> **"제가 틀렸나요? 아뇨, 틀리지 않았습니다. 잘 안 됐을 뿐입니다. 모든 거래가 잘 되진 않지만, 시장이 두 번째 기회를 주면 실행합니다."**
> **"이건 과잉거래가 아닙니다. 그냥 매우 외과적으로. 핀, 팜, 품."**

---

## 6. 거래 빈도 — 고정 규칙의 거부 (A0)

> **"하루에 1개, 또는 3~5개라고 닫힌 답을 주는 사람들이 있습니다. 그건 아무 의미가 없습니다, 전혀. (…) 1개일 수도, 3개일 수도, 5개일 수도 있습니다. 150이나 300 거래를 난사하면 당연히 잘못된 겁니다. 하지만 '하루 1거래' 또는 '3~5거래'로 닫아버리는 건 **멍청이들이나 하는 소리(de Lerdos)**입니다."**

> **"나는 거래 횟수로 일하지 않고 **목표(objetivos)**, 또는 **시장이 주는 것**에 따라 일합니다."**

```yaml
trade_frequency:
  fixed_daily_count: false          # ⭐ A0 — 고정 숫자 금지
  driven_by: [objective_reached, market_opportunity]
  hard_upper_bound: 8               # (C) 난사 방지 상한만, David 공식값 아님
  observed_examples:
    - {session: "17분", trades: 5}
    - {session: "마스터클래스 당일", trades: 9}
  exit_rule: "objetivos cumplidos, largaos y cerrar la pantalla"
```

**목표 설정 방식 (A0)**: 수강생별로 **금액 목표**($500, $250 등)를 정하고 거기 맞춰 머니매니지먼트. **"나는 매일 정확한 금액을 정하지 않습니다. 거래를 찾고, 읽고, X를 주면 좋고, X+100이면 완벽하고, X+500이면 환상적입니다."**

> ⚠️ **"큰 구간을 노리지 마라(no vayáis a buscar grandes recorridos)"** — 케이크의 조각을 노려라.

---

## 7. 세션·시간 — 대폭 정정 (A0)

### 7.1 개장 직후 매매 — v4.0 정정

v4.0은 `anti_spike_delay_minutes: 5`를 넣었다. **그는 이를 정면으로 반박한다.**

> **"수익 나는 트레이더들은 변동성 때문에 최소 15분 기다려야 한다고 하는데 왜 개장을 매매하냐고요? 개장에는 리테일만 있는 게 아니라 **금융기관**이 있습니다. 개장에 움직이는 돈은 비교할 수 없이 큽니다. **97~99%가 기관 자금**입니다. 첫 15분에는 프로들이 매매하고 있습니다."**

**실제 진입 시각 (A0)**
- CASE B: 개장 **2분 전**
- 개장 패턴 영상: **15:30:22와 15:30:38 = 개장 22초·38초 후**
- 그날의 수익 거래: **개장 약 20분 후**

**⭐ 단, 사이즈로 제어한다 (A0)**
> **"프리마켓에서 명백히 하지 않을 일은 10계약으로 난사하는 겁니다. 유동성이 적고, 스프레드가 벌어지고, 손절이 엉뚱한 데서 걸리니까요. 하지만 그렇다고 **마이크로 1~2~3개를 못 넣는다는 뜻은 아닙니다.**"**

```yaml
session_v5:
  pre_open_entry:   {allowed: true,  max_size: micro_only, max_contracts: 3}
  open_0_15min:     {allowed: true,  size_multiplier: 0.5}
  post_15min:       {allowed: true,  size_multiplier: 1.0}
  anti_spike_delay: 0                  # ⭐ v4.0의 5분 대기 폐기
  spread_guard: {max_spread_ticks: 3}  # 사이즈 대신 스프레드로 제어
  arrival_before_open_minutes: 75      # A0: "개장 1시간~1시간15분 전 사무실 도착"
  active_productive_work_minutes: 60   # A0: "생산적 작업은 1시간, 그 이상 못 함"
  passive_position_hold: allowed       # A0: "2시간 동안 마이크로를 걸어두는 건 별개"
```

**⭐ 프리마켓 볼륨 비교의 정확한 방법 (A0)** — 흔한 오해를 그가 직접 교정:
> **"프리마켓 볼륨과 개장 볼륨을 비교하지 마세요. 비교 대상이 아닙니다. 저는 **프리마켓 안의 두 구간(dos tramos de la preapertura)**을 비교합니다."**

### 7.2 월요일 — v4.0 정정

v4.0은 `monday: false`로 배제했다. 그러나 **CASE A(2222%)와 CASE B(1501%)가 모두 월요일**이다.

**정확한 표현 (A0)**: "los lunes prácticamente no, pero casi nunca" — 월요일은 **거의** 안 한다.

```yaml
weekday_filter:
  monday: {allowed: true, score_penalty: -1}   # 배제가 아니라 감점
  note: "본인 최고 기록 2건이 월요일. 습관이지 규칙이 아님. ablation으로 검증"
```

### 7.3 최적 시간대 (A0)

| 스타일 | 시간(스페인) |
|---|---|
| 강한 움직임 선호 | **미국 개장 15:30** |
| Forex | 런던 개장 09:00 |
| 좀 더 차분 | 17:00 |
| 가장 차분 | 19:00 |

---

## 8. 뉴스 필터 — 라이브 시연 기반 (A0)

마스터클래스에서 그는 **FOMC 금리 발표 5초 전에 마이크로 롱을 실제로 진입**했다. 목적은 수익이 아니라 **시연**이었다.

> **"돈을 벌거나 잃고 싶지 않습니다. 무슨 일이 일어나는지 보여드리고 싶을 뿐입니다. 여러분은 이 거래를 따라하지 마세요."**

**관찰된 메커니즘 (A0)**
> **"먼저 아래로 쓸어내고(barrido), 그다음 가격을 올리고, 결국 가격은 원래 하려던 걸 합니다. 그건 작은 고객인 우리가 레버리지를 쓰게 만들어 이쪽저쪽으로 빗자루질하는 핑계일 뿐입니다."**

**⭐ 지속 시간 규칙 (A0)** — 자동화에 직접 쓸 수 있다

| 지표 유형 | 영향 지속 |
|---|---|
| 서프라이즈 없는 지표(예상대로 금리 동결) | **수 분** |
| 강한 지표(금리 결정 20:00) | **22:00 종가까지** (약 2시간) |
| **실업/고용지표(매월 첫 금요일 14:30)** | **세션 전체**. 200~300pt 급락 후 세션 중 회복 |

**영향 큰 자산**: **지수와 EURUSD**

```yaml
news_filter_v5:
  block_stars: [2, 3]                    # ⭐ A0: "2성과 3성 모두"
  windows:
    non_surprise:      {block_after_minutes: 10}
    strong_release:    {block_after_minutes: 120}
    nfp_first_friday:  {block_entire_session: true}
  high_impact_assets: [indices, EURUSD]
  observed_pattern: "역방향 스윕 후 본래 방향"
  policy: "데이터 순간에는 포지션 보유 금지"
```

---

## 9. 포지션·리스크 규칙 v5.0 (A0 통합)

### 9.1 포지션 구축 3형태

| 형태 | 트리거 | 출처 |
|---|---|---|
| **A. +30pt 추가형** | 1계약 진입 → **+30pt** → 2계약 추가 → **가중평균 BE 손절** | CASE A |
| **B. 현금화 후 마이크로형** | 풀사이즈 → TP1 현금화 → **확보 수익 내에서만** MNQ 추가 | 인터뷰(v4.0) |
| **C. 멀티스케일형** | 5초 조기 진입(타이트 손절) → 30초/1분 확인 진입을 5초 수익이 보호 | 작업환경 영상 |

**세 형태의 공통 불변식 (C)**

```python
INVARIANTS = [
    "추가 진입은 오직 가격이 유리하게 움직인 뒤에만",
    "추가 후 손절은 항상 결합 포지션의 가중평균 BE 이상",
    "원금(base capital)으로 추격 금지 — 확보 수익 범위 내에서만",
    "총 리스크는 추가 진입 시점에 감소하거나 유지되어야 하며 절대 증가 금지",
]
```

```python
def add_second_contract(pos, bar, cfg):
    """CASE A 형태의 구현"""
    if pos.qty != 1 or pos.added:
        return
    favorable = (bar.close - pos.entry) if pos.dir == 'long' else (pos.entry - bar.close)
    if favorable < cfg.add_threshold_points:        # 기본 30pt (NQ)
        return
    new_entry = bar.close
    enter(qty=1, price=new_entry)
    # ⭐ 가중평균 BE + 1.5pt 버퍼
    wavg = (pos.entry * pos.qty + new_entry) / (pos.qty + 1)
    buffer = cfg.be_buffer_points                    # 1.5pt
    pos.stop = wavg + buffer if pos.dir == 'long' else wavg - buffer
    pos.added = True
```

### 9.2 손절 규칙 통합

```yaml
stop_v5:
  initial:
    type: structural
    placement: "조정 구간의 확정 저점/고점 바깥"
    philosophy: "너무 타이트 금지 — '나무를 흔들어 떨어뜨린다'"
    minimum_distance_atr: 0.40
    maximum_distance_atr: 1.50
    percentage_based: forbidden
  breakeven_move:
    trigger_profit_usd: 150            # A0: "100~200 정도 이익 있을 때"
    trigger_or_context: "위험 구역 진입 시 즉시"
    placement: "entry + 1.5 points"    # ⭐ A0 정확값 (NQ 기준 $30)
    rationale: "수수료 커버 + 슬리피지 흡수 후 완전 BE"
    after_add: "가중평균 BE + 1.5pt"
  protective_order_type: STOP_MARKET   # STOP_LIMIT 금지 (Gowex 사례)
  never_widen: true
```

### 9.3 익절 규칙 통합

```yaml
targets_v5:
  primary_target: "HLIT 66% (다우 2/3)"
  observed_partial_r: [1.2, 1.5]       # A0 인터뷰
  cyborg_reversal_r: [3.0, 4.0]
  exit_discipline:
    gift_points_to_market: 5           # A0: "정확한 가격과 싸우지 않는다"
    exit_before_big_trades: true       # ⭐ A0: Big Trades와 맞서지 않는다
    exit_on_objective: true            # "목표 달성하면 화면 닫고 나가라"
  do_not_chase_large_moves: true       # "큰 구간을 노리지 마라"
```

### 9.4 Big Trades 처리 — 역할 재정의 (A0)

v4.0은 Big Trades를 **확인 신호(+1점)**로 넣었다. 실제 사용법은 **회피 대상**이다.

```yaml
big_trades_v5:
  timeframe: 5m
  marker_colors: [yellow, red]         # A0: 두 색 모두 사용
  role:
    - obstacle_ahead: "진행 방향 앞의 Big Trade → 진입 감점 또는 거부"
    - exit_signal: "포지션 방향 앞에 출현 → 청산 검토"
    - alert: "출현 즉시 경계 태세"
  never: "Big Trade와 맞서 진입하지 않는다"
  scoring:
    blocking_ahead: -2
    behind_supporting: +1
```

---

## 10. 존(Zone) 구축 규칙 (A0)

> **"그 사각형은 이미 그려져 있었습니다. 제가 과거의 고점·저점·종가·시가 구역을 식별해뒀기 때문입니다. **언제까지요? 제가 필요한 날짜까지입니다. 5분 차트에서 한 달 전까지 가는 건 터무니없습니다.** 거기엔 아무것도 없을 테니까요, 사상 최고가 구간에 있으니."**

> **"존은 좁지도 두껍지도 않습니다. **가격이 이전에 어떻게 움직였는지에 따라(en función de cómo se ha movido el precio anteriormente)** 넓이가 결정됩니다."**

```python
def build_zones(bars_5m, lookback_days=None):
    """
    ⭐ 존 폭은 고정 ATR 배수가 아니라 과거 가격 행동에서 도출
    lookback은 '필요한 만큼'. 사상 최고가 구간이면 짧게.
    """
    if lookback_days is None:
        lookback_days = 3 if at_all_time_high(bars_5m) else 10

    pivots = collect(bars_5m, lookback_days,
                     kinds=['swing_high', 'swing_low', 'close', 'open'])
    clusters = cluster_by_density(pivots)

    zones = []
    for c in clusters:
        if c.touch_count < 3:                 # 고·저·고·저 반복 확인
            continue
        zones.append({
            'lo': c.min_price,
            'hi': c.max_price,                # ⭐ 폭 = 실제 가격 분포
            'touches': c.touch_count,
            'strength': min(c.touch_count, 5),
        })
    return zones
```

---

## 11. 플랫폼·브로커 (A0)

| 항목 | 그의 선택/의견 |
|---|---|
| **선호 브로커** | **Interactive Brokers** — "세계 최대" |
| 대안 | Saxo Bank(덴마크). 스페인 거주자는 스페인 지사 있는 곳 |
| 차트 | TradingView |
| 실행 | **ATAS**(주력), NinjaTrader(프랍펌), R\|Trader(WCTC) |
| **MetaTrader 5** | ❌ **비판적**. "MT5 쓰는 브로커는 마켓메이커, 손절이 엉뚱한 데서 걸리고 가격이 다르다" |
| 규제 시장만 | **주식·선물·옵션** |
| 회피 | Vanuatu, Virgin Islands, Madagascar 등. **CNMV "chiringuitos financieros" 목록 확인** |
| 프랍펌 선택 | NinjaTrader/ATAS 연결 + **Rithmic** 되는 곳. **TradingView 기반 회피** |
| 이해관계 | **브로커로부터 대가를 받지 않으며 다수 제안을 거절** |

**계약 사양 (A0)**

| | 승수 | 30,000pt 기준 명목 | 브로커 증거금 범위 |
|---|---|---|---|
| **NQ** | ×$20 | $600,000 | $2,000 ~ $30,000+ |
| **MNQ** | ×$2 | $60,000 | 훨씬 낮음 |

**만기**: 3·6·9·12월, 통상 셋째 금요일. **만기까지 들고 가면 안 됨.** 롤오버 주간에는 양 계약이 공존하며 **거래량이 이동한 쪽으로 옮겨야 함**.

---

## 12. A0 코어 설정 v5.0 — v6.0의 고정 기반

> 아래 YAML은 **A0 고정값과 자동매매 안전 후보(C/X1)를 함께 담고 있다.** `source_status`가 없는 기존 필드는 원문 v5.0 구조를 보존한 것이며, 특히 위험률·ATR 경계·거래 횟수 상한은 David의 공식값이 아니다.

```yaml
strategy:
  id: trullas_v6
  version: "6.0"
  evidence_base: "v5 A0 core + official curriculum/ATAS docs + visual reverse engineering V1"

engines:
  daily_swing:                          # Método Trullás
    enabled: true
    timeframe: 1d
    universe_filter:
      country_strength: [US]
      sector_rank_max: 3                # ⭐ 4위부터 폐기
      exclude_sectors: [real_estate, financials, energy]
      min_liquidity: mega_cap
      exclude: [small_caps, low_volatility_fx]
    indicators:
      sma: [6, 70, 200]                 # ⭐ A0 실측
      macd: [12, 26, 9]
    entry:
      regime: "slope(sma200)>0 AND sma70>sma200 AND slope(sma70)>0"
      signal_a: "cross_up(sma6, sma70)"
      signal_b: "cross_up(macd, signal) AND macd > 0"
      signal_c: "cross_up(macd, signal) AND macd < 0 AND sentiment==PESSIMISM_EXTREME"
    exit: "cross_down(sma6, sma70)"

  hlit:                                 # ⭐ 핵심 엔진
    enabled: true
    timeframes: [1d, 4h, 1h, 15m, 5m, 1m, 30s, 5s]
    precondition: regular_divergence_required   # ⭐ 없으면 작도조차 금지
    oscillator: macd_12_26_9
    anchor_rule:
      bullish: "A = max(high) between low1..low2 ; B = low2"
      bearish: "A = min(low) between high1..high2 ; B = high2"
    fib_levels: [0.25, 0.50, 0.66]      # ⭐ A0 실측
    target: 0.66                        # 다우 이론 2/3
    entry_confirmation: exhaustion       # 존 도달만으로 진입 금지
    exhaustion_def: "연속 3+ 극점 갱신 + 계단식 거래량 감소"
    divergence_preference: {regular: 2, hidden: 1}
    provisional_pivots: true             # ⭐ 확정 대기 안 함, 갱신 모델

  cyborg:                               # 확인 레이어
    enabled: false                       # 틱 데이터 확보 후
    tools: [footprint_30s, delta, cvd, market_profile, big_trades]
    role: "peces gordos 위치 파악 + 더 큰 구간 가능성 판단"

universe:
  primary: [NQ, MNQ]
  hlit_eligible: [AAPL, AMZN, NFLX, TSLA, NVDA, GOLD, SILVER, PALLADIUM,
                  EURUSD, GBPUSD, USDJPY, SP500, DAX, BTCUSD]
  forbidden: [small_caps, illiquid, low_vol_fx]
  specialization: "소수 종목 집중. 본인은 NASDAQ만"

session:
  arrival_before_open_min: 75
  productive_work_minutes: 60
  pre_open_entry: {allowed: true, max_micro_contracts: 3}
  open_first_15min: {allowed: true, size_multiplier: 0.5}
  anti_spike_delay_minutes: 0
  weekday: {monday: {allowed: true, score_penalty: -1}}
  overnight: false
  premarket_volume_compare: "프리마켓 내 두 구간끼리 비교"

position_building:
  mode_a_add_on_favorable:
    trigger_points: 30
    stop_after_add: "weighted_avg_be + 1.5"
  mode_b_bank_then_micro:
    funded_by: realized_pnl_only
    cap_fraction: 0.30
  mode_c_multiscale:
    early_scale: 5s
    confirm_scale: [30s, 1m]
    protection_from_early_unrealized: true
  invariants:
    - never_add_while_losing
    - never_widen_stop
    - never_fund_from_base_capital
    - total_risk_never_increases_after_add

stop:
  initial: {type: structural, min_atr: 0.40, max_atr: 1.50, source_status: C}
  be_move: {trigger_profit_usd: 150, placement_offset_points: 1.5}
  order_type: STOP_MARKET
  percentage_based: forbidden

targets:
  primary: fib_066
  partial_r: [1.2, 1.5]
  cyborg_r: [3.0, 4.0]
  gift_points: 5
  exit_before_blocking_big_trade: true

trade_frequency:
  fixed_count: false
  hard_upper_bound: 8
  driven_by: [objective, market_opportunity]

news:
  block_stars: [2, 3]
  non_surprise_block_min: 10
  strong_release_block_min: 120
  nfp_block_entire_session: true

risk:
  source_status: IMPLEMENTATION_SAFETY_CANDIDATE_NOT_DAVID
  per_trade: 0.0015
  a_grade_max: 0.0025
  daily_stop: 0.0075
  weekly_stop: 0.0200
  consecutive_loss_halt: 2
  forbid_risking_banked_profit: true

platform:
  broker_preferred: InteractiveBrokers
  execution: ATAS
  prop: {require: [ninjatrader_or_atas, rithmic], avoid: tradingview_only}
  forbidden: [metatrader5_marketmaker_brokers, offshore_unregulated]

ui:
  pnl_color: neutral
  marker_color: yellow
  hide_leaderboard_during_session: true

prohibited:
  - blind_limit_entry_at_zone          # ⭐ A0: "BUY LIMIT 대기 아님"
  - fibonacci_without_divergence       # ⭐ A0: "없으면 그리지 않는다"
  - range_breakout_entry
  - fighting_big_trades
  - averaging_down
  - martingale
  - stop_widening
  - stop_limit_protective
  - percentage_based_stop
  - trading_during_2_3_star_news
  - fixed_daily_trade_count
  - small_cap_universe
  - permanent_long_only
  - deriving_targets_from_championship_returns
```

---

## 13. 백테스트 설계 v6.0

### 13.1 Ablation 재구성 (A0 근거 반영)

| 실험 | 구성 | 검증 질문 |
|---|---|---|
| `H0` | **HLIT 단독**: 정규 다이버전스 + 66% 목표 | **핵심 엔진 자체가 기대값 > 0인가?** |
| `H1` | H0 + 소진 확인(거래량 감소 연쇄) | 소진 필터가 개선하는가? |
| `H2` | H1 + 사전 존 겹침 요건 | 존이 개선하는가? |
| `H3` | H2 + 상위 프레임 거부권(일/1H) | LAYER 0가 개선하는가? |
| `H4` | H3 + 히든 다이버전스 추가 | 히든 포함이 개선하는가? |
| `P1` | H3 + **모드 A(+30pt 추가 + 가중 BE)** | 추가 진입이 개선하는가? |
| `P2` | H3 + 모드 B(현금화 후 마이크로) | |
| `P3` | H3 + 모드 C(멀티스케일 5초) | 5초가 슬리피지를 이기는가? |
| `S1` | 최선 + BE 이동(진입가 +1.5pt) | BE 규칙이 개선하는가? |
| `N1` | 최선 + 뉴스 필터(2·3성) | |
| `B1` | 최선 + Big Trades 회피 | |
| `M1` | 최선 + 월요일 포함/제외 | |
| `T1` | 최선 + Método Trullás 일봉 병행 | 포트폴리오 효과 |

### 13.2 파라미터 규율 — 고정값 확대 (v5.0)

**(A0) 근거가 있는 값은 최적화 금지 대상으로 이동한다.** 자유 파라미터가 줄어든 것이 v5.0의 실질적 이득이다.

```yaml
fixed_by_evidence:                       # ⭐ 최적화 금지
  - sma: [6, 70, 200]
  - macd: [12, 26, 9]
  - rsi_length: 14
  - fib_levels: [0.25, 0.50, 0.66]
  - target_level: 0.66
  - be_offset_points: 1.5
  - add_threshold_points: 30
  - sector_rank_max: 3
  - divergence_required: true
  - instruments: [NQ, MNQ]

free_parameters:                          # 최대 8개로 축소
  - exhaustion_min_legs: [2, 3, 4]
  - zone_min_touches: [2, 3, 4]
  - stop_min_atr: [0.3, 0.4, 0.5]
  - stop_max_atr: [1.2, 1.5, 2.0]
  - be_trigger_profit: [100, 150, 200]
  - risk_per_trade: [0.001, 0.0015, 0.0025]
  - active_window_end: ["10:30", "11:00", "11:30"]
  - execution_scale: [1m, 30s, 5s]
```

> v4.0에서 자유 파라미터 12개 상한을 뒀는데, **v5.0에서는 8개로 줄었다.** 그의 알고리즘이 규명될수록 최적화할 것이 줄어든다 — 이것이 올바른 방향이다. 그가 33,500조 조합에서 실패한 이유가 파라미터 폭발이었음을 기억할 것.

### 13.3 특유의 함정 (v5.0 신규)

| 함정 | 대응 |
|---|---|
| **다이버전스 전제 누락** | 피보나치를 먼저 그리고 다이버전스를 나중에 확인하면 완전히 다른 시스템. **순서를 코드로 강제** |
| **잠정 피벗 처리** | 확정 대기(look-ahead 방지)와 셋업 갱신(그의 방식)을 **분리 구현**. 판정 시점은 봉 마감 고정 |
| **66% vs 61.8%** | 그는 **66%(2/3)**를 쓴다. 0.618로 바꾸면 다른 시스템 |
| **BE +1.5pt** | 정확히 BE로 옮기면 수수료만큼 손실. 1.5pt 오프셋 필수 |
| **5초 스케일 슬리피지** | 백테스트는 반드시 틱 데이터. 분봉 리샘플링 금지 |
| **Big Trades 데이터** | 임계값 미공개. 자체 정의 시 반드시 파라미터로 노출하고 민감도 분석 |
| **섹터 강약의 시점 정합성** | 당시 시점 기준 섹터 랭킹을 써야 함. 현재 랭킹 사용 시 look-ahead |

---

## 14. 시장 이식 원칙 — 한국·가상화폐

**HLIT의 핵심이 OHLCV + MACD만 요구한다는 점이 확인되어 이식성 평가가 상향된다.**

| 요소 | 이식성 | 비고 |
|---|---|---|
| **HLIT 전체 알고리즘** | 🟢 **완전** | MACD + 스윙 + 피보나치. OHLCV만 필요 |
| Método Trullás(SMA 6/70/200 + 섹터) | 🟢 **완전** | **KRX 업종 분류 + 기존 스크리닝 시스템과 직결** |
| 소진(거래량 감소 연쇄) | 🟢 완전 | 거래량만 필요 |
| 사전 존 구축 | 🟢 완전 | |
| 66% 목표 | 🟢 완전 | 시장 무관 |
| BE +1.5pt | 🟡 **재환산 필요** | KOSPI200 호가단위 0.05pt=12,500원 → 1.5pt는 과대. **1~2틱으로 재정의** |
| +30pt 추가 규칙 | 🟡 **재환산 필요** | NQ 30pt≈0.1% → KOSPI200에서는 **ATR 비율로 재정의** |
| 30초/5초 스케일 | 🔴 어려움 | 호가단위·틱밀도 문제 |
| 풋프린트/Delta | 🔴 제한 | |
| Big Trades | 🟡 근사 | 대량 체결 필터 |

### 14.1 이식 시 절대 보존할 것

```text
다이버전스 → 앵커 → 25/50/66 → 소진 → 진입의 인과 순서
66% 목표
손실 중 추가 진입 금지
손절 확대 금지
포지션 추가 후 총 위험 증가 금지
```

### 14.2 NQ 고유값을 정규화해야 하는 항목

| NQ 규칙 | 다른 시장에서의 처리 |
|---|---|
| +30포인트 추가 | `ATR`, `bps`, 최근 변동성 분위수로 재정의 |
| BE +1.5포인트 | 왕복 수수료 + 예상 슬리피지 + 안전 버퍼로 재계산 |
| 몇 계약 Big Trade | 명목 거래대금·세션 내 백분위·동적 사건 수로 변환 |
| Footprint의 0 | 체결 밀도가 높은 시장에서는 하위 5% near-zero로 변환 |
| 미국 개장 | 시장별 유동성 세션으로 대체 |
| 국가·섹터 | 가상화폐에서는 시장 레짐·카테고리 상대강도로 대체 |

### 14.3 한국시장 이식

```yaml
korea_v5:
  engines: [daily_swing, hlit]           # cyborg 제외
  hlit_timeframes: [1d, 1h, 15m, 5m, 1m]  # 30s/5s 제외
  universe:
    futures: KOSPI200
    equities: "KOSPI 대형주 + 거래대금 상위"
    exclude: [소형주, 관리종목, 저유동성]
  sector_source: KRX_업종분류
  normalization:
    be_offset: "1~2틱 (=12,500~25,000원)"
    add_threshold: "0.35 × ATR(14, 5m)"
  filters:
    exclude_vi_periods: true
    exclude_single_price_auction: true
  news:
    substitute: [FOMC, 미국고용지표, 한은금통위, 옵션만기일]
```

---

## 15. 확정 사실 최종 목록 (A0)

### 15.1 v5.0 신규 확정

```text
✅ HLIT 앵커: 두 극점 사이의 절대 반대극점 → 두 번째 극점
✅ 피보나치 레벨 25% / 50% / 66%, 목표 66% (다우 이론 2/3)
✅ 다이버전스가 작도의 전제조건 — 없으면 그리지도 매매하지도 않음
✅ Método Trullás 이평: SMA 6 / 70 / 200
✅ MACD 12/26/9, 일봉·인트라데이 동일
✅ MACD 0선 위 교차 = 매수 / 0선 아래는 비관 극단에서만
✅ 국가 → 섹터(상위 3개만) → 종목 순 필터
✅ 약한 섹터: 부동산·금융·에너지
✅ 소진 = 새 극점 갱신 + 계단식 거래량 감소
✅ 존 도달만으로 진입 금지, BUY LIMIT 대기 금지
✅ 존 폭은 과거 가격 행동에서 도출, 고정 배수 아님
✅ 5분 차트 lookback은 "필요한 만큼", 한 달은 터무니없음
✅ 정규 다이버전스 선호, 히든도 사용
✅ 잠정 피벗을 폐기하지 않고 새 셋업으로 갱신
✅ +30pt 도달 시 2번째 계약 추가 → 가중평균 BE 손절
✅ BE 손절은 진입가 +1.5포인트 (수수료 + 슬리피지 흡수)
✅ BE 이동 트리거: 이익 100~200 확보 시 또는 위험 구역
✅ Big Trades와 맞서지 않음 — 회피·청산 신호
✅ 거래 횟수 고정 규칙 거부. 17분 5거래 / 하루 9거래 실례
✅ 개장 22~38초 후 진입 실제로 함. "15분 대기"론 반박
✅ 프리마켓 진입 가능, 단 마이크로 소량
✅ 프리마켓 볼륨은 프리마켓 내 두 구간끼리 비교
✅ 개장 1시간~1시간15분 전 도착, 생산적 작업은 1시간
✅ 뉴스: 2성·3성 모두 회피. 지속시간 유형별 상이
✅ NFP(첫 금요일 14:30)는 세션 전체 영향
✅ 뉴스 메커니즘: 역방향 스윕 → 본래 방향
✅ 브로커 IB 선호, MT5 비판, 규제 시장만
✅ NQ ×$20 / MNQ ×$2, 증거금 $2,000~$30,000+
✅ 스윙이 승률 높고, 데이트레이딩이 수익 많음
✅ 목표 미달 조기 청산을 스스로 인정 (1500% 사례, 89pt 남김)
✅ 청산 시 "시장에 몇 포인트 선물" — 정확한 가격과 싸우지 않음
✅ 역대 TOP10에 3개 기록 보유 (2위 2222%, 6위 930%, 9위)
✅ 1985년 Ralph Casadone 1283%를 41년 만에 경신
✅ 2026 Q3(7·8월) 대회 불참 선언, 18개월 연속 소진
```

### 15.2 직접 공개되지 않았지만 v6.0에서 가설화한 항목

| 항목 | 직접 공개 상태 | v6.0 최유력 가설 | 신뢰도 | 실전 권한 |
|---|---|---|---:|---|
| Big Trades 임계값 | 미공개 | Cumulative Trades + Auto Filter, 강한 필터 후보 | MEDIUM | `score_only` |
| MIG 캔들 | 미공개 | Big Trade 방향의 노력 대비 가격 결과 실패/흡수 캔들 | MEDIUM | `score_only` |
| Ceros osmóticos | 미공개 | 0 또는 near-zero Bid/Ask 셀·연속 불균형 | LOW | `telemetry_only` |
| Secados | 수치 미공개 | 극단 Delta + 가격 진행 실패 + 공격량 감소 + 회수 | MEDIUM-HIGH | `score_only` |
| GOLD | 용도 미공개 | 61.8% 되돌림 / 161.8% 확장 | HIGH(값), MEDIUM(용도) | 분석 레벨 |
| 초기 손절 거리 | 공식 산식 미공개 | 최종 소진/MIG 극점 바깥 + 적응형 버퍼 | MEDIUM | 독립 검증 후 |
| 실제 거래당 위험률 | 미공개 | 일반계좌와 대회계좌를 분리해야 함 | LOW | 사용 금지 |
| 대회 최대낙폭 | 미공개 | 공개 자료로 계산 불가, 시나리오만 가능 | LOW | 성과 주장 금지 |
| 승률 실측치 | 미공개 | BE 비중이 큰 분포로 추정 | MEDIUM | 성과 주장 금지 |
| Cyborg 큰 구간 | 최종 결정식 미공개 | 상위 프레임+확장 클러스터+Order Flow+3R 공간 | MEDIUM | `score_only` |
| 25%/50% 용도 | 미공개 | 25%=첫 반응, 50%=균형·관리 | MEDIUM | 주문 없음 |

> **진입 로직의 필수 골격은 A0로 복원되어 있다.** 그러나 Cyborg·Order Flow 세부값과 성과 기대치는 여전히 복원되지 않았다. v6.0은 이 부분을 “공식값”이 아니라 검증 가능한 연구 가설로 바꾼 버전이다.

---

## 16. 비판·한계 (v6.0)

### 16.1 방법론 자체

- **다이버전스 + 66% 되돌림**은 고전 기법이며, 학술적으로 비용 차감 후 유의성이 확립되지 않았다. 그의 성과가 이 기법 자체의 통계적 우위인지, **기법 + 재량 + 레버리지 + 생존**의 결합인지 분리 불가능하다.
- **"66%가 안 나오면 새 셋업"** 구조는 사후적으로 항상 설명 가능한 형태다. 백테스트에서 **무효화 조건을 엄격히 정의**하지 않으면 곡선맞춤이 된다.
- 그가 시연한 8개 사례는 전부 **성공 사례**다. 실패 사례의 빈도는 제시되지 않았다.

### 16.2 그의 실제 손익 구조가 시사하는 것

CASE C(17분 5거래)를 보면 **5거래 중 4거래가 브레이크이븐 언저리, 1거래가 수익**이다. CASE D도 미니 2건 중 1건 손실 후 회복이다.

**(C) 이는 승률이 아니라 손실 최소화가 엔진임을 시사한다.** BE +1.5pt 규칙과 Big Trades 회피가 그 장치다. 자동화 시 **"이기는 거래를 늘리는 것"보다 "지는 거래를 BE로 만드는 것"에 최적화 노력을 집중**해야 한다.

### 16.3 상업 구조 (B)

마스터클래스는 **판매 세미나**다. Método Trullás + HLIT = €2,900(분할) / €2,498(일시), Cyborg = €5,000, 전체 48시간 특가 €5,400 / €4,998. 시연 사례가 선별적일 유인이 존재한다.

다만 **실계좌 화면과 WCTC 순위표를 즉시 대조 가능한 형태로 공개**하고("내일 1570.4%가 떠야 합니다"), 손실 거래도 보여준다는 점은 통상적 판매 세미나보다 검증 가능성이 높다.

### 16.4 반복되는 근본 경고

> 5회 우승자 본인이 ① 대회 수익률은 **레버리지 산물**이며 실전 포트폴리오에 적용 불가, ② **대회 참가를 권하지 않으며**, ③ **알고 자동화를 시도해 실패**했고(33,500조 조합·과최적화), ④ **자기 방법을 자동화하지 않는다**고 말했다.
>
> 그리고 ⑤ **18개월 연속 경쟁 후 소진되어 2026년 7·8월 대회를 쉰다**고 선언했다.

---

## 17. 실행 로드맵 v6.0

```text
PHASE 1 (2~3주) — HLIT 코어 단독 검증
  · 정규 다이버전스 탐지 (MACD 12/26/9)
  · 앵커 규칙 구현 (절대 반대극점 → 두 번째 극점)
  · 25/50/66% 작도, 66% 목표
  · NQ 5분봉 3년치, look-ahead 차단
  · ⭐ 판단 지점: 여기서 기대값이 안 나오면 나머지를 붙여도 안 나온다

PHASE 2 (2주) — 소진 + 존
  · 거래량 계단식 감소 필터
  · 사전 존 구축 (적응형 폭)
  · 상위 프레임(일/1H) 거부권

PHASE 3 (1주) — 리스크 레이어
  · 구조적 손절 (0.40~1.50 ATR)
  · BE +1.5pt 이동
  · +30pt 추가 + 가중평균 BE
  · 일일/주간 가드, 심리 상태머신

PHASE 4 (1주) — 필터
  · 뉴스 2·3성 차단
  · Big Trades 회피 (임계값은 파라미터)
  · 세션 창

PHASE 5 (선택, 4주+) — Cyborg
  · 틱 데이터 확보 후 풋프린트/Delta
  · 5초 스케일 (슬리피지 검증 필수)

PHASE 6 — 한국 트랙
  · Método Trullás 일봉을 KRX에 이식 (기존 스크리닝과 결합)
  · HLIT를 KOSPI200 선물 5분/1분에 이식
  · VI·단일가 배제 필터

PHASE 7 — Binance 트랙
  · BTCUSDT 단일 종목, HLIT A0 코어만
  · aggTrade 기반 5초/30초 봉·Delta·CVD 생성
  · Big Trades/MIG/Secado는 telemetry_only
  · 8주 섀도 후 유효성 확인 시 score_only 승격
  · Cyborg가 주문을 직접 생성하는 단계는 마지막

승격 게이트: OOS 기대값>0.15R, PF≥1.15, 셋업별 표본≥50,
             리페인트 테스트 통과, 자유 파라미터≤8,
             MNQ 섀도 4주 → 모의 4주 → 실거래 최소수량
```

### 한 문단 요약 (v6.0)

> **하나의 유동성 높은 시장을 정한다. 일봉에서는 강한 국가·상위 3개 섹터·강한 종목만 남기고 SMA 6/70/200과 MACD로 스윙을 잡는다. 인트라데이에서는 가격이 극점을 갱신하는데 MACD가 반대로 가는 비대칭을 먼저 찾는다. 그 비대칭이 없으면 피보나치를 그리지도 않는다. 있으면 두 극점 사이의 절대 반대극점에서 두 번째 극점까지 피보나치를 긋고 25·50·66%를 표시한다. 목표는 다우 이론의 2/3인 66%다. 존에 도달했다고 사지 않는다. 새 저점이 나오는데 거래량이 계단처럼 줄어드는 소진을 기다린다. 진입 후 100~200 이익이 나거나 위험해지면 손절을 진입가+1.5포인트로 올린다. 가격이 30포인트 유리해지면 계약을 하나 더 얹고 손절은 가중평균 브레이크이븐으로 옮긴다. 앞을 막는 대형 체결과는 싸우지 않고 나온다. 정확한 목표가와도 싸우지 않고 몇 포인트를 시장에 선물한다. 하루 몇 번 매매할지는 정하지 않는다. 목표가 채워지면 화면을 닫는다.**

---


## 18. v6.0 미공개 11개 항목의 통합 판정

### 18.1 한눈에 보는 최종 결론

| 항목 | 가장 가능성 높은 복원값 | 근거 형태 | 신뢰도 |
|---|---|---|---:|
| Big Trades | `Cumulative Trades + Auto Filter` 계열, 강도 Strong/Medium 후보 | A1+V1 | MEDIUM |
| NQ 수동 임계값 | 개별 25~60계약, 누적 60~120계약, 극대형 120~250계약을 **연구 그리드**로 사용 | C/V1 | LOW |
| MIG | 대형 공격 체결과 가격 결과가 불일치하는 흡수·반전 캔들, 또는 동일 방향으로 종가가 유지되는 기관 연속 캔들 | A1+V1 | MEDIUM |
| Ceros osmóticos | Footprint 한쪽의 0/near-zero 셀과 반대쪽 강한 체결의 연속 | A1+V1 | LOW |
| Secados | 공격적 Delta/CVD가 극단인데 가격 진행 실패 → 다음 공격량 감소 → 반대 방향 회수 | A1+C | MEDIUM-HIGH |
| GOLD | `0.618033...` 되돌림 및 `1.618033...` 확장 | A1+V1 | HIGH(값) |
| 초기 손절 | 최종 소진 극점 또는 MIG 극점 바깥 + 적응형 버퍼 | A0+C/V1 | MEDIUM |
| 실제 위험률 | 일반 교육 원칙과 대회 레버리지 운용을 분리해야 하며 정확값은 불명 | A0/C | LOW |
| 대회 MDD | 공식 Equity Curve 없으므로 계산 불가; 10~45%의 시나리오 범위 외 단정 금지 | C | LOW |
| 승률 | 의미 있는 승리 20~30%, BE·미세수익 45~60%, 손실 15~25% 가설 | A0 사례+C | MEDIUM |
| Cyborg 큰 구간 | 상위 프레임·확장 클러스터·Profile·Delta/CVD·Secado/MIG·Big Trade 경로 + 최소 3R | A1+C/V1 | MEDIUM |
| 25%/50% | 25%=첫 반응·유효성, 50%=균형·관리, 66%=최종 목표 | A0+C/V1 | MEDIUM |

### 18.2 실전 권한 등급

```yaml
permission_levels:
  fixed_core:
    description: "A0 규칙. 최적화 금지 또는 매우 제한"
    examples: [macd_12_26_9, fib_025_050_066, target_066, divergence_required]

  execution_candidate:
    description: "C/V1이지만 체결·리스크 구현에 필요. 독립 검증 후 사용"
    examples: [structural_stop_buffer, crypto_be_offset, add_trigger_normalization]

  score_only:
    description: "단독 주문 금지. 기존 A0 셋업의 점수 또는 거부권으로만 사용"
    examples: [big_trades, mig, secado, cyborg_score]

  telemetry_only:
    description: "기록만 하고 의사결정에 사용하지 않음"
    examples: [osmotic_zero, experimental_footprint_patterns]
```

### 18.3 추정을 다루는 규율

1. **가설 이름에 출처를 넣는다**: `V1_MIG_REVERSAL`, `V1_SECADO_TRAPPED_LONGS`처럼 저장한다.
2. **David 공식값이라는 이름을 금지한다**: `david_big_trade_threshold=80` 같은 변수명을 쓰지 않는다.
3. **대안 가설을 같이 보존한다**: MIG 반전형과 연속형, Ceros의 세 가설을 병렬로 테스트한다.
4. **성과가 좋다는 이유로 A0를 바꾸지 않는다**: 66%를 61.8%로 바꾸거나 다이버전스를 제거하면 별도 전략이다.
5. **Walk-forward에서만 승격한다**: 학습 구간 성과만으로 `telemetry_only → score_only → execution` 승격 금지.

---

## 19. Order Flow 화면 역공학 — Big Trades·MIG·Ceros·Secados·GOLD

### 19.1 Big Trades

#### A1로 확인되는 플랫폼 구조

ATAS의 공식 설명상 Big Trades는 단일 대형 체결 또는 유사 체결의 누적 그룹을 표시한다.

```text
Calculation Mode
├─ Cumulative Trades  → Auto Filter 사용 가능
└─ Separate Trades    → Auto Filter 사용 불가

Auto Filter Intensity
├─ Strong  → 더 적고 더 큰 이벤트
├─ Medium  → 균형
└─ Weak    → 더 많은 이벤트
```

그 밖에 `Min Volume`, `Max Volume`, `Price Location`, `Execution Price`, `Fixed Sizes`, 매수·매도·중간 체결 색상 등이 존재한다. 즉 화면의 노란색·빨간색은 **크기 등급일 수도 있지만 매수/매도 방향색일 가능성이 더 높다.** 색상만으로 임계값을 역산하면 안 된다.

#### V1 최유력 설정

```yaml
big_trades_v1:
  calculation_mode: CumulativeTrades
  auto_filter: true
  auto_filter_intensity_candidates: [Strong, Medium]
  price_location: Any
  use_as_entry_signal: false
  roles:
    - blocking_obstacle
    - exit_warning
    - institutional_activity_marker
  confidence: MEDIUM
```

David가 Big Trades를 드문 장애물처럼 보고 “맞서 싸우지 않는다”고 말한 점은 `Weak`보다 `Strong/Medium` 후보를 지지한다. 그러나 영상에서 설정 창이 직접 확인되지 않았으므로 확정할 수 없다.

#### 고정 계약 수를 써야 할 때의 연구 그리드

아래는 David의 값이 아니라 NQ에서 시도할 **민감도 분석 범위**다.

```yaml
nq_big_trade_manual_grid:
  separate_trade_contracts: [25, 40, 60]
  cumulative_trade_contracts: [60, 80, 120]
  extreme_cumulative_contracts: [120, 180, 250]
  aggregation_window_ms: [100, 150, 250]
  max_price_distance_ticks: [0, 1, 2]
```

고정값 하나를 선택하는 대신 “RTH 한 세션당 의미 있는 마커가 몇 개 나오는가”를 기준으로 조정하는 것이 낫다.

```text
너무 많음 → 임계값 상승 또는 Strong
너무 적음 → 임계값 하락 또는 Medium
목표 → 모든 잡음을 표시하지 않고 실제로 경로를 막는 소수의 이벤트만 남김
```

#### Big Trade의 방향적 해석

| 위치 | 해석 | 전략 행동 |
|---|---|---|
| 롱 진행 방향 바로 위 | 매수자가 흡수될 수 있는 장애물 | 신규 롱 감점·일부 청산 |
| 롱 진입 아래 | 지지성 대형 체결 후보 | 보조 점수, 단독 진입 금지 |
| 숏 진행 방향 바로 아래 | 매도자가 흡수될 수 있는 장애물 | 신규 숏 감점·일부 청산 |
| 가격이 마커를 통과한 뒤 유지 | 해당 대형 체결이 흡수되었을 가능성 | 다음 목표 공간 재평가 |

---

### 19.2 MIG 캔들

#### 확인되는 역할

공식 과정에서 `Big Trades y velas MIG`가 바로 `Identificar movimientos institucionales` 앞에 놓인다. 따라서 MIG는 **기관 움직임을 판별하기 위한 캔들 유형**이라는 역할까지는 강하게 지지된다. 약자의 정확한 원문은 공개되지 않았다.

#### 가설 A — 기관 흡수·반전 MIG

> 대형 공격 체결이 발생했지만 가격이 그 방향으로 진행하지 못하고 반대 방향으로 마감하는 캔들.

##### 강세 MIG 후보

```python
bullish_mig = (
    big_sell_trade_near_low
    and delta_is_extreme_negative
    and downside_progress <= 0.15 * atr_30s
    and lower_wick_ratio >= 0.35
    and close_location_value >= 0.65
    and next_bar_breaks_trigger_high
)
```

##### 약세 MIG 후보

```python
bearish_mig = (
    big_buy_trade_near_high
    and delta_is_extreme_positive
    and upside_progress <= 0.15 * atr_30s
    and upper_wick_ratio >= 0.35
    and close_location_value <= 0.35
    and next_bar_breaks_trigger_low
)
```

여기서:

```python
close_location_value = (close - low) / max(high - low, tick_size)
```

#### 가설 B — 기관 연속 MIG

반전형만 존재한다고 단정하면 위험하다. Big Trade 방향으로 강한 종가가 형성되고 다음 봉에서도 50% 이상 되돌리지 않는다면 기관 연속 캔들일 수 있다.

```python
continuation_mig = (
    big_trade_direction == candle_direction
    and body_ratio >= 0.60
    and close_near_directional_extreme <= 0.15
    and next_bar_retracement <= 0.50 * trigger_range
)
```

#### 권장 구현

```yaml
mig:
  enabled_for_orders: false
  modes:
    reversal: V1_MIG_REVERSAL
    continuation: V1_MIG_CONTINUATION
  confirmation_bars: [1, 2]
  delta_extreme_quantiles: [0.80, 0.90, 0.95]
  max_progress_atr: [0.10, 0.15, 0.25]
  wick_ratio_min: [0.30, 0.35, 0.45]
```

---

### 19.3 Ceros osmóticos

#### 공식적으로 확인되는 것

- David 공식 과정에 실제 명칭이 존재한다.
- `Ceros osmóticos en NQ y MNQ`로 상품이 특정된다.
- Delta/CVD와 Big Trades/MIG 사이에 배치된 Order Flow 개념이다.

#### 가설 순위

| 가설 | 설명 | 확률 평가 |
|---|---|---:|
| H1 | Bid×Ask Footprint에서 한쪽이 `0` 또는 거의 0이고 반대쪽 체결만 연속되는 셀 | 가장 유력 |
| H2 | 고점·저점의 finished/unfinished auction을 독자적으로 부르는 표현 | 대안 |
| H3 | 같은 시장을 나타내는 NQ와 MNQ 사이의 0 셀·체결 불일치 | 보조 대안 |

ATAS 공식 Footprint는 0 값을 포함한 Bid/Ask Imbalance 비교를 지원하고, `Ignore Zero Values` 옵션도 제공한다. 따라서 H1은 플랫폼 기능과 용어가 가장 직접적으로 맞는다.

#### V1 판정식

```python
osmotic_zero = (
    one_side_volume <= zero_or_near_zero_threshold
    and opposite_side_volume >= rolling_quantile(opposite_side_volume, 0.80)
    and diagonal_imbalance_ratio >= 4.0
    and consecutive_levels >= 2
    and distance_from_candle_extreme <= 0.30 * candle_range
)
```

NQ/MNQ 연구값:

```yaml
osmotic_zero_nq_v1:
  literal_zero_threshold_contracts: [0, 1]
  imbalance_ratio_min: [3.0, 4.0, 5.0]
  stacked_levels_min: [2, 3]
  location_outer_pct: [0.20, 0.30]
```

거래량이 매우 많은 암호화폐에서는 문자 그대로 0이 거의 없으므로 하위 분위수로 바꾼다.

```python
near_zero = side_volume <= rolling_quantile(side_volume, 0.05)
```

> **권한**: `telemetry_only`. 이 가설 하나만으로 주문을 내면 안 된다.

---

### 19.4 Secados

#### Agotamiento와 구분

| 개념 | 데이터 | 공개 의미 |
|---|---|---|
| **Agotamiento** | 가격 극점 + 구간별 거래량 | 새 극점이 나오지만 거래량이 계단식 감소 |
| **Secado** | Delta·CVD·Footprint·가격 결과 | 공격적 참여자가 갇히고 추가 공격이 마르는 시장 함정 |

공식 과정은 Secados를 `operaciones atrapadas (largos y cortos)`라고 설명한다. 따라서 Secado는 단순 저거래량이 아니라 **강한 공격에도 가격이 진행하지 못한 뒤 공격 주체가 갇히는 구조**로 보는 것이 가장 타당하다.

#### 갇힌 롱 → 숏 Secado

```python
trapped_longs = (
    delta_leg1 >= q80_positive_delta
    and cvd_makes_new_high
    and upside_price_progress <= max(2 * tick_size, 0.15 * atr_30s)
    and abs(delta_leg2) <= 0.65 * abs(delta_leg1)
    and volume_leg2 <= 0.75 * volume_leg1
    and price_reclaims_below(event_poc_or_midpoint, within_bars=3)
)
```

#### 갇힌 숏 → 롱 Secado

```python
trapped_shorts = (
    delta_leg1 <= q20_negative_delta
    and cvd_makes_new_low
    and downside_price_progress <= max(2 * tick_size, 0.15 * atr_30s)
    and abs(delta_leg2) <= 0.65 * abs(delta_leg1)
    and volume_leg2 <= 0.75 * volume_leg1
    and price_reclaims_above(event_poc_or_midpoint, within_bars=3)
)
```

#### 초기 실험값

```yaml
secado_v1:
  delta_extreme_quantile: [0.80, 0.90]
  max_price_progress_atr: [0.10, 0.15, 0.25]
  second_leg_delta_ratio_max: [0.50, 0.65, 0.80]
  second_leg_volume_ratio_max: [0.60, 0.75, 0.90]
  reclaim_bars: [1, 2, 3]
  reclaim_reference: [event_poc, candle_midpoint, cluster_edge]
```

Secado는 V1 항목 중 자동화 가능성이 가장 높지만, 단독 진입 신호가 아니라 **HLIT·존·상위 프레임과 합류할 때만 점수**를 주는 것이 맞다.

---

### 19.5 GOLD 비율

홍보 화면에는 다음 표준 레벨이 직접 보인다.

```text
0.00 / 23.60 / 38.20 / 50.00 / 61.80 / 76.40 / 89.00 / 100.00%
```

수학적으로:

\[
\varphi = \frac{1+\sqrt{5}}{2} = 1.6180339887\ldots
\]

\[
\varphi^{-1} = 0.6180339887\ldots
\]

따라서 가장 유력한 해석은 다음이다.

```yaml
gold_ratio_v1:
  retracement: 0.6180339887
  extension: 1.6180339887
  confidence_value: HIGH
  confidence_exact_david_usage: MEDIUM
```

#### 66%와의 관계

| 도구 | 레벨 | 역할 |
|---|---:|---|
| 표준 피보나치 템플릿 | 61.8% | GOLD 되돌림 후보 |
| 표준 확장 | 161.8% | GOLD 확장 후보 |
| HLIT 전용 템플릿 | 25% / 50% / **66%** | 다우 이론 2/3 목표 |

```text
GOLD 61.8% ≠ HLIT 66%
```

두 레벨이 서로 가까워도 코드에서는 별도 상수로 관리해야 한다.

---

## 20. 손절·리스크·성과 역산

### 20.1 초기 손절 — 확인 사실과 연구식

#### A0로 확인되는 원칙

- 진입가에서 고정 퍼센트만큼 떨어진 곳이 아니라 **구조적 무효화 지점 바깥**이다.
- 너무 타이트한 손절은 시장의 흔들기에 제거될 수 있다.
- 손절을 넓히지 않는다.
- 보호 주문은 체결 가능성이 우선이며 STOP-LIMIT보다 STOP-MARKET 철학에 가깝다.

#### V1에서 가장 유력한 구조 기준

```text
롱: 최종 소진 다리의 저점 또는 MIG 트리거 캔들 저점 중 더 낮은 값
숏: 최종 소진 다리의 고점 또는 MIG 트리거 캔들 고점 중 더 높은 값
```

#### 자동화 연구식

```python
adaptive_buffer = max(
    buffer_ticks * tick_size,
    spread_multiplier * current_spread,
    atr_multiplier * atr_30s,
)

stop_long  = structural_low  - adaptive_buffer
stop_short = structural_high + adaptive_buffer
```

권장 그리드:

```yaml
structural_stop_v1:
  buffer_ticks: [2, 4, 6]
  spread_multiplier: [1.5, 2.0, 3.0]
  atr_multiplier_30s: [0.05, 0.10, 0.20]
  total_distance_filter_atr: [0.40, 0.60, 0.90, 1.20, 1.50]
```

`0.40~1.50 ATR`은 David의 공개 산식이 아니라 지나치게 좁거나 넓은 거래를 제거하기 위한 연구 경계다.

#### 화면 손실로부터의 조건부 역산

마스터클래스 사례에는 NQ 거래에서 약 `-$500` 손실이 나온다. NQ가 1포인트당 계약당 $20이므로:

| 가정 수량 | 수수료 전 손절 거리 |
|---|---:|
| 1 NQ | 약 25포인트 |
| 2 NQ | 약 12.5포인트 |

실제 수량·평균 체결가·수수료가 완전히 확인되지 않았으므로 **12.5~25포인트는 범위 가설일 뿐 손절 공식이 아니다.**

---

### 20.2 BE 이동

A0 규칙:

```text
NQ에서 진입가 또는 가중평균 진입가보다 1.5포인트 유리한 곳
목적 = 왕복 수수료 + 불리한 슬리피지 흡수
이익 100~200달러 확보 또는 위험 구역 진입 시 이동
```

다른 시장에서는 다음으로 바꾼다.

```python
be_offset_price = ceil_to_tick(
    expected_entry_fee
    + expected_exit_fee
    + expected_exit_slippage
    + safety_buffer
)
```

수수료 구조가 바뀌면 offset도 자동으로 다시 계산되어야 한다.

---

### 20.3 실제 거래당 위험률

#### 공개 자료가 말해주는 것

- David는 구조적 손절을 먼저 정하고 계약 수로 손실금액을 조절한다.
- 대회 수익률은 강한 레버리지의 결과이며 일반 포트폴리오에 복사하면 안 된다고 스스로 경고한다.
- WCTC 분기 Futures의 최소 시작 잔액은 $2,500이지만, David가 정확히 얼마로 시작했는지는 공개되지 않았다.

#### 위험률 시나리오

`-$500` 손실이 다음 계좌에서 차지하는 비중은:

| 계좌자산 가정 | 손실률 |
|---|---:|
| $2,500 | 20% |
| $5,000 | 10% |
| $10,000 | 5% |
| $25,000 | 2% |

따라서 대회 초기 위험률은 계좌 시작금 가정에 따라 2~20%까지 달라진다. **시작금과 체결 로그 없이 “David는 거래당 15%를 쓴다”고 단정할 수 없다.**

#### 구현 권고

```yaml
risk_profiles:
  david_championship_estimate:
    enabled: false
    range_scenarios: [0.02, 0.05, 0.10, 0.20]
    purpose: forensic_analysis_only

  binance_research:
    normal: 0.0025
    a_grade: 0.0050
    absolute_max: 0.0075
    note: "David 공식값이 아닌 시스템 안전 후보"
```

---

### 20.4 대회 최대낙폭

공식 성적표에 일별 NAV·일중 Equity·개별 거래가 없으면 MDD를 계산할 수 없다.

\[
MDD = \max_t \left(\frac{Peak_t - Trough_t}{Peak_t}\right)
\]

이 식을 적용하려면 시간순 Equity Curve가 필요하다. 최종 수익률 하나만으로는 불가능하다.

#### 조건부 시나리오

두 번 연속 동일 비율 `r`을 잃으면:

\[
DD_2 = 1-(1-r)^2
\]

| 거래당 위험 | 2연속 완전 손절 |
|---:|---:|
| 5% | 9.75% |
| 10% | 19.0% |
| 15% | 27.75% |
| 20% | 36.0% |

따라서 공개 사례와 공격적 대회 운용을 전제로 한 **넓은 시나리오 범위**는 다음 정도다.

```yaml
championship_mdd_scenario:
  conservative: [0.10, 0.20]
  central_hypothesis: [0.20, 0.35]
  aggressive_tail: [0.35, 0.45]
  official_value: null
```

이 값은 성과 지표로 인용하면 안 된다.

---

### 20.5 승률과 손익 분포

17분 5거래 사례는 다음을 보여준다.

```text
여러 거래 → +1.5pt 부근의 BE·미세수익
일부 거래 → 명확한 손실
소수 거래 → 세션 전체 수익을 만드는 큰 승리
```

따라서 “브로커상 수익 거래”와 “의미 있는 승리”를 구분해야 한다.

```yaml
outcome_distribution_v1:
  meaningful_win_ge_1r: [0.20, 0.30]
  scratch_or_small_profit: [0.45, 0.60]
  clear_loss: [0.15, 0.25]
  broker_positive_trade_rate: [0.55, 0.70]
  non_loss_rate_including_scratch: [0.75, 0.85]
```

이 분포의 핵심은 높은 적중률이 아니라 **손실 거래를 BE에 가깝게 바꾸고, 소수의 큰 거래를 남기는 것**이다.

백테스트 보고서에는 최소한 다음을 따로 보여줘야 한다.

```text
Win rate excluding scratches
Scratch rate
Loss rate
Average win / average loss
Profit factor
Expectancy in R
MFE / MAE
BE 이동 전후 성과 차이
```

---

## 21. Cyborg의 큰 구간 판정과 25·50·66 레벨

### 21.1 공식적으로 확인되는 Cyborg 구성요소

```text
Fibonacci 확장과 확장 클러스터
재진입·두 번째 기회
Market Profile: POC / VAH / VAL
캔들 내부 체결
주문 교차
Delta / 누적 Delta
Secados / 갇힌 롱·숏
Ceros osmóticos
Big Trades / MIG 캔들
기관 움직임 식별
HLIT와의 결합
```

공식 소개는 Cyborg가 시장 전환과 대형 기관 포지션을 찾고 **3R 초과** 손익비를 노린다고 설명한다.

### 21.2 V1 필수조건

```python
mandatory = (
    higher_timeframe_bias_aligned
    and target_space_r >= 3.0
    and (secado_confirmed or mig_confirmed)
    and no_blocking_big_trade_before_1_5r
)
```

### 21.3 연구용 점수표

| 요소 | 점수 |
|---|---:|
| 상위 프레임 방향 일치 | +2 |
| 정규 HLIT 다이버전스 | +2 |
| 히든 다이버전스 | +1 |
| 피보나치 확장 클러스터 | +2 |
| POC·VAH·VAL 합류 | +1 |
| Secado | +2 |
| Cero osmótico | +1 |
| 반전 MIG | +2 |
| 진행 방향 뒤쪽의 지지성 Big Trade | +1 |
| 진행 방향 앞의 방해 Big Trade | -4 |
| 고영향 뉴스 위험 | -3 |
| 스프레드·슬리피지 비정상 | -2 |

```python
cyborg_large_move_candidate = mandatory and score >= 7
cyborg_high_confidence = mandatory and score >= 9
```

이 점수는 David의 직접식이 아니다. 각 구성요소가 실제로 어떤 증분 효과를 내는지 Ablation으로 검증하기 위한 연구 프레임이다.

### 21.4 목표 선택

```yaml
cyborg_target_v1:
  minimum_expected_r: 3.0
  normal_target: first_extension_cluster
  high_confidence_target:
    candidates: [1.272_extension, 1.618_extension, next_profile_cluster]
  early_exit:
    - blocking_big_trade
    - secado_against_position
    - failed_mig_follow_through
```

### 21.5 25%·50%·66%의 역할

#### 25% — 첫 반응 검증

```text
25% 도달 전 극점 재갱신 → 반전 힘 부족, 기존 셋업 갱신 검토
25% 도달·유지 → 조정이 실제로 시작됐다는 첫 증거
```

```python
if reaches(fib_25):
    setup.progress = FIRST_REACTION_CONFIRMED
```

기본 주문 행동은 없다.

#### 50% — 균형·관리 구간

50%는 앵커 범위의 중간값이다.

```text
50% 이전 → 아직 원래 방향의 단순 반등/조정일 수 있음
50% 회수·안착 → 반전 측이 범위 절반을 되찾음
```

연구 대상:

- 부분청산
- 손절을 더 최근의 구조로 끌어올림
- Big Trade·Profile과 66% 사이 공간 재평가
- 66% 도달 확률의 조건부 변화

단, 50% 도달을 자동 BE 트리거로 고정할 직접 근거는 없다.

#### 66% — A0 최종 목표

```python
long_target  = anchor_low  + (anchor_high - anchor_low) * 0.66
short_target = anchor_high - (anchor_high - anchor_low) * 0.66
```

### 21.6 레벨별 권한

```yaml
hlit_level_roles_v1:
  fib_025:
    role: first_reaction_validation
    order_action: none
  fib_050:
    role: equilibrium_and_management_research
    order_action: disabled
  fib_066:
    role: primary_target
    order_action: exit_remaining_position
```

---

## 22. 자동매매 통합 명세와 Binance 이식

### 22.1 전략 상태 머신

```text
SCANNING
  ↓
DIVERGENCE_FOUND
  ↓
ANCHORS_CREATED
  ↓
FIB_LEVELS_READY
  ↓
ZONE_ARMED
  ↓
EXHAUSTION_CONFIRMED
  ↓
ORDERFLOW_SCORE_READY        # V1, 초기에는 기록만
  ↓
RISK_APPROVED
  ↓
ENTRY_SUBMITTED
  ↓
PARTIALLY_FILLED / OPEN
  ↓
PROTECTIVE_STOP_ACTIVE
  ↓
ADD_ELIGIBLE
  ↓
POSITION_ADDED
  ↓
WEIGHTED_BE_PROTECTED
  ↓
EXITING
  ↓
CLOSED
```

종료·무효 상태:

```text
INVALIDATED_BY_NEW_EXTREME
REPLACED_BY_NEW_SETUP
RISK_REJECTED
NEWS_BLOCKED
SPREAD_BLOCKED
DATA_GAP_BLOCKED
PROTECTION_FAILURE
EMERGENCY_CLOSED
```

### 22.2 불변식

```python
INVARIANTS = [
    "다이버전스 없이 피보나치 생성 금지",
    "존 도달만으로 진입 금지",
    "손실 중 추가 진입 금지",
    "손절 확대 금지",
    "추가 진입 후 총 위험 증가 금지",
    "포지션이 있으면 보호 손절 필수",
    "Big Trade/MIG/Ceros/Secado 추정 모듈은 초기 단독 주문 금지",
    "미완성 봉과 미래 봉 사용 금지",
]
```

### 22.3 핵심 셋업 데이터

```yaml
setup_record:
  identity:
    setup_id: uuid
    symbol: str
    direction: [long, short]
    strategy_version: trullas_v6

  divergence:
    type: [regular, hidden]
    pivot1_price: decimal
    pivot2_price: decimal
    pivot1_macd: decimal
    pivot2_macd: decimal

  anchors:
    anchor_a: decimal
    anchor_b: decimal
    provisional: bool
    invalidation_price: decimal

  levels:
    fib_025: decimal
    fib_050: decimal
    fib_066: decimal
    gold_0618: decimal|null
    extension_1272: decimal|null
    extension_1618: decimal|null

  confirmation:
    exhaustion_legs: int
    leg_volumes: list
    zone_id: uuid|null
    big_trade_score: float|null
    mig_type: str|null
    secado_type: str|null
    osmotic_zero_count: int|null

  risk:
    structural_stop: decimal
    estimated_slippage: decimal
    risk_amount: decimal
    quantity: decimal
```

### 22.4 Binance에서의 데이터 매핑

| David/NQ 개념 | Binance 근사 데이터 |
|---|---|
| 체결 흐름 | Aggregate Trade 또는 원시 Trade 스트림 |
| 공격적 매수·매도 | buyer-maker 플래그를 이용한 taker 방향 분류 |
| 5초·30초 캔들 | 체결 스트림을 로컬 집계 |
| Delta | 공격적 매수 명목금액 - 공격적 매도 명목금액 |
| CVD | Delta 누적합 |
| Footprint | 가격 틱/버킷별 Bid×Ask 체결 집계 |
| Big Trades | 동일 방향·짧은 시간·가까운 가격의 체결 묶음과 명목금액 백분위 |
| Market Profile | 세션별 가격 버킷 거래량, POC·VAH·VAL 계산 |
| 청산 압력 | 강제청산 스트림을 보조 피처로 사용 |
| NQ +30pt | `ATR × 계수` 또는 `bps`로 정규화 |
| NQ +1.5pt BE | 실제 왕복 비용 기반 가격 오프셋 |

### 22.5 Binance Big Trades 정규화

```python
aggregated_event = aggregate_trades(
    same_aggressor_side=True,
    window_ms=150,
    max_price_distance_ticks=2,
)

event_notional = sum(price * quantity for trade in aggregated_event)

normal_big_trade = event_notional >= rolling_quantile(0.995)
extreme_big_trade = event_notional >= rolling_quantile(0.999)
```

고정 백분위도 시장 상태에 따라 과다·과소 검출될 수 있으므로 세션당 이벤트 수를 함께 통제한다.

```yaml
crypto_big_trade_v1:
  quantile_grid: [0.990, 0.995, 0.998, 0.999]
  target_events_per_liquidity_session: [5, 10, 20]
  aggregation_window_ms: [100, 150, 250]
```

### 22.6 Binance Footprint와 Ceros

가격 단위가 지나치게 세밀하면 셀이 희소해지고, 지나치게 넓으면 흡수가 사라진다.

```python
price_bucket = round_to_multiple(price, footprint_bucket_size)
```

버킷은 최소 다음을 비교한다.

```yaml
footprint_bucket_grid:
  by_ticks: [1, 2, 4, 8]
  by_atr_fraction: [0.01, 0.02, 0.05]
```

암호화폐의 Cero는 literal zero보다 near-zero가 현실적이다.

```python
near_zero_side = side_notional <= rolling_quantile(side_notional, 0.05)
```

### 22.7 +30포인트 추가 진입의 정규화

```python
add_trigger = max(
    entry_price * add_trigger_bps,
    atr_5m * add_trigger_atr,
)
```

연구 그리드:

```yaml
add_trigger_crypto:
  atr_5m: [0.20, 0.35, 0.50]
  bps: [5, 10, 15]
```

추가 진입 후:

```python
weighted_entry = sum(fill_price * fill_qty) / sum(fill_qty)
new_stop = weighted_entry + cost_offset   # long
```

총 위험이 진입 전보다 커지면 추가 주문을 거부한다.

### 22.8 출시 단계

```text
1. 역사 데이터 리플레이 — HLIT A0 코어
2. 소진·존 추가
3. 구조적 손절·BE·추가 진입
4. 실시간 시장 데이터 섀도
5. Big Trades/MIG/Secado telemetry 수집
6. Ablation에서 유효한 모듈만 score_only 승격
7. 최소 수량 실거래
8. Cyborg는 가장 마지막에 제한적 활성화
```

### 22.9 승격 게이트

```yaml
promotion_gates:
  core_hlit:
    out_of_sample_expectancy_r_min: 0.15
    profit_factor_min: 1.15
    minimum_setups_per_regime: 50
    repaint_test: pass

  orderflow_feature:
    incremental_expectancy_positive: true
    walk_forward_windows_positive_ratio_min: 0.60
    sensitivity_stable: true
    no_single_threshold_dependency: true

  live:
    shadow_weeks_min: 8
    paper_weeks_min: 4
    reconciliation_failures: 0
    unprotected_position_incidents: 0
```

### 22.10 v6.0 최종 실행 프로필

```yaml
trullas_v6_runtime:
  official_core:
    metodo_daily: optional
    hlit_regular_divergence: true
    macd: [12, 26, 9]
    fib_levels: [0.25, 0.50, 0.66]
    target: 0.66
    exhaustion: true
    provisional_pivots: true

  inferred_orderflow:
    big_trades: score_only
    mig: score_only
    secado: score_only
    osmotic_zero: telemetry_only
    gold_ratio: analysis_only
    cyborg_large_move: disabled_for_direct_orders

  prohibited:
    - fibonacci_without_divergence
    - blind_limit_at_zone
    - averaging_down
    - stop_widening
    - unprotected_position
    - treating_v1_as_official
```

---

## 23. 출처

### v5.0 신규 자막 원문 (A0)

| 영상 | ID | 분량 |
|---|---|---|
| ⭐ 마스터클래스 (92분) | `bm1SPttrRbM` | 11,850단어 |
| 41년 기록 경신 트레이드 | `ljT5a-OY-Qo` | 2,199단어 |
| 거래 빈도 + 5거래 실례 | `NGvqdsF6dj4` | 1,975단어 |
| +1501% 트레이드 | `mAlsGaEyRRI` | 1,415단어 |
| 개장 패턴 + 반론 | `-dhwafVA6ac` | 2,340단어 |
| 선물·롤오버 | `2AYbNirqqUE` | 2,628단어 |
| 대회 실전 57분 | `Rm53qKZZG8o` | 6,238단어 |
| 역대 TOP3 트레이드 | `fp7k2JYMAv8` | 2,086단어 |
| 4연속 우승의 진실 | `yD7zFLwQ7u8` | 2,263단어 |
| 플랫폼 비교 | `lllApwKl-8A` | 2,347단어 |
| Delta 양수≠상승 | `KhD206ckyDI` | 1,559단어 |
| 볼륨 클러스터 | `TJyBHPTuNio` | 1,678단어 |
| 볼륨의 실제 압력 | `XV44zg_x4vk` | 2,466단어 |
| Bid/Ask 풋프린트 | `i-121CJtLps` | 1,877단어 |
| MOTILIDAD | `OR_tzWD_B7o` | 1,843단어 |

*동일 제목 마스터클래스 5회 아카이브: `UnIIVfME_2g`(123분), `hzHyLOsEzPE`(115분), `btHiteihias`(104분), `GXjsvXlvzHE`(100분), `bm1SPttrRbM`(92분)*

### v4.0 이월 (A0)

`fTcDHSDPv4g`(15,952단어) · `qmlqz7bAx6k`(1,874단어) · `77kTdMa5Jpw` · `0W7EYXrDRBc` · `JunRcXPZBDs`

### 공식 (A1)

- WCTC 순위 및 참가 조건: <https://www.worldcupchampionships.com/>
- Live in Trading 공식 소개·커리큘럼: <https://liveintrading.com/registro-vsl>, <https://liveintrading.com/registro-workshop>
- YouTube 공식 채널: <https://www.youtube.com/@davidtrullas>
- ATAS Big Trades: <https://help.atas.net/en/support/solutions/articles/72000602332-big-trades>
- ATAS Footprint 설정: <https://help.atas.net/en/support/solutions/articles/72000606631>
- ATAS Footprint 패턴: <https://atas.net/es/oportunidades-con-atas-es/graficos-tipo-clusters-footprint/patrones-de-footprint/>
- Binance 공식 개발자 문서: <https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/Introduction>

### 통합된 외부 분석

v1.0(본 계열) · MD판 · DOCX판 · v6.0 화면 역공학 대화 분석

### v6.0 화면 자료

- `David_Trullas_Vila_v6.0_assets/david_promo_chart_crop.png`
- `David_Trullas_Vila_v6.0_assets/david_promo_labels_crop.png`

> Markdown 단독 파일에서는 이미지 경로가 없을 수 있으므로, 함께 제공되는 ZIP 패키지를 풀어보는 것이 가장 확실하다.

### 출처 해석 원칙

```text
공식 교육과정에 이름이 있다 ≠ 판정식이 공개되었다
화면에 숫자가 보인다 ≠ 그 숫자가 모든 셋업에서 쓰인다
대회 최종 수익률이 높다 ≠ MDD·승률·거래당 위험률을 역산할 수 있다
ATAS에 기능이 있다 ≠ David가 그 기능의 기본값을 사용한다
```

---

## 24. 주의사항

1. **자동 생성 자막 기반이다.** 숫자·고유명사 오인식 가능성이 있다. 특히 **SMA 6/70/200**, **BE +1.5pt**, **+30pt** 같은 핵심 수치는 원 영상에서 눈으로 재확인할 것을 강력히 권한다. 부록에 자막 원문을 첨부했다.

2. **"teoría de Dado"는 "teoría de Dow"(다우 이론)의 오인식**으로 판단했다. 다른 영상에서 그가 "teoría de Dow"라고 명확히 말하고 "2/3 되돌림"을 설명하므로 확실하다.

3. **v3.0 → v4.0 → v5.0에서 판단이 세 번 바뀐 항목이 있다**(타임프레임, 손익비, 개장 대기, 월요일). 2차 자료 재구성의 한계를 보여주는 사례이며, **남은 (C) 항목들도 같은 운명일 수 있다.**

4. **마스터클래스는 판매 세미나다.** 시연 사례의 선별 가능성을 감안할 것.

5. **여전히 리스크 %·최대낙폭·승률 실측치는 미공개다.** 이 문서로 규칙은 복원했지만 **성과 기대치는 복원되지 않았다.** 본인 백테스트로만 설정할 것.

6. 본 문서는 교육·연구 목적이며 투자 권유가 아니다.


7. **V1은 화면 역공학이다.** Big Trades의 Strong/Medium, NQ 계약 수 그리드, MIG·Ceros·Secado 판정식, Cyborg 점수표는 검증 가능한 가설이지 본인의 직접 공개 공식이 아니다.

8. **성과 수치를 전략 파라미터로 만들지 말 것.** 대회 수익률·추정 MDD·추정 승률을 사용해 레버리지나 위험률을 역산하면 생존편향과 경로 의존성이 그대로 유입된다.

9. **Binance 이식은 별도 전략 버전이다.** `TRULLAS_A0_CORE`와 `TRULLAS_BINANCE_X1`의 성과를 분리 보고해야 한다.

10. **최종 판단 기준은 재현성이다.** 공개 성공 사례와 비슷한 차트를 찾는 것이 아니라, 실패 사례를 포함한 고정 규칙의 OOS 기대값과 실시간 체결 안정성으로 승격 여부를 결정한다.


---

*문서 버전 6.0 · 2026-08-24 · v5.0 A0 코어 + 공식 자료 + 화면 역공학 V1 통합*
