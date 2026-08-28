"""What the deployment exposes, and what waits for what.

Phase 6 asks for one property above the others: Caddy publishes 80 and 443 and
nothing else is reachable from outside the host. That is a claim about a file
nobody type-checks, so it is checked here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "compose" / "compose.yaml"
PUBLIC = "caddy"


def _compose() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))


def _services() -> dict[str, dict[str, Any]]:
    return cast("dict[str, dict[str, Any]]", _compose()["services"])


def test_only_the_proxy_publishes_a_public_port() -> None:
    """Every other service reaches the network it needs through the compose
    network, which has no route in."""
    exposed = {
        name: service["ports"]
        for name, service in _services().items()
        if service.get("ports")
        and any(not str(port).startswith("127.0.0.1:") for port in service["ports"])
    }

    assert set(exposed) == {PUBLIC}


def test_the_proxy_publishes_only_http_and_https() -> None:
    ports = {str(port) for port in _services()[PUBLIC]["ports"]}

    assert ports == {"80:80", "443:443"}


def test_a_bundled_datastore_is_bound_to_loopback() -> None:
    """They exist for local work. Reaching one from another host would make
    the compose network's boundary decorative."""
    for name in ("mysql", "redis"):
        for port in _services()[name]["ports"]:
            assert str(port).startswith("127.0.0.1:"), name


def test_the_datastores_are_opt_in() -> None:
    """This project already runs against an external database, and starting a
    second one by default is how two end up half populated."""
    for name in ("mysql", "redis"):
        assert _services()[name].get("profiles") == ["local-data"]


@pytest.mark.parametrize("name", ("backoffice", "capture"))
def test_nothing_starts_before_the_migration_succeeds(name: str) -> None:
    """A back office against an unmigrated database renders an empty vault
    rather than refusing, which is the failure this ordering exists to stop."""
    depends = _services()[name]["depends_on"]

    assert depends["migrate"]["condition"] == "service_completed_successfully"


def test_the_migration_does_not_restart() -> None:
    """It either brought the schema to head or it did not; running it again on
    a loop would hide which."""
    assert _services()["migrate"]["restart"] == "no"


def test_the_backoffice_binds_every_interface_on_purpose() -> None:
    """Caddy reaches it across the container network. The default is loopback,
    so this has to be said rather than inherited."""
    environment = _services()["backoffice"]["environment"]

    assert environment["BACKOFFICE_BIND_HOST"] == "0.0.0.0"


def test_the_public_url_is_a_domain_not_a_bind_address() -> None:
    """They were the same value once, and a container cannot bind a domain."""
    environment = _services()["backoffice"]["environment"]

    assert environment["BACKOFFICE_PUBLIC_URL"].startswith("https://")
    assert environment["BACKOFFICE_PUBLIC_URL"] != environment["BACKOFFICE_BIND_HOST"]


def test_the_readiness_report_is_not_a_service_that_runs() -> None:
    """The loop has inputs with no producer. A container that restarted this
    forever would look like a worker that is working."""
    check = _services()["trader-check"]

    assert check["profiles"] == ["check"]
    assert check["restart"] == "no"


def test_every_application_service_is_on_the_private_network() -> None:
    application = ("migrate", "backoffice", "capture", "trader-check")
    for name in application:
        assert _services()[name].get("networks") == ["private"], name
