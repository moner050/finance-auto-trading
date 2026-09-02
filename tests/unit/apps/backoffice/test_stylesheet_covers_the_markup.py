"""Every class the templates use has a rule behind it.

`2f5c3f0` collapsed eight per-screen style blocks into one shared layout, and
`state`, `running` and `halted` did not survive the move. Nothing failed: the
markup kept emitting them and the browser kept ignoring them, so the single
most important line on the operations console - whether this account is
permitted to trade - rendered as unstyled body text for as long as nobody
looked at that screen with fresh eyes.

That is the same shape as the defects the strategy audit keeps finding: one
side written, the other side missing, and no test between them. This is the
test between them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "autotrader"
    / "apps"
    / "backoffice"
    / "templates"
)
LAYOUT = TEMPLATES / "_layout.html"

# Emitted by the browser or by script, not by a rule of ours.
NOT_OURS = frozenset({"has-dialog", "no-dialog"})

_STATIC_CLASS = re.compile(r'class="([^"{}]*)"')
# `class="state {{ 'running' if ... else 'halted' }}"` - the names are inside
# the expression, and those are exactly the ones that went missing.
_QUOTED_IN_EXPRESSION = re.compile(r"class=\"[^\"]*\{\{[^\"]*\}\}[^\"]*\"")
_LITERAL = re.compile(r"'([a-z][a-z0-9-]*)'")


def _used() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for template in sorted(TEMPLATES.glob("*.html")):
        names: set[str] = set()
        text = template.read_text(encoding="utf-8")
        for value in _STATIC_CLASS.findall(text):
            names.update(value.split())
        for attribute in _QUOTED_IN_EXPRESSION.findall(text):
            # The literal strings a conditional can choose between.
            names.update(_LITERAL.findall(attribute))
            # And any plain words sitting outside the expression.
            names.update(re.sub(r"\{\{.*?\}\}", " ", attribute)[7:-1].split())
        found[template.name] = {name for name in names if name} - NOT_OURS
    return found


def _defined() -> set[str]:
    stylesheet = LAYOUT.read_text(encoding="utf-8")
    body = stylesheet[stylesheet.index("<style>") : stylesheet.index("</style>")]
    return set(re.findall(r"\.([a-z][a-z0-9-]*)", body))


@pytest.mark.parametrize("template", sorted(_used()))
def test_every_class_the_markup_uses_is_styled(template: str) -> None:
    missing = sorted(_used()[template] - _defined())

    assert not missing, f"{template} uses unstyled classes: {missing}"


def test_the_trading_state_banner_is_styled() -> None:
    """Named on its own, because it is the one this test was written for and
    a future consolidation would otherwise silently take it again."""
    defined = _defined()

    for name in ("state", "running", "halted"):
        assert name in defined, name


def test_the_check_would_notice_a_missing_rule() -> None:
    """A test that reads a stylesheet can pass by finding nothing to check.
    This says the reader actually reads."""
    assert len(_defined()) > 30
    assert "safety" in _defined()
    assert "nonexistent-class-name" not in _defined()
