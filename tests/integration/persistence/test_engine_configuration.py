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
    assert (
        engine.sync_engine.get_execution_options()["isolation_level"]
        == "READ COMMITTED"
    )
    assert engine.sync_engine.pool.dispatch.connect


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
