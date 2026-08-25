from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src"
FORBIDDEN_MODULE_PREFIXES = (
    "autotrader.integrations.brokers.toss",
    "autotrader.integrations.brokers.kis",
)
UNWIRED_TOSS_WRITE_MODULES = (
    "autotrader.integrations.brokers.toss.submit_recovery",
    "autotrader.integrations.brokers.toss.write_transport",
)


def test_real_broker_import_detector_resolves_relative_imports() -> None:
    source = SOURCE_ROOT / "autotrader" / "execution" / "orders" / "service.py"
    tree = ast.parse("from ...integrations.brokers.kis import adapter")

    assert _real_broker_imports(source, tree) == (
        "autotrader.integrations.brokers.kis.adapter",
    )


def test_execution_core_never_imports_a_real_broker() -> None:
    violations = {
        source.relative_to(ROOT): imports
        for source in (SOURCE_ROOT / "autotrader" / "execution").rglob("*.py")
        if (
            imports := _real_broker_imports(
                source,
                ast.parse(source.read_text(encoding="utf-8"), filename=str(source)),
            )
        )
    }

    assert violations == {}


def test_strategy_core_never_imports_a_real_broker() -> None:
    violations = {
        source.relative_to(ROOT): imports
        for source in (SOURCE_ROOT / "autotrader" / "strategies").rglob("*.py")
        if (
            imports := _real_broker_imports(
                source,
                ast.parse(source.read_text(encoding="utf-8"), filename=str(source)),
            )
        )
    }

    assert violations == {}


def test_toss_recovery_is_composed_only_by_writer_and_persistence_store() -> None:
    violations = {
        source.relative_to(ROOT): imports
        for source in SOURCE_ROOT.rglob("*.py")
        if (
            imports := tuple(
                imported
                for imported in _all_imports(
                    source,
                    ast.parse(source.read_text(encoding="utf-8"), filename=str(source)),
                )
                if imported in UNWIRED_TOSS_WRITE_MODULES
                or any(
                    imported.startswith(f"{module}.")
                    for module in UNWIRED_TOSS_WRITE_MODULES
                )
            )
        )
    }

    assert violations == {
        Path("src/autotrader/integrations/brokers/toss/us_cash_writer.py"): (
            "autotrader.integrations.brokers.toss.submit_recovery.TossPostSendFailure",
            "autotrader.integrations.brokers.toss.submit_recovery.TossPreSendFailure",
            "autotrader.integrations.brokers.toss.submit_recovery.TossRecoveryRecord",
            "autotrader.integrations.brokers.toss.submit_recovery.TossRecoveryState",
            "autotrader.integrations.brokers.toss.submit_recovery.TossRecoveryStore",
            (
                "autotrader.integrations.brokers.toss.submit_recovery."
                "canonical_toss_request_digest"
            ),
        ),
        Path(
            "src/autotrader/persistence/mysql/repositories/toss_us_reconciliation.py"
        ): (
            "autotrader.integrations.brokers.toss.submit_recovery.TossRecoveryClaim",
            "autotrader.integrations.brokers.toss.submit_recovery.TossRecoveryRecord",
            "autotrader.integrations.brokers.toss.submit_recovery.TossRecoveryState",
        ),
    }


def _real_broker_imports(source: Path, tree: ast.AST) -> tuple[str, ...]:
    return tuple(
        imported
        for imported in _all_imports(source, tree)
        if any(
            imported == forbidden or imported.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_MODULE_PREFIXES
        )
    )


def _all_imports(source: Path, tree: ast.AST) -> tuple[str, ...]:
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import_from(source, node)
            if resolved is not None:
                imports.extend(
                    f"{resolved}.{alias.name}" if resolved else alias.name
                    for alias in node.names
                )
    return tuple(imports)


def _resolve_import_from(source: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    relative = source.relative_to(SOURCE_ROOT).with_suffix("").parts
    package = relative[:-1]
    if node.level > len(package):
        return None
    base = package[: len(package) - node.level + 1]
    return ".".join((*base, *(node.module or "").split(".")))
