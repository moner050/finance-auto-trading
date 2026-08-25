from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import SecretStr

if TYPE_CHECKING:
    from autotrader.integrations.brokers.binance_usdm.secrets import (
        BinanceUsdmSecret,
    )

_TOSS_LIVE_REFERENCE = "secret://dotenv/toss/live"
_KIS_REAL_REFERENCE = "secret://dotenv/kis/real"
_KIS_PAPER_REFERENCE = "secret://dotenv/kis/paper"
_SCOPE_DOMAIN = b"EXEC_ACCOUNT_SCOPE_V1"


class AccountSecretResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TossAccountSecret:
    reference: str
    environment: str
    client_id: SecretStr
    client_secret: SecretStr

    def __post_init__(self) -> None:
        if (
            self.reference != _TOSS_LIVE_REFERENCE
            or self.environment != "LIVE"
            or not _secret_value(self.client_id)
            or not _secret_value(self.client_secret)
        ):
            raise ValueError("Toss account secret is invalid")


@dataclass(frozen=True, slots=True)
class KisAccountSecret:
    reference: str
    environment: str
    app_key: SecretStr
    app_secret: SecretStr
    account_number: SecretStr
    product_code: str

    def __post_init__(self) -> None:
        account_number = _secret_value(self.account_number)
        if (
            (self.reference, self.environment)
            not in {
                (_KIS_REAL_REFERENCE, "LIVE"),
                (_KIS_PAPER_REFERENCE, "PAPER"),
            }
            or not _secret_value(self.app_key)
            or not _secret_value(self.app_secret)
            or not _ascii_digits(account_number, length=8)
            or not _ascii_digits(self.product_code, length=2)
        ):
            raise ValueError("KIS account secret is invalid")


class DotenvAccountSecretResolver:
    __slots__ = ("_env_file",)

    def __init__(self, env_file: Path) -> None:
        raw_env_file = cast(object, env_file)
        if not isinstance(raw_env_file, Path):
            raise ValueError("env_file must be a Path")
        self._env_file = raw_env_file

    def resolve_toss(self, reference: str) -> TossAccountSecret:
        env_file = self._env_file
        if reference != _TOSS_LIVE_REFERENCE:
            del env_file, reference, self
            raise AccountSecretResolutionError(
                "account secret is unavailable"
            ) from None
        values = _selected_values(
            env_file,
            ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"),
        )
        del env_file, reference, self
        if values is None:
            raise AccountSecretResolutionError(
                "account secret is unavailable"
            ) from None
        return TossAccountSecret(
            reference=_TOSS_LIVE_REFERENCE,
            environment="LIVE",
            client_id=SecretStr(values["TOSS_CLIENT_ID"]),
            client_secret=SecretStr(values["TOSS_CLIENT_SECRET"]),
        )

    def resolve_kis(self, reference: str) -> KisAccountSecret:
        env_file = self._env_file
        if reference == _KIS_REAL_REFERENCE:
            environment = "LIVE"
            keys = (
                "KIS_APP_KEY",
                "KIS_SECRET_KEY",
                "KIS_BANK_NO",
                "KIS_ACCOUNT_PRODUCT_CODE",
            )
        elif reference == _KIS_PAPER_REFERENCE:
            environment = "PAPER"
            keys = (
                "KIS_PAPER_APP_KEY",
                "KIS_PAPER_SECRET_KEY",
                "KIS_PAPER_BANK_NO",
                "KIS_PAPER_ACCOUNT_PRODUCT_CODE",
            )
        else:
            del env_file, reference, self
            raise AccountSecretResolutionError(
                "account secret is unavailable"
            ) from None
        values = _selected_values(env_file, keys)
        del env_file, keys, self
        if values is None:
            del environment, reference
            raise AccountSecretResolutionError(
                "account secret is unavailable"
            ) from None
        prefix = "KIS_" if environment == "LIVE" else "KIS_PAPER_"
        return KisAccountSecret(
            reference=reference,
            environment=environment,
            app_key=SecretStr(values[f"{prefix}APP_KEY"]),
            app_secret=SecretStr(values[f"{prefix}SECRET_KEY"]),
            account_number=SecretStr(values[f"{prefix}BANK_NO"]),
            product_code=values[f"{prefix}ACCOUNT_PRODUCT_CODE"],
        )

    def resolve_binance_usdm(self, reference: str) -> BinanceUsdmSecret:
        from autotrader.integrations.brokers.binance_usdm.secrets import (
            resolve_binance_usdm_secret,
        )

        env_file = self._env_file
        if reference != "secret://dotenv/binance/usdm/live":
            del env_file, reference, self
            raise AccountSecretResolutionError(
                "account secret is unavailable"
            ) from None
        values = _selected_values(
            env_file,
            ("BINANCE_USDM_API_KEY", "BINANCE_USDM_SECRET_KEY"),
        )
        del env_file, self
        if values is None:
            del reference
            raise AccountSecretResolutionError(
                "account secret is unavailable"
            ) from None
        return resolve_binance_usdm_secret(reference, values)


def toss_account_scope_hash(*, reference: str, account_seq: int) -> bytes:
    if (
        reference != _TOSS_LIVE_REFERENCE
        or type(account_seq) is not int
        or account_seq <= 0
    ):
        raise ValueError("Toss account scope is invalid")
    return _scope_hash("TOSS", "LIVE", reference, str(account_seq))


def kis_account_scope_hash(secret: KisAccountSecret) -> bytes:
    if type(secret) is not KisAccountSecret:
        raise ValueError("exact KIS account secret is required")
    secret.__post_init__()
    account_number = secret.account_number.get_secret_value()
    return _scope_hash(
        "KIS",
        secret.environment,
        secret.reference,
        account_number,
        secret.product_code,
    )


def _selected_values(env_file: Path, keys: tuple[str, ...]) -> dict[str, str] | None:
    try:
        selected = set(keys)
        values: dict[str, str] = {}
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name not in selected:
                continue
            if name in values or not _single_line(value):
                return None
            values[name] = value
        if set(values) != selected:
            return None
        return values
    except Exception:
        return None
    finally:
        del env_file, keys


def _scope_hash(*parts: str) -> bytes:
    if not all(_single_line(part) for part in parts):
        raise ValueError("account scope is invalid")
    return hashlib.sha256(
        _SCOPE_DOMAIN + b"\x00" + b"\x00".join(part.encode("utf-8") for part in parts)
    ).digest()


def _secret_value(value: object) -> str:
    if type(value) is not SecretStr:
        return ""
    return value.get_secret_value()


def _single_line(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and "\r" not in value
        and "\n" not in value
        and "\\r" not in value
        and "\\n" not in value
    )


def _ascii_digits(value: object, *, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and value.isascii()
        and value.isdecimal()
    )
