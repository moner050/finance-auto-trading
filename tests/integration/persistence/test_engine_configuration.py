from pydantic import SecretStr

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine


def test_engine_uses_aiomysql_and_installs_utc_strict_initialization():
    engine = create_engine(
        Settings(
            database_url="mysql+aiomysql://user:pass@localhost:3306/finance_auto_trading"
        )
    )

    assert engine.url.drivername == "mysql+aiomysql"
    # Isolation is set once when the connection is made, not through
    # `execution_options`, which asks SQLAlchemy to set it on every checkout
    # and put it back on every return - about 120ms per session against a
    # database thirty milliseconds away, which was more than the queries.
    assert "isolation_level" not in engine.sync_engine.get_execution_options()
    assert engine.sync_engine.pool.dispatch.connect

    statements: list[str] = []

    class _Cursor:
        def execute(self, statement: str) -> None:
            statements.append(statement)

        def close(self) -> None:
            return None

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    # Only our handler. SQLAlchemy registers its own alongside it, and
    # driving the whole chain would need a full DBAPI fake - which would make
    # this a test of SQLAlchemy rather than of what the engine sets.
    ours = [
        handler
        for handler in engine.sync_engine.pool.dispatch.connect
        if handler.__name__ == "configure_connection"
    ]
    assert len(ours) == 1
    ours[0](_Connection(), None)

    joined = " ".join(statements)
    assert "SET time_zone = '+00:00'" in joined
    assert "STRICT_ALL_TABLES" in joined
    # The isolation the loop reads positions and orders under. Losing it
    # would not fail a test that only checked the option was absent.
    assert "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED" in joined


def test_engine_uses_component_derived_mysql_connection_url():
    engine = create_engine(
        Settings(
            mysql_host="localhost",
            mysql_port=3306,
            mysql_database="finance_auto_trading",
            mysql_user="user",
            mysql_password=SecretStr("pass"),
        )
    )

    assert engine.url.drivername == "mysql+aiomysql"
