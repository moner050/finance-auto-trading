"""The backoffice application, which cannot be built without an identity.

Every route hangs off one dependency that resolves an operator or refuses.
There is no unauthenticated branch to forget about: the dashboard, and
anything added beside it, gets the operator or never runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from autotrader.apps.backoffice.commands import (
    MySqlSafetyControls,
    SafetyAction,
    new_command,
)
from autotrader.apps.backoffice.read_model import OperationsReadModel, OperationsView
from autotrader.shared.ids import new_uuid7

TEMPLATES = Path(__file__).resolve().parent / "templates"
LOGIN_PATH = "/auth/login"


class OperatorRequired(Exception):
    """Raised by the dependency when nobody is signed in."""


def create_app(
    *,
    config: BackofficeConfig,
    sessions: async_sessionmaker[AsyncSession],
    store: SessionStore,
    provider: IdentityProvider,
    account_id: UUID,
) -> FastAPI:
    """Build the application, or raise rather than serve anonymously."""
    if type(config) is not BackofficeConfig:
        raise IdentityUnavailableError("an exact BackofficeConfig is required")
    for name, dependency in (
        ("store", store),
        ("provider", provider),
        ("sessions", sessions),
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
                source_ip=request.client.host if request.client else None,
                correlation_id=request.headers.get("x-request-id", str(new_uuid7())),
                requested_at=datetime.now(UTC),
            )
        )
        # Rendered from what was committed, read back, rather than from what
        # the handler believes it just did.
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
    return app


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
