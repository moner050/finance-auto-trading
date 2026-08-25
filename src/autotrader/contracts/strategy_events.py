from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from autotrader.contracts.envelope import EventEnvelope
from autotrader.strategies.common.decisions import StrategyDecision


class StrategyDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1)
    decision_hash: str = Field(min_length=64, max_length=64)

    @classmethod
    def from_decision(cls, decision: StrategyDecision) -> StrategyDecisionPayload:
        return cls(
            decision_id=str(decision.id), decision_hash=decision.decision_hash().hex()
        )


type StrategyDecisionEvent = EventEnvelope[StrategyDecisionPayload]
