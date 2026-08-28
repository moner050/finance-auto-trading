"""The backoffice application, which cannot be built without an identity.

Every route hangs off one dependency that resolves an operator or refuses.
There is no unauthenticated branch to forget about: the dashboard, and
anything added beside it, gets the operator or never runs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.account_commands import (
    EnableFacts,
    MySqlAccountCommands,
    ProviderBindingFacts,
)
from autotrader.apps.backoffice.account_commands import (
    binding_approval_for as provider_approval_for,
)
from autotrader.apps.backoffice.account_commands import (
    enable_approval_for as account_enable_approval_for,
)
from autotrader.apps.backoffice.accounts_read_model import AccountsReadModel
from autotrader.apps.backoffice.auth import (
    SESSION_COOKIE,
    SESSION_LIFETIME,
    SESSION_PATH,
    BackofficeConfig,
    IdentityProvider,
    IdentityUnavailableError,
    Session,
    SessionStore,
    admitted_operator,
    new_login_attempt,
    require_csrf,
)
from autotrader.apps.backoffice.binding_commands import (
    BindingFacts,
    MySqlBindingCommands,
    new_binding_command,
)
from autotrader.apps.backoffice.binding_commands import (
    approval_for as binding_approval_for,
)
from autotrader.apps.backoffice.commands import (
    MySqlSafetyControls,
    SafetyAction,
    new_command,
)
from autotrader.apps.backoffice.evidence_read_model import EvidenceReadModel
from autotrader.apps.backoffice.exposure import (
    DangerousAction,
    MySqlExposureControls,
    approval_for,
    new_exposure_command,
)
from autotrader.apps.backoffice.ledger import SourceAddressUnknownError
from autotrader.apps.backoffice.policies_read_model import PoliciesReadModel
from autotrader.apps.backoffice.policy_commands import (
    MySqlPolicyCommands,
    PolicyFacts,
    new_create_command,
    new_policy_command,
)
from autotrader.apps.backoffice.policy_commands import (
    approval_for as policy_approval_for,
)
from autotrader.apps.backoffice.promotion_read_model import PromotionReadModel
from autotrader.apps.backoffice.read_model import OperationsReadModel, OperationsView
from autotrader.apps.backoffice.second_password import (
    ApprovalStore,
    MySqlSecondPasswords,
    check_password,
)
from autotrader.apps.backoffice.secret_commands import (
    MySqlSecretCommands,
    SecretAction,
    SecretFacts,
    new_secret_command,
)
from autotrader.apps.backoffice.secret_commands import (
    approval_for as secret_approval_for,
)
from autotrader.apps.backoffice.secrets import (
    OAUTH,
    PROVIDER_CREDENTIAL,
    MySqlSecretStore,
    SecretReferenceError,
    SecretScope,
)
from autotrader.execution.promotion.models import PromotionMode
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
from autotrader.persistence.mysql.repositories.promotion import PromotionSessions
from autotrader.security.secret_crypto import MasterKeyRing
from autotrader.shared.ids import new_uuid7

TEMPLATES = Path(__file__).resolve().parent / "templates"
LOGIN_PATH = "/auth/login"
# One message for a wrong password, wherever it is typed.
PASSWORD_MISMATCH = "2차 비밀번호가 일치하지 않습니다."


class OperatorRequired(Exception):
    """Raised by the dependency when nobody is signed in."""


def create_app(
    *,
    config: BackofficeConfig,
    sessions: async_sessionmaker[AsyncSession],
    store: SessionStore,
    provider: IdentityProvider,
    approvals: ApprovalStore,
    account_id: UUID,
    keys: MasterKeyRing | None = None,
) -> FastAPI:
    """Build the application, or raise rather than serve anonymously."""
    if type(config) is not BackofficeConfig:
        raise IdentityUnavailableError("an exact BackofficeConfig is required")
    for name, dependency in (
        ("store", store),
        ("provider", provider),
        ("sessions", sessions),
        ("approvals", approvals),
    ):
        # Checked at build time rather than at first request. A server that
        # accepts a connection and only then finds it has no session store has
        # already exposed the port.
        if cast(object, dependency) is None:
            raise IdentityUnavailableError(f"{name} is required")

    app = FastAPI(title="Autotrader Backoffice", docs_url=None, redoc_url=None)
    app.state.session_store = store
    templates = Jinja2Templates(directory=str(TEMPLATES))
    controls = MySqlSafetyControls(sessions)
    passwords = MySqlSecondPasswords(sessions)
    account_reader = None if keys is None else AccountsReadModel(sessions, keys)
    secret_store = None if keys is None else MySqlSecretStore(sessions, keys)
    policy_reader = PoliciesReadModel(sessions)
    evidence_reader = EvidenceReadModel(sessions)
    promotion_reader = PromotionReadModel(sessions)
    policy_commands = MySqlPolicyCommands(sessions=sessions, approvals=approvals)
    binding_commands = MySqlBindingCommands(sessions=sessions, approvals=approvals)
    account_commands = MySqlAccountCommands(sessions=sessions, approvals=approvals)
    secret_commands = (
        None
        if secret_store is None
        else MySqlSecretCommands(
            sessions=sessions, store=secret_store, approvals=approvals
        )
    )
    exposure = MySqlExposureControls(
        sessions=sessions, approvals=approvals, account_id=account_id
    )

    async def _sign_in(request: Request, error: Exception) -> Response:
        del request, error
        return RedirectResponse(LOGIN_PATH, status_code=303)

    async def health() -> dict[str, str]:
        """Liveness only. It names no account and reads no table."""
        return {"status": "ok"}

    async def login() -> Response:
        attempt = new_login_attempt()
        await store.begin_login(attempt)
        return RedirectResponse(provider.authorization_url(attempt), status_code=303)

    async def callback(code: str, state: str) -> Response:
        # The state is spent here whatever happens next, so a replayed
        # callback finds nothing to redeem.
        attempt = await store.take_login(state)
        if attempt is None:
            raise IdentityUnavailableError("identity rejected")
        operator = admitted_operator(
            await provider.verify(code=code, attempt=attempt), config=config
        )
        session_id = await store.create_session(operator)
        response = RedirectResponse("/", status_code=303)
        _set_session_cookie(response, session_id, config=config)
        return response

    async def logout(request: Request) -> Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id is not None:
            await store.end_session(session_id)
        response = RedirectResponse(LOGIN_PATH, status_code=303)
        response.delete_cookie(SESSION_COOKIE, path=SESSION_PATH)
        return response

    async def dashboard(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Response:
        return await _render(request, session, templates, sessions, account_id)

    async def control(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        action: str = Form(...),
        csrf_token: str = Form(...),
    ) -> Response:
        # The token first, and nothing else before it. Section 12 allows this
        # path to depend on authentication and the form token, and on nothing
        # that could be unavailable at the moment it is needed.
        require_csrf(session, csrf_token)
        try:
            requested = SafetyAction(action)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="unknown action") from error
        await controls.apply(
            new_command(
                action=requested,
                operator=session.operator,
                source_ip=_source_ip(request),
                correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
                requested_at=datetime.now(UTC),
            )
        )
        # Rendered from what was committed, read back, rather than from what
        # the handler believes it just did.
        return await _render(request, session, templates, sessions, account_id)

    async def accounts(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Response:
        return await _render_accounts(request, session, templates, account_reader)

    async def secrets_page(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Response:
        return await _render_secrets(request, session, templates, secret_store)

    async def register_secret(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        logical_name: str = Form(...),
        provider: str = Form(...),
        environment: str = Form(""),
        plaintext: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        if secret_store is None:
            raise HTTPException(status_code=503, detail="secrets are unavailable")
        try:
            await secret_store.store(
                logical_name=logical_name.strip(),
                scope=SecretScope(
                    category=OAUTH if provider == "GOOGLE" else PROVIDER_CREDENTIAL,
                    provider_code=provider,
                    environment=environment or None,
                ),
                plaintext=plaintext,
                now=datetime.now(UTC),
                # Stored, not used. Putting it into use is a separate decision
                # and takes the second password.
                activate=False,
            )
        except (ValueError, SecretReferenceError) as error:
            return await _render_secrets(
                request, session, templates, secret_store, error=str(error)
            )
        finally:
            # The plaintext goes no further than this call, and nothing that
            # renders the page can reach it.
            del plaintext
        return await _render_secrets(
            request,
            session,
            templates,
            secret_store,
            registered=logical_name.strip(),
        )

    async def approve_secret(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        action: str = Form(...),
        logical_name: str = Form(...),
        target_version: int | None = Form(None),
        second_password: str | None = Form(None),
    ) -> Response:
        require_csrf(session, csrf_token)
        commands = _require_secret_commands(secret_commands)
        facts = await commands.facts(
            action=SecretAction(action),
            logical_name=logical_name,
            target_version=target_version,
        )
        if second_password is None:
            # First press: show what will change, so the password is typed
            # against exactly that.
            return await _render_secrets(
                request, session, templates, secret_store, facts=facts
            )
        session_id = _session_id(request)
        source_ip = _source_ip(request)
        await approvals.require_attempts_left(
            session_id=session_id, source_ip=source_ip
        )
        verifier = await passwords.active()
        if not check_password(verifier, second_password):
            await approvals.record_failure(session_id=session_id, source_ip=source_ip)
            return await _render_secrets(
                request,
                session,
                templates,
                secret_store,
                facts=facts,
                error=PASSWORD_MISMATCH,
            )
        await approvals.clear_failures(session_id=session_id, source_ip=source_ip)
        approval_id = await approvals.issue(
            secret_approval_for(
                session_id=session_id, operator=session.operator, facts=facts
            )
        )
        return await _render_secrets(
            request,
            session,
            templates,
            secret_store,
            facts=facts,
            approval_id=approval_id,
        )

    async def apply_secret(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        action: str = Form(...),
        logical_name: str = Form(...),
        approval_id: str = Form(...),
        target_version: int | None = Form(None),
    ) -> Response:
        require_csrf(session, csrf_token)
        commands = _require_secret_commands(secret_commands)
        await commands.apply(
            new_secret_command(
                action=SecretAction(action),
                logical_name=logical_name,
                target_version=target_version,
                operator=session.operator,
                source_ip=_source_ip(request),
                correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
                approval_id=approval_id,
                requested_at=datetime.now(UTC),
            ),
            session_id=_session_id(request),
        )
        return await _render_secrets(request, session, templates, secret_store)

    async def create_account(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        broker_code: str = Form(...),
        account_alias: str = Form(...),
        environment: str = Form(...),
        secret_reference: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        # Section 9 gates enablement, not creation; the row this writes is
        # disabled and cannot trade.
        await account_commands.create(
            broker_code=broker_code,
            account_alias=account_alias,
            environment=environment,
            secret_reference=secret_reference,
            operator=session.operator,
            source_ip=_source_ip(request),
            correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
            now=datetime.now(UTC),
        )
        return await _render_accounts(request, session, templates, account_reader)

    async def disable_account(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        account_id: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        # No second password, for the same reason HALT has none: taking an
        # account out of service must work when the approval path does not.
        await account_commands.set_enabled(
            account_id=_uuid(account_id, "unknown account"),
            enabled=False,
            operator=session.operator,
            source_ip=_source_ip(request),
            correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
            approval_id=None,
            session_id=_session_id(request),
            now=datetime.now(UTC),
        )
        return await _render_accounts(request, session, templates, account_reader)

    async def approve_enable_account(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        account_id: str = Form(...),
        second_password: str | None = Form(None),
    ) -> Response:
        require_csrf(session, csrf_token)
        facts = await account_commands.enable_facts(
            _uuid(account_id, "unknown account")
        )
        if second_password is None:
            return await _render_accounts(
                request, session, templates, account_reader, enable=facts
            )
        session_id = _session_id(request)
        source_ip = _source_ip(request)
        await approvals.require_attempts_left(
            session_id=session_id, source_ip=source_ip
        )
        verifier = await passwords.active()
        if not check_password(verifier, second_password):
            await approvals.record_failure(session_id=session_id, source_ip=source_ip)
            return await _render_accounts(
                request,
                session,
                templates,
                account_reader,
                enable=facts,
                error=PASSWORD_MISMATCH,
            )
        await approvals.clear_failures(session_id=session_id, source_ip=source_ip)
        approval_id = await approvals.issue(
            account_enable_approval_for(
                session_id=session_id, operator=session.operator, facts=facts
            )
        )
        return await _render_accounts(
            request,
            session,
            templates,
            account_reader,
            enable=facts,
            approval_id=approval_id,
        )

    async def apply_enable_account(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        account_id: str = Form(...),
        approval_id: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        await account_commands.set_enabled(
            account_id=_uuid(account_id, "unknown account"),
            enabled=True,
            operator=session.operator,
            source_ip=_source_ip(request),
            correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
            approval_id=approval_id,
            session_id=_session_id(request),
            now=datetime.now(UTC),
        )
        return await _render_accounts(request, session, templates, account_reader)

    async def approve_provider_binding(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        account_id: str = Form(...),
        provider_code: str = Form(...),
        account_seq: str = Form(""),
        second_password: str | None = Form(None),
    ) -> Response:
        require_csrf(session, csrf_token)
        facts = await account_commands.binding_facts(
            account_id=_uuid(account_id, "unknown account"),
            provider_code=provider_code,
            account_seq=_account_seq(account_seq),
        )
        if second_password is None:
            return await _render_accounts(
                request, session, templates, account_reader, provider=facts
            )
        session_id = _session_id(request)
        source_ip = _source_ip(request)
        await approvals.require_attempts_left(
            session_id=session_id, source_ip=source_ip
        )
        verifier = await passwords.active()
        if not check_password(verifier, second_password):
            await approvals.record_failure(session_id=session_id, source_ip=source_ip)
            return await _render_accounts(
                request,
                session,
                templates,
                account_reader,
                provider=facts,
                error=PASSWORD_MISMATCH,
            )
        await approvals.clear_failures(session_id=session_id, source_ip=source_ip)
        approval_id = await approvals.issue(
            provider_approval_for(
                session_id=session_id, operator=session.operator, facts=facts
            )
        )
        return await _render_accounts(
            request,
            session,
            templates,
            account_reader,
            provider=facts,
            approval_id=approval_id,
        )

    async def apply_provider_binding(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        account_id: str = Form(...),
        provider_code: str = Form(...),
        account_seq: str = Form(""),
        approval_id: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        await account_commands.bind_provider(
            account_id=_uuid(account_id, "unknown account"),
            provider_code=provider_code,
            account_seq=_account_seq(account_seq),
            operator=session.operator,
            source_ip=_source_ip(request),
            correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
            approval_id=approval_id,
            session_id=_session_id(request),
            now=datetime.now(UTC),
        )
        return await _render_accounts(request, session, templates, account_reader)

    async def promotion_page(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Response:
        return await _render_promotion(request, session, templates, promotion_reader)

    async def claim_session(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        binding_id: str = Form(...),
        mode: str = Form(...),
        exchange_date: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        # Claiming records that a day is being watched. It creates no
        # readiness: only completing does, and only against evidence.
        async with sessions() as store:
            binding = await store.scalar(
                select(ProviderAccountBinding).where(
                    ProviderAccountBinding.id == _uuid(binding_id, "unknown binding")
                )
            )
            if binding is None:
                raise HTTPException(status_code=400, detail="unknown binding")
            manifest = await store.scalar(
                select(DavidV6ManifestRow).order_by(
                    DavidV6ManifestRow.registered_at.desc()
                )
            )
            if manifest is None:
                raise HTTPException(
                    status_code=409, detail="no strategy manifest is registered"
                )
            await PromotionSessions(store).claim(
                binding_id=binding.id,
                account_id=binding.account_id,
                manifest_id=manifest.id,
                mode=_promotion_mode(mode),
                exchange_date=_exchange_date(exchange_date),
                now=datetime.now(UTC),
            )
            await store.commit()
        return await _render_promotion(request, session, templates, promotion_reader)

    async def complete_session(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        session_id: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        moment = datetime.now(UTC)
        async with sessions() as store:
            # The repository counts the evidence and refuses if the manifest
            # does not verify. Nothing the form said is taken as a fact.
            await PromotionSessions(store).complete(
                session_id=_uuid(session_id, "unknown session"),
                now=moment,
                today=moment.date(),
            )
            await store.commit()
        return await _render_promotion(request, session, templates, promotion_reader)

    async def evidence_page(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="evidence.html",
            context={
                "session": session,
                "view": await evidence_reader.load(now=datetime.now(UTC)),
            },
        )

    async def policies_page(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Response:
        return await _render_policies(
            request, session, templates, policy_reader, commands=policy_commands
        )

    async def approve_policy(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        target_version_id: str = Form(...),
        second_password: str | None = Form(None),
    ) -> Response:
        require_csrf(session, csrf_token)
        commands = _require_policy_commands(policy_commands)
        facts = await commands.facts(_version_id(target_version_id))
        if second_password is None:
            # First press: show what moves, so the password is typed against
            # the difference rather than against a version number.
            return await _render_policies(
                request,
                session,
                templates,
                policy_reader,
                commands=policy_commands,
                facts=facts,
            )
        session_id = _session_id(request)
        source_ip = _source_ip(request)
        await approvals.require_attempts_left(
            session_id=session_id, source_ip=source_ip
        )
        verifier = await passwords.active()
        if not check_password(verifier, second_password):
            await approvals.record_failure(session_id=session_id, source_ip=source_ip)
            return await _render_policies(
                request,
                session,
                templates,
                policy_reader,
                commands=policy_commands,
                facts=facts,
                error=PASSWORD_MISMATCH,
            )
        await approvals.clear_failures(session_id=session_id, source_ip=source_ip)
        approval_id = await approvals.issue(
            policy_approval_for(
                session_id=session_id, operator=session.operator, facts=facts
            )
        )
        return await _render_policies(
            request,
            session,
            templates,
            policy_reader,
            commands=policy_commands,
            facts=facts,
            approval_id=approval_id,
        )

    async def apply_policy(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        target_version_id: str = Form(...),
        approval_id: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        commands = _require_policy_commands(policy_commands)
        await commands.activate(
            new_policy_command(
                target_version_id=_version_id(target_version_id),
                operator=session.operator,
                source_ip=_source_ip(request),
                correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
                approval_id=approval_id,
                requested_at=datetime.now(UTC),
            ),
            session_id=_session_id(request),
        )
        return await _render_policies(
            request, session, templates, policy_reader, commands=policy_commands
        )

    async def create_policy_version(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        policy_code: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        commands = _require_policy_commands(policy_commands)
        # Section 9 puts the second password on activation, not on writing an
        # inert row whose every value comes from the approved definition.
        await commands.create(
            new_create_command(
                policy_code=policy_code,
                operator=session.operator,
                source_ip=_source_ip(request),
                correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
                requested_at=datetime.now(UTC),
            )
        )
        return await _render_policies(
            request, session, templates, policy_reader, commands=policy_commands
        )

    async def approve_binding(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        account_id: str = Form(...),
        target_version_id: str = Form(...),
        second_password: str | None = Form(None),
    ) -> Response:
        require_csrf(session, csrf_token)
        facts = await binding_commands.facts(
            account_id=_uuid(account_id, "unknown account"),
            target_version_id=_uuid(target_version_id, "unknown policy version"),
        )
        if second_password is None:
            return await _render_policies(
                request,
                session,
                templates,
                policy_reader,
                commands=policy_commands,
                binding=facts,
            )
        session_id = _session_id(request)
        source_ip = _source_ip(request)
        await approvals.require_attempts_left(
            session_id=session_id, source_ip=source_ip
        )
        verifier = await passwords.active()
        if not check_password(verifier, second_password):
            await approvals.record_failure(session_id=session_id, source_ip=source_ip)
            return await _render_policies(
                request,
                session,
                templates,
                policy_reader,
                commands=policy_commands,
                binding=facts,
                error=PASSWORD_MISMATCH,
            )
        await approvals.clear_failures(session_id=session_id, source_ip=source_ip)
        approval_id = await approvals.issue(
            binding_approval_for(
                session_id=session_id, operator=session.operator, facts=facts
            )
        )
        return await _render_policies(
            request,
            session,
            templates,
            policy_reader,
            commands=policy_commands,
            binding=facts,
            approval_id=approval_id,
        )

    async def apply_binding(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        csrf_token: str = Form(...),
        account_id: str = Form(...),
        target_version_id: str = Form(...),
        approval_id: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        await binding_commands.bind(
            new_binding_command(
                account_id=_uuid(account_id, "unknown account"),
                target_version_id=_uuid(target_version_id, "unknown policy version"),
                operator=session.operator,
                source_ip=_source_ip(request),
                correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
                approval_id=approval_id,
                requested_at=datetime.now(UTC),
            ),
            session_id=_session_id(request),
        )
        return await _render_policies(
            request, session, templates, policy_reader, commands=policy_commands
        )

    async def arming_panel(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="arming.html",
            context={
                "session": session,
                "facts": await exposure.facts(),
                "error": request.query_params.get("error"),
            },
        )

    async def approve(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        action: str = Form(...),
        csrf_token: str = Form(...),
        second_password: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        requested = _dangerous(action)
        session_id = _session_id(request)
        source_ip = _source_ip(request)
        await approvals.require_attempts_left(
            session_id=session_id, source_ip=source_ip
        )
        verifier = await passwords.active()
        if not check_password(verifier, second_password):
            await approvals.record_failure(session_id=session_id, source_ip=source_ip)
            return RedirectResponse("/controls/arm?error=password", status_code=303)
        await approvals.clear_failures(session_id=session_id, source_ip=source_ip)
        facts = await exposure.facts()
        approval_id = await approvals.issue(
            approval_for(
                session_id=session_id,
                operator=session.operator,
                action=requested,
                facts=facts,
            )
        )
        return templates.TemplateResponse(
            request=request,
            name="arming.html",
            context={
                "session": session,
                "facts": facts,
                "approval_id": approval_id,
                "action": requested.value,
                "error": None,
            },
        )

    async def enable(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        action: str = Form(...),
        csrf_token: str = Form(...),
        approval_id: str = Form(...),
    ) -> Response:
        require_csrf(session, csrf_token)
        await exposure.apply(
            new_exposure_command(
                action=_dangerous(action),
                operator=session.operator,
                source_ip=_source_ip(request),
                correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
                approval_id=approval_id,
                requested_at=datetime.now(UTC),
            ),
            session_id=_session_id(request),
        )
        return await _render(request, session, templates, sessions, account_id)

    # Registered rather than decorated: a decorated closure reads as dead
    # code to a type checker, and silencing that on every route is a habit
    # worth not starting.
    app.add_exception_handler(OperatorRequired, _sign_in)
    app.add_api_route("/healthz", health, methods=["GET"])
    app.add_api_route(LOGIN_PATH, login, methods=["GET"])
    app.add_api_route("/auth/callback", callback, methods=["GET"])
    app.add_api_route("/auth/logout", logout, methods=["POST"])
    app.add_api_route("/", dashboard, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(
        "/controls", control, methods=["POST"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/accounts", accounts, methods=["GET"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/secrets", secrets_page, methods=["GET"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/accounts/create",
        create_account,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/accounts/disable",
        disable_account,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/accounts/enable/approve",
        approve_enable_account,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/accounts/enable/apply",
        apply_enable_account,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/accounts/provider/approve",
        approve_provider_binding,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/accounts/provider/apply",
        apply_provider_binding,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/promotion", promotion_page, methods=["GET"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/promotion/claim",
        claim_session,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/promotion/complete",
        complete_session,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/evidence", evidence_page, methods=["GET"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/policies", policies_page, methods=["GET"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/policies/approve",
        approve_policy,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/policies/apply",
        apply_policy,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/policies/create",
        create_policy_version,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/bindings/approve",
        approve_binding,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/bindings/apply",
        apply_binding,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/secrets/register",
        register_secret,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/secrets/approve",
        approve_secret,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/secrets/apply", apply_secret, methods=["POST"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/controls/arm", arming_panel, methods=["GET"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/controls/approve", approve, methods=["POST"], response_class=HTMLResponse
    )
    app.add_api_route(
        "/controls/enable", enable, methods=["POST"], response_class=HTMLResponse
    )
    return app


async def _render_promotion(
    request: Request,
    session: Session,
    templates: Jinja2Templates,
    reader: PromotionReadModel,
    *,
    error: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="promotion.html",
        context={
            "session": session,
            "view": await reader.load(today=datetime.now(UTC).date()),
            "error": error,
        },
    )


def _promotion_mode(value: str) -> PromotionMode:
    try:
        return PromotionMode(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="unknown mode") from error


def _exchange_date(value: str) -> date:
    """A trading day, not a timestamp."""
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="exchange_date must be YYYY-MM-DD"
        ) from error


async def _render_accounts(
    request: Request,
    session: Session,
    templates: Jinja2Templates,
    reader: AccountsReadModel | None,
    *,
    error: str | None = None,
    enable: EnableFacts | None = None,
    provider: ProviderBindingFacts | None = None,
    approval_id: str | None = None,
) -> Response:
    if reader is None:
        # Without a master key nothing here can even name a credential, and a
        # page that renders every value as absent would read as an empty vault
        # rather than as a missing key.
        raise HTTPException(status_code=503, detail="secrets are unavailable")
    return templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context={
            "session": session,
            "view": await reader.load(),
            "error": error,
            "enable": enable,
            "provider": provider,
            "approval_id": approval_id,
        },
    )


async def _render_policies(
    request: Request,
    session: Session,
    templates: Jinja2Templates,
    reader: PoliciesReadModel,
    *,
    commands: MySqlPolicyCommands | None,
    error: str | None = None,
    facts: PolicyFacts | None = None,
    binding: BindingFacts | None = None,
    approval_id: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="policies.html",
        context={
            "session": session,
            "view": await reader.load(),
            "creatable": () if commands is None else await commands.creatable(),
            "error": error,
            "facts": facts,
            "binding": binding,
            "approval_id": approval_id,
        },
    )


def _account_seq(value: str) -> int | None:
    """Blank means absent, which is what KIS and Binance require."""
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="account_seq must be a whole number"
        ) from error


def _uuid(value: str, detail: str) -> UUID:
    """A malformed id is a refusal here, rather than a framework error whose
    message describes the form field."""
    try:
        return UUID(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=detail) from error


def _version_id(value: str) -> UUID:
    return _uuid(value, "unknown policy version")


def _require_policy_commands(
    commands: MySqlPolicyCommands | None,
) -> MySqlPolicyCommands:
    if commands is None:
        raise HTTPException(status_code=503, detail="policies are unavailable")
    return commands


async def _render_secrets(
    request: Request,
    session: Session,
    templates: Jinja2Templates,
    store: MySqlSecretStore | None,
    *,
    registered: str | None = None,
    error: str | None = None,
    facts: SecretFacts | None = None,
    approval_id: str | None = None,
) -> Response:
    if store is None:
        raise HTTPException(status_code=503, detail="secrets are unavailable")
    return templates.TemplateResponse(
        request=request,
        name="secrets.html",
        context={
            "session": session,
            "versions": await store.versions(),
            "registered": registered,
            "error": error,
            "facts": facts,
            "approval_id": approval_id,
        },
    )


def _require_secret_commands(
    commands: MySqlSecretCommands | None,
) -> MySqlSecretCommands:
    if commands is None:
        raise HTTPException(status_code=503, detail="secrets are unavailable")
    return commands


def _dangerous(action: str) -> DangerousAction:
    try:
        return DangerousAction(action)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="unknown action") from error


def _source_ip(request: Request) -> str:
    """Where the command came from, which the audit contract requires.

    A request with no peer address is anomalous for a backoffice reached over
    loopback or a reverse proxy, and recording a placeholder would put a fact
    in the ledger that is not one.
    """
    if request.client is None or not request.client.host:
        raise SourceAddressUnknownError("a command must record where it came from")
    return request.client.host


def _session_id(request: Request) -> str:
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id is None:
        raise OperatorRequired
    return session_id


async def _render(
    request: Request,
    session: Session,
    templates: Jinja2Templates,
    sessions: async_sessionmaker[AsyncSession],
    account_id: UUID,
) -> Response:
    async with sessions() as db:
        view = await OperationsReadModel(db).load(account_id=account_id)
    return templates.TemplateResponse(
        request=request,
        name="operations.html",
        context={"session": session, "view": view},
    )


async def require_session(request: Request) -> Session:
    """Resolve the operator, or refuse.

    Defined at module scope rather than inside the factory: with postponed
    annotations FastAPI resolves a dependency by name against the module, and
    a closure variable is not there to find. Silently, in that case, the
    parameter becomes a query string the caller controls.
    """
    store = cast(SessionStore, request.app.state.session_store)
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id is None:
        raise OperatorRequired
    session = await store.session_for(session_id)
    if session is None:
        # Redis lost the session, or it expired. Either way nobody is signed
        # in, and a cookie is not a second opinion.
        raise OperatorRequired
    return session


def _set_session_cookie(
    response: Response, session_id: str, *, config: BackofficeConfig
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path=SESSION_PATH,
        secure=config.secure_cookie,
        httponly=True,
        samesite="lax",
    )


__all__ = ("LOGIN_PATH", "OperationsView", "OperatorRequired", "create_app")
