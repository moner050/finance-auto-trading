"""Reject infrastructure imports from domain and application boundaries."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ("application", "strategies", "risk", "execution")
FORBIDDEN = (
    "sqlalchemy",
    "redis",
    "httpx",
    "websockets",
    "autotrader.persistence",
    "autotrader.integrations",
    "autotrader.apps",
)


def is_forbidden(name: str, *, path: Path) -> bool:
    del path
    return any(
        name == forbidden or name.startswith(f"{forbidden}.") for forbidden in FORBIDDEN
    )


def main() -> None:
    violations: list[str] = []
    for boundary in BOUNDARIES:
        directory = ROOT / "src" / "autotrader" / boundary
        for path in directory.rglob("*.py") if directory.is_dir() else ():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = (
                    [node.module]
                    if isinstance(node, ast.ImportFrom) and node.module
                    else []
                )
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                for name in names:
                    if is_forbidden(name, path=path):
                        violations.append(f"{path.relative_to(ROOT)} imports {name}")
    if violations:
        print("\n".join(violations), file=sys.stderr)
        raise SystemExit(1)
    print("IMPORT_BOUNDARIES_VALID=1")


if __name__ == "__main__":
    main()
