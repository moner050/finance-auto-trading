"""Wiring a broker into the Shadow loop has to be a visible edit.

The account this runs against is LIVE and its key can place futures orders, so
what stops a Shadow session trading is worth stating exactly.

It is `RefusingExecution`: `run_tick` submits through the execution port and
nowhere else, and that port has no broker, no credentials, and returns None.
The unit tests pin that. What this file adds is that the module cannot acquire
one by accident - a submitter or a credential store has to appear as a new
import here, where a reader will see it.

A first version of this test walked the whole import graph and asserted no
broker code was reachable. That failed, correctly: `composition.py` holds the
ports this needs and also the paper submitter. The lesson is that reachability
is the wrong claim - the paper submitter being importable somewhere in the
process has never been what shadow mode means, and asserting it would have
meant either a false sense of safety or a refactor performed to satisfy a
test rather than a requirement. The claim is narrowed to what is true and
still useful.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
SHADOW = SRC / "autotrader" / "apps" / "trader" / "shadow.py"

# Anything that can reach a venue, or unlock what would be needed to.
FORBIDDEN_PREFIXES = (
    "autotrader.integrations.brokers",
    "autotrader.execution.dispatch",
    "autotrader.apps.backoffice.provider_secrets",
    "autotrader.apps.backoffice.credentials",
    "autotrader.security.secret_crypto",
)


def _direct_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_the_shadow_module_imports_no_broker_and_no_credential() -> None:
    offending = sorted(
        module
        for module in _direct_imports(SHADOW)
        if module.startswith(FORBIDDEN_PREFIXES)
    )

    assert not offending, (
        "the shadow loop imports code that talks to a venue: " + ", ".join(offending)
    )


def test_the_guard_would_notice() -> None:
    """A test that can only pass is not a guard. The paper composition does
    import a submitter, so the same check over it must find one."""
    offending = [
        module
        for module in _direct_imports(
            SRC / "autotrader" / "apps" / "trader" / "binance_paper.py"
        )
        if module.startswith(FORBIDDEN_PREFIXES)
    ]

    assert offending


def test_run_tick_submits_through_the_execution_port_and_nowhere_else() -> None:
    """The narrowed claim above only holds because there is one way out. If a
    second call to something submitting appeared in the tick, the execution
    port would stop being the whole of the answer."""
    tick = (SRC / "autotrader" / "apps" / "trader" / "tick.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(tick)

    awaited = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "submit" in awaited
    assert not {"place", "send_order", "dispatch"} & awaited
