"""The backoffice application, which cannot be built without an identity.

Every route hangs off one dependency that resolves an operator or refuses.
There is no unauthenticated branch to forget about: the dashboard, and
anything added beside it, gets the operator or never runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
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
    Operator,
    SessionStore,
    admitted_operator,
    new_login_attempt,
)
from autotrader.apps.backoffice.read_model import OperationsReadModel, OperationsView

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
        operator: Annotated[Operator, Depends(require_operator)],
    ) -> Response:
        async with sessions() as session:
            view = await OperationsReadModel(session).load(account_id=account_id)
        return templates.TemplateResponse(
            request=request,
            name="operations.html",
            context={"operator": operator, "view": view},
        )

    # Registered rather than decorated: a decorated closure reads as dead
    # code to a type checker, and silencing that on every route is a habit
    # worth not starting.
    app.add_exception_handler(OperatorRequired, _sign_in)
    app.add_api_route("/healthz", health, methods=["GET"])
    app.add_api_route(LOGIN_PATH, login, methods=["GET"])
    app.add_api_route("/auth/callback", callback, methods=["GET"])
    app.add_api_route("/auth/logout", logout, methods=["POST"])
    app.add_api_route("/", dashboard, methods=["GET"], response_class=HTMLResponse)
    return app


async def require_operator(request: Request) -> Operator:
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
    operator = await store.operator_for(session_id)
    if operator is None:
        # Redis lost the session, or it expired. Either way nobody is signed
        # in, and a cookie is not a second opinion.
        raise OperatorRequired
    return operator


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
