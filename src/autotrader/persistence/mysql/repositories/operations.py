from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.execution.controls.models import KillSwitchLevel
from autotrader.persistence.mysql.models.operations import (
    OpsAuditLog,
    OpsRuntimeInstance,
    OpsSchedulerLease,
    OpsTradingControl,
)


async def lock_global_dispatch_guard(session: AsyncSession) -> OpsTradingControl:
    """Serialize new blocking evidence with dispatch authorization attempts."""

    control = await session.scalar(
        select(OpsTradingControl)
        .where(
            OpsTradingControl.scope_type == "GLOBAL",
            OpsTradingControl.scope_key == "GLOBAL",
        )
        .with_for_update()
    )
    if control is not None:
        return control
    await session.execute(
        insert(OpsTradingControl)
        .values(
            scope_type="GLOBAL",
            scope_key="GLOBAL",
            armed=False,
            kill_switch_level=KillSwitchLevel.NONE.value,
            owner_runtime_instance_id=None,
            acquired_at=None,
            expires_at=None,
            fencing_token=0,
            row_version=1,
        )
        .prefix_with("IGNORE")
    )
    control = await session.scalar(
        select(OpsTradingControl)
        .where(
            OpsTradingControl.scope_type == "GLOBAL",
            OpsTradingControl.scope_key == "GLOBAL",
        )
        .with_for_update()
    )
    if control is None:
        raise RuntimeError("global dispatch guard cannot be read")
    return control


class RuntimeControlRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_runtime_instance(
        self, *, runtime_instance_id: UUID, now: datetime
    ) -> OpsRuntimeInstance:
        runtime = OpsRuntimeInstance(
            id=runtime_instance_id,
            local_state="DISARMED",
            started_at=now,
            stopped_at=None,
        )
        self._session.add(runtime)
        await self._session.flush()
        return runtime

    async def acquire_named_scheduler_lease(
        self,
        *,
        lease_name: str,
        runtime_instance_id: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> OpsSchedulerLease | None:
        if (
            type(lease_name) is not str
            or not lease_name
            or len(lease_name) > 64
            or lease_name != lease_name.strip()
            or lease_expires_at <= now
        ):
            raise ValueError("named scheduler lease input is invalid")
        lease = await self._session.scalar(
            select(OpsSchedulerLease)
            .where(OpsSchedulerLease.lease_name == lease_name)
            .with_for_update()
        )
        if (
            lease is not None
            and lease.expires_at is not None
            and lease.expires_at > now
            and lease.owner_runtime_instance_id != runtime_instance_id
        ):
            return None
        if lease is None:
            lease = OpsSchedulerLease(
                lease_name=lease_name,
                owner_runtime_instance_id=runtime_instance_id,
                acquired_at=now,
                expires_at=lease_expires_at,
                fencing_token=1,
                row_version=1,
            )
            self._session.add(lease)
        else:
            lease.owner_runtime_instance_id = runtime_instance_id
            lease.acquired_at = now
            lease.expires_at = lease_expires_at
            lease.fencing_token += 1
            lease.row_version += 1
        await self._session.flush()
        return lease

    async def release_named_scheduler_lease(
        self,
        *,
        lease_name: str,
        runtime_instance_id: UUID,
        fencing_token: int,
        now: datetime,
    ) -> bool:
        lease = await self._session.scalar(
            select(OpsSchedulerLease)
            .where(OpsSchedulerLease.lease_name == lease_name)
            .with_for_update()
        )
        if (
            lease is None
            or lease.owner_runtime_instance_id != runtime_instance_id
            or lease.fencing_token != fencing_token
        ):
            return False
        lease.owner_runtime_instance_id = None
        lease.acquired_at = None
        lease.expires_at = now
        lease.row_version += 1
        await self._session.flush()
        return True

    async def stop_runtime_instance(
        self, *, runtime_instance_id: UUID, now: datetime
    ) -> bool:
        runtime = await self._session.scalar(
            select(OpsRuntimeInstance)
            .where(OpsRuntimeInstance.id == runtime_instance_id)
            .with_for_update()
        )
        if runtime is None or runtime.stopped_at is not None:
            return False
        runtime.stopped_at = now
        await self._session.flush()
        return True

    async def create_control(
        self,
        *,
        scope_type: str,
        scope_key: str,
        owner_runtime_instance_id: UUID,
        armed: bool,
        kill_switch_level: KillSwitchLevel,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> OpsTradingControl:
        control = await self._session.scalar(
            select(OpsTradingControl)
            .where(
                OpsTradingControl.scope_type == scope_type,
                OpsTradingControl.scope_key == scope_key,
            )
            .with_for_update()
        )
        if control is None:
            control = OpsTradingControl(
                scope_type=scope_type,
                scope_key=scope_key,
                owner_runtime_instance_id=owner_runtime_instance_id,
                armed=armed,
                kill_switch_level=kill_switch_level,
                acquired_at=acquired_at,
                expires_at=expires_at,
                fencing_token=1,
                row_version=1,
            )
            self._session.add(control)
        else:
            control.owner_runtime_instance_id = owner_runtime_instance_id
            control.armed = armed
            control.kill_switch_level = kill_switch_level.value
            control.acquired_at = acquired_at
            control.expires_at = expires_at
            control.fencing_token += 1
            control.row_version += 1
        if armed:
            self._session.add(
                OpsAuditLog(
                    action="INITIAL_ARMED_CONTROL_CREATED",
                    scope_type=scope_type,
                    scope_key=scope_key,
                    actor_runtime_instance_id=owner_runtime_instance_id,
                    fencing_token=control.fencing_token,
                    details={"armed": True},
                    occurred_at=acquired_at,
                )
            )
        await self._session.flush()
        return control

    async def start_execution_control(
        self,
        *,
        runtime_instance_id: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be after now")
        self._session.add(
            OpsRuntimeInstance(
                id=runtime_instance_id,
                local_state="DISARMED",
                started_at=now,
            )
        )
        lease = await self._session.scalar(
            select(OpsSchedulerLease)
            .where(OpsSchedulerLease.lease_name == "execution-control")
            .with_for_update()
        )
        if (
            lease is not None
            and lease.expires_at is not None
            and lease.expires_at > now
        ):
            await self._session.flush()
            return False
        if lease is None:
            lease = OpsSchedulerLease(
                lease_name="execution-control",
                owner_runtime_instance_id=runtime_instance_id,
                acquired_at=now,
                expires_at=lease_expires_at,
                fencing_token=1,
                row_version=1,
            )
            self._session.add(lease)
        else:
            lease.owner_runtime_instance_id = runtime_instance_id
            lease.acquired_at = now
            lease.expires_at = lease_expires_at
            lease.fencing_token += 1
            lease.row_version += 1
            control = await self._session.scalar(
                select(OpsTradingControl)
                .where(
                    OpsTradingControl.scope_type == "GLOBAL",
                    OpsTradingControl.scope_key == "GLOBAL",
                )
                .with_for_update()
            )
            if control is not None:
                control.owner_runtime_instance_id = runtime_instance_id
                control.acquired_at = now
                control.expires_at = lease_expires_at
                control.armed = False
                control.fencing_token += 1
                control.row_version += 1
                self._session.add(
                    OpsAuditLog(
                        action="SCHEDULER_LEASE_TAKEOVER_DISARMED",
                        scope_type="GLOBAL",
                        scope_key="GLOBAL",
                        actor_runtime_instance_id=runtime_instance_id,
                        fencing_token=control.fencing_token,
                        details={"scheduler_fencing_token": lease.fencing_token},
                        occurred_at=now,
                    )
                )
        await self._session.flush()
        return True
