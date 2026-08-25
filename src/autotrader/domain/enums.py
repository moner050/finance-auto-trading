from enum import StrEnum


class BrokerProvider(StrEnum):
    TOSS = "TOSS"
    KIS = "KIS"
    BINANCE = "BINANCE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class IntentType(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    PROTECTIVE = "PROTECTIVE"


class OrderStyle(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
