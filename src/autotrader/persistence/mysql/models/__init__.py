"""SQLAlchemy models owned by MySQL persistence.

Importing this package registers every table on the shared metadata, which is
what Alembic compares the live schema against.
"""

from autotrader.persistence.mysql.models import (
    accounts as accounts,
)
from autotrader.persistence.mysql.models import (
    bindings as bindings,
)
from autotrader.persistence.mysql.models import (
    david_v6 as david_v6,
)
from autotrader.persistence.mysql.models import (
    events as events,
)
from autotrader.persistence.mysql.models import (
    fills as fills,
)
from autotrader.persistence.mysql.models import (
    intents as intents,
)
from autotrader.persistence.mysql.models import (
    operations as operations,
)
from autotrader.persistence.mysql.models import (
    orders as orders,
)
from autotrader.persistence.mysql.models import (
    paper as paper,
)
from autotrader.persistence.mysql.models import (
    positions as positions,
)
from autotrader.persistence.mysql.models import (
    reconciliation as reconciliation,
)
from autotrader.persistence.mysql.models import (
    risk as risk,
)
from autotrader.persistence.mysql.models import (
    strategy as strategy,
)
from autotrader.persistence.mysql.models.backoffice import (
    BackofficeBootstrapAuthorityRow,
    BackofficeCommandRow,
    BackofficeSecondPasswordVersionRow,
    BackofficeSecretActivationRow,
    BackofficeSecretVersionRow,
)
from autotrader.persistence.mysql.models.binance_usdm import (
    BinanceUsdmAlgoOrderFactRow,
    BinanceUsdmBalanceFactRow,
    BinanceUsdmConfigurationFactRow,
    BinanceUsdmIncomeFactRow,
    BinanceUsdmOrderFactRow,
    BinanceUsdmPositionFactRow,
    BinanceUsdmReconciliationRunRow,
    BinanceUsdmTradeFactRow,
)
from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.models.pessimism import MarketPessimismDailyRow
from autotrader.persistence.mysql.models.promotion import PromotionSessionRow
from autotrader.persistence.mysql.models.toss_us_reconciliation import (
    TossUsCashFactRow,
    TossUsOrderFactRow,
    TossUsPositionFactRow,
    TossUsReconciliationRunRow,
    TossUsRecoveryLeaseRow,
)
from autotrader.persistence.mysql.models.universe import (
    UniverseSnapshotMemberRow,
    UniverseSnapshotRow,
)

metadata = CoreBase.metadata

__all__ = [
    "BackofficeBootstrapAuthorityRow",
    "BackofficeCommandRow",
    "BackofficeSecondPasswordVersionRow",
    "BackofficeSecretActivationRow",
    "BackofficeSecretVersionRow",
    "BinanceUsdmAlgoOrderFactRow",
    "BinanceUsdmBalanceFactRow",
    "BinanceUsdmConfigurationFactRow",
    "BinanceUsdmIncomeFactRow",
    "BinanceUsdmOrderFactRow",
    "BinanceUsdmPositionFactRow",
    "BinanceUsdmReconciliationRunRow",
    "BinanceUsdmTradeFactRow",
    "MarketPessimismDailyRow",
    "PromotionSessionRow",
    "TossUsCashFactRow",
    "TossUsOrderFactRow",
    "TossUsPositionFactRow",
    "TossUsReconciliationRunRow",
    "TossUsRecoveryLeaseRow",
    "UniverseSnapshotMemberRow",
    "UniverseSnapshotRow",
    "metadata",
]
