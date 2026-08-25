from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from conftest import (
    _require_disposable_test_database,
    prepare_integration_database,
    reset_schema,
)


def test_schema_reset_rejects_non_disposable_database_urls() -> None:
    with pytest.raises(RuntimeError, match="disposable CI test database"):
        reset_schema("mysql+aiomysql://user:password@example.com:3306/production")


def test_schema_reset_requires_ci_even_for_the_test_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CI", raising=False)

    with pytest.raises(RuntimeError, match="disposable CI test database"):
        reset_schema(
            "mysql+aiomysql://autotrader:local-development-only@"
            "127.0.0.1:3306/finance_auto_trading_test"
        )


def test_targeted_test_accepts_exact_authorized_database_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOTRADER_AUTHORIZED_TEST_DATABASE_FINGERPRINT",
        "7dcf20d1fc590b725637440c129b50ced23cc12bc4671f030f1e8feebd533d07",
    )

    _require_disposable_test_database(
        "mysql+aiomysql://user:password@example.com:3306/production",
        allow_targeted=True,
    )


def test_targeted_test_rejects_mismatched_database_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOTRADER_AUTHORIZED_TEST_DATABASE_FINGERPRINT",
        "0" * 64,
    )

    with pytest.raises(RuntimeError, match="disposable CI test database"):
        _require_disposable_test_database(
            "mysql+aiomysql://user:password@example.com:3306/production",
            allow_targeted=True,
        )


def test_schema_reset_ignores_targeted_database_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOTRADER_AUTHORIZED_TEST_DATABASE_FINGERPRINT",
        "7dcf20d1fc590b725637440c129b50ced23cc12bc4671f030f1e8feebd533d07",
    )

    with pytest.raises(RuntimeError, match="disposable CI test database"):
        reset_schema("mysql+aiomysql://user:password@example.com:3306/production")


def test_integration_fixture_rejects_non_disposable_database_before_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "mysql+aiomysql://user:password@example.com:3306/production",
    )

    def ignore_migration(*_args: object, **_kwargs: object) -> None:
        return None

    def closest_marker(name: str) -> object | None:
        return object() if name == "integration" else None

    monkeypatch.setattr("conftest.command.downgrade", ignore_migration)
    monkeypatch.setattr("conftest.command.upgrade", ignore_migration)
    request = SimpleNamespace(
        path=Path("tests/integration/persistence/test_example.py"),
        node=SimpleNamespace(get_closest_marker=closest_marker),
    )
    fixture = cast(
        Callable[[pytest.FixtureRequest], None],
        prepare_integration_database,
    )

    with pytest.raises(RuntimeError, match="disposable CI test database"):
        fixture(cast(pytest.FixtureRequest, request))
