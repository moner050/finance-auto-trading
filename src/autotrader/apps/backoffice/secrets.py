"""Secrets in MySQL, and a plaintext that has nowhere to leak to.

Section 8 asks for versioned AES-256-GCM records whose key never touches the
database, so a backup carries ciphertext and nothing that opens it. That much
is the easy half. The harder half is section 8.3: the plaintext must never
reach JSON, a template, a log line, an exception message, an audit detail, or
a debug representation.

A str cannot promise that. Every one of those paths reaches for str, repr or
JSON, and a str answers all three honestly. So the resolver returns a Secret,
which answers all three with nothing and hands the value over only when asked
in as many words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.persistence.mysql.models.backoffice import (
    BackofficeSecretActivationRow,
    BackofficeSecretVersionRow,
)
from autotrader.security.secret_crypto import MasterKeyRing, SecretEnvelope
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

AAD_SCHEMA_VERSION: Final = 1
REFERENCE = re.compile(r"^secret://db/(?P<name>[a-z0-9][a-z0-9._-]{0,62})@active$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

OAUTH = "OAUTH"
PROVIDER_CREDENTIAL = "PROVIDER_CREDENTIAL"
ACCOUNT_IDENTIFIER = "ACCOUNT_IDENTIFIER"


class SecretNotFoundError(LookupError):
    """Raised when no active secret answers to a reference."""


class SecretReferenceError(ValueError):
    """Raised when a reference is not the exact form consumers must use."""


class Secret:
    """A plaintext that refuses to be printed.

    Not a dataclass and not a str subclass on purpose: both would inherit a
    representation that shows the value.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if type(value) is not str or not value:
            raise ValueError("a secret must be a non-empty string")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The value, asked for in as many words."""
        return self._value

    def __repr__(self) -> str:
        return "<Secret hidden>"

    def __str__(self) -> str:
        # Templates and f-strings land here.
        return "<Secret hidden>"

    def __format__(self, spec: str) -> str:
        del spec
        return "<Secret hidden>"

    def __eq__(self, other: object) -> bool:
        # Comparing against a plaintext is how a secret ends up in an
        # assertion message, and comparing two secrets is not a use this has.
        return NotImplemented

    def __hash__(self) -> int:
        raise TypeError("a secret is not hashable")

    def __iter__(self) -> object:
        # json.dumps and dict() both probe for this before failing usefully.
        raise TypeError("a secret cannot be serialized")


@dataclass(frozen=True, slots=True)
class SecretScope:
    """Where a secret belongs, which is also part of what authenticates it."""

    category: str
    provider_code: str
    environment: str | None

    def __post_init__(self) -> None:
        if self.category == OAUTH:
            if self.provider_code != "GOOGLE" or self.environment is not None:
                raise ValueError("OAUTH secrets are Google and have no environment")
        elif self.category in (PROVIDER_CREDENTIAL, ACCOUNT_IDENTIFIER):
            if self.provider_code not in ("KIS", "TOSS", "BINANCE"):
                raise ValueError("provider secrets belong to KIS, TOSS or BINANCE")
            if self.environment not in ("PAPER", "LIVE"):
                raise ValueError("provider secrets are PAPER or LIVE")
        else:
            raise ValueError("unknown secret category")


def parse_reference(reference: str) -> str:
    """The logical name a reference names, or a refusal.

    One form only. A resolver that accepted several would eventually be given
    one that means something slightly different somewhere else.
    """
    match = REFERENCE.fullmatch(reference)
    if match is None:
        raise SecretReferenceError("use secret://db/<logical-name>@active")
    return match.group("name")


def secret_aad(
    *, logical_name: str, version: int, scope: SecretScope, schema_version: int
) -> bytes:
    """What the ciphertext is bound to.

    Moving a secret to another name, version, provider or environment changes
    the AAD, so the tag no longer verifies. A row copied sideways in the
    database does not decrypt.
    """
    parts = (
        logical_name,
        str(version),
        scope.provider_code,
        scope.environment or "",
        str(schema_version),
    )
    if any("|" in part for part in parts):
        raise ValueError("AAD fields must not contain the separator")
    return "|".join(parts).encode("utf-8")


class MySqlSecretStore:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], keys: MasterKeyRing
    ) -> None:
        self._sessions = sessions
        self._keys = keys

    async def store(
        self,
        *,
        logical_name: str,
        scope: SecretScope,
        plaintext: str,
        now: datetime,
        activate: bool = True,
    ) -> UUID:
        """Write a new version, and make it the active one.

        Existing versions are never edited. Rotation is a new row and a moved
        activation, so the history of what was in use stays readable.
        """
        _require_name(logical_name)
        moment = require_utc(now)
        async with self._sessions() as session:
            version = await self._next_version(session, logical_name)
            envelope = self._keys.encrypt(
                plaintext=plaintext.encode("utf-8"),
                aad=secret_aad(
                    logical_name=logical_name,
                    version=version,
                    scope=scope,
                    schema_version=AAD_SCHEMA_VERSION,
                ),
            )
            stored = BackofficeSecretVersionRow(
                id=new_uuid7(),
                logical_name=logical_name,
                category=scope.category,
                provider_code=scope.provider_code,
                environment=scope.environment,
                version=version,
                ciphertext=envelope.ciphertext,
                nonce=envelope.nonce,
                aad_schema_version=AAD_SCHEMA_VERSION,
                master_key_version=envelope.master_key_version,
                fingerprint=envelope.fingerprint,
                created_at=moment,
            )
            session.add(stored)
            await session.flush()
            if activate:
                await self._activate(session, stored, moment)
            # Retiring the old activation and starting the new one are one
            # transaction. Between them the secret would resolve to nothing.
            await session.commit()
            return stored.id

    async def resolve(self, reference: str) -> Secret:
        logical_name = parse_reference(reference)
        async with self._sessions() as session:
            row = await session.scalar(
                select(BackofficeSecretVersionRow)
                .join(
                    BackofficeSecretActivationRow,
                    BackofficeSecretActivationRow.secret_version_id
                    == BackofficeSecretVersionRow.id,
                )
                .where(
                    BackofficeSecretActivationRow.logical_name == logical_name,
                    BackofficeSecretActivationRow.active_marker == "ACTIVE",
                )
            )
        if row is None:
            raise SecretNotFoundError(f"no active secret named {logical_name}")
        scope = SecretScope(
            category=row.category,
            provider_code=row.provider_code or "",
            environment=row.environment,
        )
        # The tag is checked here. A ciphertext edited in the database, or a
        # row moved to another name, fails to verify rather than decrypting to
        # something.
        plaintext = self._keys.decrypt(
            envelope=SecretEnvelope(
                ciphertext=row.ciphertext,
                nonce=row.nonce,
                master_key_version=row.master_key_version,
                fingerprint=row.fingerprint,
            ),
            aad=secret_aad(
                logical_name=row.logical_name,
                version=row.version,
                scope=scope,
                schema_version=row.aad_schema_version,
            ),
        )
        return Secret(plaintext.decode("utf-8"))

    async def fingerprint(self, reference: str) -> bytes:
        """What may be shown about a secret: that it is this one, not what it is."""
        logical_name = parse_reference(reference)
        async with self._sessions() as session:
            row = await session.scalar(
                select(BackofficeSecretVersionRow)
                .join(
                    BackofficeSecretActivationRow,
                    BackofficeSecretActivationRow.secret_version_id
                    == BackofficeSecretVersionRow.id,
                )
                .where(
                    BackofficeSecretActivationRow.logical_name == logical_name,
                    BackofficeSecretActivationRow.active_marker == "ACTIVE",
                )
            )
        if row is None:
            raise SecretNotFoundError(f"no active secret named {logical_name}")
        return row.fingerprint

    async def _next_version(self, session: AsyncSession, logical_name: str) -> int:
        current = await session.scalar(
            select(BackofficeSecretVersionRow.version)
            .where(BackofficeSecretVersionRow.logical_name == logical_name)
            .order_by(BackofficeSecretVersionRow.version.desc())
            .limit(1)
            .with_for_update()
        )
        return 1 if current is None else current + 1

    async def _activate(
        self,
        session: AsyncSession,
        stored: BackofficeSecretVersionRow,
        moment: datetime,
    ) -> None:
        previous = await session.scalar(
            select(BackofficeSecretActivationRow)
            .where(
                BackofficeSecretActivationRow.logical_name == stored.logical_name,
                BackofficeSecretActivationRow.active_marker == "ACTIVE",
            )
            .with_for_update()
        )
        if previous is not None:
            previous.deactivated_at = moment
            previous.active_marker = None
            await session.flush()
        session.add(
            BackofficeSecretActivationRow(
                id=new_uuid7(),
                logical_name=stored.logical_name,
                secret_version_id=stored.id,
                previous_activation_id=None if previous is None else previous.id,
                activated_at=moment,
                deactivated_at=None,
                active_marker="ACTIVE",
            )
        )


def _require_name(logical_name: str) -> None:
    if _NAME.fullmatch(logical_name) is None:
        raise SecretReferenceError("a logical secret name is lowercase and short")


__all__ = (
    "AAD_SCHEMA_VERSION",
    "ACCOUNT_IDENTIFIER",
    "OAUTH",
    "PROVIDER_CREDENTIAL",
    "REFERENCE",
    "MySqlSecretStore",
    "Secret",
    "SecretNotFoundError",
    "SecretReferenceError",
    "SecretScope",
    "parse_reference",
    "secret_aad",
)
