from __future__ import annotations

from base64 import b64decode
from binascii import Error as BinasciiError
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import HttpUrl, SecretStr, TypeAdapter, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


class RuntimeMode(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


class TradingState(StrEnum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="", case_sensitive=False, extra="ignore"
    )

    trading_mode: RuntimeMode = RuntimeMode.SHADOW
    allow_live: bool = False
    database_url: str | None = None
    mysql_host: str | None = None
    mysql_port: int | None = None
    mysql_database: str | None = None
    mysql_user: str | None = None
    mysql_password: SecretStr | None = None
    redis_url: str | None = None
    backoffice_public_url: str | None = None
    backoffice_master_key: SecretStr | None = None
    backoffice_master_key_version: int | None = None
    backoffice_previous_master_key: SecretStr | None = None
    backoffice_previous_master_key_version: int | None = None

    @model_validator(mode="after")
    def resolve_mysql_component_url(self) -> Settings:
        components = (
            self.mysql_host,
            self.mysql_port,
            self.mysql_database,
            self.mysql_user,
            self.mysql_password,
        )
        if self.database_url is not None or not any(
            component is not None for component in components
        ):
            return self
        if not all(component is not None for component in components):
            raise ValueError(
                "MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE, MYSQL_USER, and "
                "MYSQL_PASSWORD must be configured together"
            )
        host = self.mysql_host
        port = self.mysql_port
        database = self.mysql_database
        user = self.mysql_user
        password = self.mysql_password
        assert host is not None
        assert port is not None
        assert database is not None
        assert user is not None
        assert password is not None
        if not 1 <= port <= 65535 or not all(
            (
                host.strip(),
                database.strip(),
                user.strip(),
                password.get_secret_value().strip(),
            )
        ):
            raise ValueError(
                "MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE, MYSQL_USER, and "
                "MYSQL_PASSWORD must be configured together"
            )
        return self

    @model_validator(mode="after")
    def validate_backoffice_bootstrap(self) -> Settings:
        current_components = (
            self.backoffice_public_url,
            self.backoffice_master_key,
            self.backoffice_master_key_version,
        )
        previous_components = (
            self.backoffice_previous_master_key,
            self.backoffice_previous_master_key_version,
        )
        if any(component is not None for component in current_components) and not all(
            component is not None for component in current_components
        ):
            raise ValueError(
                "BACKOFFICE_PUBLIC_URL, BACKOFFICE_MASTER_KEY, and "
                "BACKOFFICE_MASTER_KEY_VERSION must be configured together"
            )
        if any(component is not None for component in previous_components) and not all(
            component is not None for component in previous_components
        ):
            raise ValueError(
                "BACKOFFICE_PREVIOUS_MASTER_KEY and "
                "BACKOFFICE_PREVIOUS_MASTER_KEY_VERSION must be configured together"
            )
        if all(component is None for component in current_components):
            if any(component is not None for component in previous_components):
                raise ValueError("BACKOFFICE previous key requires current bootstrap")
            return self

        public_url = self.backoffice_public_url
        master_key = self.backoffice_master_key
        master_key_version = self.backoffice_master_key_version
        assert public_url is not None
        assert master_key is not None
        assert master_key_version is not None
        try:
            _HTTP_URL_ADAPTER.validate_python(public_url)
            parsed_url = urlsplit(public_url)
            _ = parsed_url.port
        except (ValidationError, ValueError) as exc:
            raise ValueError("BACKOFFICE_PUBLIC_URL must be an HTTPS origin") from exc
        if (
            "\\" in public_url
            or any(character.isspace() for character in public_url)
            or parsed_url.netloc.endswith(":")
            or parsed_url.scheme != "https"
            or parsed_url.hostname is None
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in ("", "/")
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("BACKOFFICE_PUBLIC_URL must be an HTTPS origin")
        if master_key_version <= 0:
            raise ValueError("BACKOFFICE_MASTER_KEY_VERSION must be positive")

        keys = (master_key, self.backoffice_previous_master_key)
        for key in keys:
            if key is None:
                continue
            try:
                decoded_key = b64decode(key.get_secret_value(), validate=True)
            except (BinasciiError, ValueError) as exc:
                raise ValueError("backoffice master keys must be base64") from exc
            if len(decoded_key) != 32:
                raise ValueError("backoffice master keys must decode to 32 bytes")

        previous_master_key_version = self.backoffice_previous_master_key_version
        if previous_master_key_version is not None:
            if previous_master_key_version <= 0:
                raise ValueError(
                    "BACKOFFICE_PREVIOUS_MASTER_KEY_VERSION must be positive"
                )
            if previous_master_key_version == master_key_version:
                raise ValueError("backoffice master key versions must be distinct")
        return self

    @property
    def database_connection_url(self) -> str | None:
        if self.database_url is not None:
            return self.database_url
        if any(
            component is None
            for component in (
                self.mysql_host,
                self.mysql_port,
                self.mysql_database,
                self.mysql_user,
                self.mysql_password,
            )
        ):
            return None
        assert self.mysql_host is not None
        assert self.mysql_port is not None
        assert self.mysql_database is not None
        assert self.mysql_user is not None
        assert self.mysql_password is not None
        return URL.create(
            "mysql+aiomysql",
            username=self.mysql_user,
            password=self.mysql_password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
        ).render_as_string(hide_password=False)


def effective_startup_state(settings: Settings) -> TradingState:
    del settings
    return TradingState.DISARMED
