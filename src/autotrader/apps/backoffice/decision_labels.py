"""What a decision's codes mean, in the operator's language.

Section 12 asks for the operator's language on screen with the stable reason
code still visible beside it. Both halves matter and for different reasons: an
operator reading a night's decisions should not be parsing
`REGULAR_DIVERGENCE_ABSENT`, and an operator searching the audit trail or
asking about a case has nothing to quote but that code. So the screen shows
the label and keeps the code.

The fallback is the code itself, never a blank and never a guess. Evidence
blockers are open-ended - any fact can report its own - so a table that
claimed to be exhaustive would eventually be lying, and the failure of a wrong
label is worse than the inconvenience of an untranslated one.

`test_decision_labels` reads the source for every literal blocker the engine
can append and fails when one has no entry here, which is what stops this
drifting behind the code it describes.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# Section 21.3's weighted indicators, plus the direction evidence the engine
# checks separately. These are the things the engine looked for and found.
INDICATOR_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "higher_timeframe_bias_aligned": "상위 시간대 방향 일치",
        "regular_hlit_divergence": "정규 다이버전스",
        "hidden_divergence": "히든 다이버전스",
        "fibonacci_extension_cluster": "피보나치 확장 밀집",
        "profile_value_confluence": "볼륨 프로파일 가치영역 합치",
        "v1_secado": "Secado (건조)",
        "v1_cero_osmotico": "Cero osmótico (삼투 영점)",
        "v1_mig_reversal": "MIG 반전",
        "supporting_big_trade_behind": "뒤를 받치는 대형 체결",
        "blocking_big_trade_ahead": "앞을 막는 대형 체결",
        "high_impact_news_risk": "고영향 뉴스 위험",
        "abnormal_spread_or_slippage": "비정상 스프레드·슬리피지",
        "direction:LONG": "방향 증거 (롱)",
        "direction:SHORT": "방향 증거 (숏)",
    }
)

# Everything the engine, the risk authority and the evidence assembly can
# refuse on. Grouped in the source by where they come from, because that is
# how someone verifying one of these will look for it.
BLOCKER_LABELS: Mapping[str, str] = MappingProxyType(
    {
        # --- the setup itself -------------------------------------------
        "SETUP_REJECTED": "셋업 등급 미달",
        "NO_MARKED_ZONE": "마크된 존 없음",
        "REGULAR_DIVERGENCE_ABSENT": "정규 다이버전스 없음",
        "EXHAUSTION_ABSENT": "소진 없음",
        "EXHAUSTION_UNCONFIRMED": "소진 미확정",
        "EXHAUSTION_DIRECTION_MISMATCH": "소진 방향 불일치",
        "DIRECTION_EVIDENCE_MISSING": "방향 증거 없음",
        "CONTRADICTORY_DIRECTION_EVIDENCE": "방향 증거 모순",
        "BLOCKING_BIG_TRADE_AHEAD": "앞을 막는 대형 체결",
        "BIG_TRADE_SAMPLE_INSUFFICIENT": "대형 체결 표본 부족",
        "METODO_GATE_FAILED": "Método 관문 미통과",
        "METODO_CASH_ONLY": "Método는 현금 시장 전용",
        # --- universe and regime ----------------------------------------
        "UNIVERSE_INELIGIBLE": "유니버스 자격 미달",
        "UNIVERSE_CASH_ONLY": "유니버스는 현금 시장 전용",
        "UNIVERSE_UNAVAILABLE": "유니버스 사실 없음",
        "NOT_MEMBER_AS_OF": "그 날짜 기준 구성원 아님",
        "NOT_COMMON_STOCK_AS_OF": "그 날짜 기준 보통주 아님",
        "COUNTRY_NOT_STRONG": "국가 강세 아님",
        "BELOW_MEDIAN_TRADED_VALUE": "거래대금 중앙값 미만",
        "SECTOR_OUTSIDE_TOP_THREE": "섹터 상위 3위 밖",
        "SECTOR_AUTHORITY_UNAVAILABLE": "섹터 분류 없음",
        "EXCLUDED_SECTOR": "제외 섹터",
        "REGIME_EXCLUDED": "국면 제외",
        "REGIME_UNAVAILABLE": "국면 사실 없음",
        # --- calendar and session ---------------------------------------
        "CALENDAR_BLOCKED": "캘린더 차단 (뉴스)",
        "CALENDAR_UNAVAILABLE": "캘린더 사실 없음",
        "SESSION_ENTRY_BLOCKED": "세션 진입 불가",
        "SESSION_CLOSED": "세션 종료",
        "SESSION_CALENDAR_UNAVAILABLE": "세션 캘린더 없음",
        "SESSION_CALENDAR_MARKET_MISMATCH": "세션 캘린더 시장 불일치",
        "ENTRY_CUTOFF_REACHED": "진입 마감 시각 경과",
        "FLAT_CUTOFF_REACHED": "청산 마감 시각 경과",
        "SESSION_OBJECTIVE_REACHED": "세션 목표 달성",
        "SESSION_TRADE_UPPER_BOUND": "세션 매매 횟수 상한",
        "KRX_SINGLE_PRICE_AUCTION": "단일가 매매 시간",
        "KRX_VI_ACTIVE": "변동성완화장치(VI) 발동",
        # --- evidence that could not be measured ------------------------
        "ZONES_BARS_UNAVAILABLE": "존 계산용 봉 부족",
        "ZONES_EVIDENCE_INVALID": "존 증거 무효",
        "DIVERGENCE_MACD_WARMUP_UNAVAILABLE": "MACD 예열 부족",
        "EXHAUSTION_INPUTS_UNAVAILABLE": "소진 입력 없음",
        "EXHAUSTION_EVIDENCE_INVALID": "소진 증거 무효",
        "METODO_DAILY_WARMUP_UNAVAILABLE": "Método 일봉 예열 부족",
        "ORDER_FLOW_UNAVAILABLE": "주문 흐름 사실 없음",
        "ORDER_FLOW_BINANCE_ONLY": "주문 흐름은 Binance 전용",
        "PROFILE_UNAVAILABLE": "볼륨 프로파일 없음",
        "PROFILE_BINANCE_ONLY": "볼륨 프로파일은 Binance 전용",
        "COSTS_UNAVAILABLE": "비용 사실 없음",
        "FILL_FEE_EVIDENCE_MISSING": "체결 수수료 증거 없음",
        # --- sizing and risk --------------------------------------------
        "ROUNDED_QUANTITY_ZERO": "수량이 0으로 내림",
        "NON_POSITIVE_STOP": "손절가가 0 이하",
        "INVALID_LONG_STRUCTURAL_REFERENCE": "롱 구조 기준가 무효",
        "INVALID_SHORT_STRUCTURAL_REFERENCE": "숏 구조 기준가 무효",
        "OPEN_RISK_LIMIT": "미결제 위험 한도",
        "DAILY_LOSS_LIMIT": "일일 손실 한도",
        "WEEKLY_LOSS_LIMIT": "주간 손실 한도",
        "CONSECUTIVE_LOSS_LIMIT": "연속 손실 한도",
        "RISK_MARKET_MISMATCH": "리스크 정책 시장 불일치",
        "CASH_A_CANDIDATE_UNSUPPORTED": "현금 정책에 A후보 비율 없음",
        "SPREAD_ABOVE_THREE_TICKS": "스프레드 3틱 초과",
        "STOP_DISTANCE_BELOW_0_40_ATR5M": "손절 거리가 ATR5m의 0.40배 미만",
        "STOP_DISTANCE_ABOVE_1_50_ATR5M": "손절 거리가 ATR5m의 1.50배 초과",
        "BINANCE_ATR30S_REQUIRED": "Binance는 30초 ATR 필요",
        "CASH_ATR30S_NOT_APPLICABLE": "현금 시장에 30초 ATR 해당 없음",
        # --- venue and account ------------------------------------------
        "SPOT_VENUE_CANNOT_SHORT": "현물 거래소는 숏 불가",
        "CASH_LEVERAGE_NOT_APPLICABLE": "현금 시장에 레버리지 해당 없음",
        "BINANCE_LEVERAGE_REQUIRED": "Binance 레버리지 사실 필요",
        "BINANCE_LEVERAGE_LIMIT": "Binance 레버리지 한도",
        "LEVERAGE_MISMATCH": "레버리지 불일치",
        "LEVERAGE_OUT_OF_RANGE": "레버리지 허용 범위 밖",
        "MARGIN_TYPE_NOT_ISOLATED": "마진 격리 모드 아님",
        "AUTO_ADD_MARGIN_ENABLED": "자동 증거금 추가 켜짐",
        "MULTI_ASSET_MODE_ENABLED": "멀티에셋 모드 켜짐",
        "POSITION_MODE_NOT_ONE_WAY": "단방향 포지션 모드 아님",
        "POSITION_MODE_FACT_CONFLICT": "포지션 모드 사실 충돌",
        "ACCOUNT_TRADING_DISABLED": "계정 거래 비활성",
        "ACCOUNT_FACT_STALE": "계정 사실이 오래됨",
        "ACCOUNT_FACT_FAILURE": "계정 사실 조회 실패",
        "ACCOUNT_FACT_SCOPE_MISMATCH": "계정 사실 범위 불일치",
        "ACCOUNT_FACT_TIME_INVALID": "계정 사실 시각 무효",
        "ACCOUNT_SNAPSHOT_TIME_MISMATCH": "계정 스냅샷 시각 불일치",
        # --- key permissions and stray exposure -------------------------
        "API_KEY_WITHDRAWALS_ENABLED": "API 키 출금 권한 켜짐",
        "API_KEY_WITHDRAWALS_UNPROVEN": "API 키 출금 권한 미확인",
        "API_KEY_IP_NOT_RESTRICTED": "API 키 IP 제한 없음",
        "API_KEY_IP_RESTRICTION_UNPROVEN": "API 키 IP 제한 미확인",
        "API_KEY_EVIDENCE_STALE": "API 키 증거가 오래됨",
        "UNOWNED_BTCUSDT_EXPOSURE": "장부에 없는 BTCUSDT 노출",
        "UNEXPECTED_SYMBOL_EXPOSURE": "예상 밖 종목 노출",
        "UNEXPECTED_SYMBOL_NORMAL_ORDER": "예상 밖 종목 일반 주문",
        "UNEXPECTED_SYMBOL_ALGO_ORDER": "예상 밖 종목 알고 주문",
        "UNSTABLE_ORDER_SNAPSHOT": "주문 스냅샷 불안정",
    }
)

# `engine.py` composes three families from a fact key at runtime, so they
# cannot be listed. Read the shape instead of leaving them untranslated.
_FACT_STATES: Mapping[str, str] = MappingProxyType(
    {
        "UNAVAILABLE": "사실 없음",
        "NOT_APPLICABLE": "해당 없음",
        "INVALID": "무효",
    }
)
_FACT_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "UNIVERSE": "유니버스",
        "REGIME": "국면",
        "METODO": "Método",
        "ZONES": "존",
        "DIVERGENCE": "다이버전스",
        "EXHAUSTION": "소진",
        "ORDER_FLOW": "주문 흐름",
        "PROFILE": "볼륨 프로파일",
        "CALENDAR": "캘린더",
        "SESSION": "세션",
        "COSTS": "비용",
    }
)


def indicator_label(key: str) -> str:
    """The indicator in the operator's language, or the key unchanged."""
    if type(key) is not str:
        raise TypeError("indicator key must be text")
    return INDICATOR_LABELS.get(key, key)


def blocker_label(code: str) -> str:
    """The reason in the operator's language, or the code unchanged.

    An unrecognised code is returned as it is. A screen showing a code it
    cannot explain is a small inconvenience; one showing a confident label
    for something else is a wrong answer, and these are the reasons an
    operator decides whether the loop is behaving.
    """
    if type(code) is not str:
        raise TypeError("blocker code must be text")
    known = BLOCKER_LABELS.get(code)
    if known is not None:
        return known
    prefix, separator, rest = code.partition(":")
    if separator and prefix == "INDICATOR_PROVENANCE_MISSING":
        return f"지표 출처 없음 ({indicator_label(rest)})"
    for state, reading in _FACT_STATES.items():
        marker = f"_FACT_{state}"
        if code.endswith(marker):
            name = _FACT_NAMES.get(code[: -len(marker)])
            if name is not None:
                return f"{name} {reading}"
    if code.endswith("_VALUE_INVALID"):
        name = _FACT_NAMES.get(code[: -len("_VALUE_INVALID")])
        if name is not None:
            return f"{name} 값 무효"
    return code


__all__ = (
    "BLOCKER_LABELS",
    "INDICATOR_LABELS",
    "blocker_label",
    "indicator_label",
)
