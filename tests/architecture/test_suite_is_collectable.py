"""The whole suite has to be runnable in one command.

Most of `tests/` is not a package, so pytest names a module by its basename
alone. Two files called the same thing in different directories are then the
same module, and collecting both is an error that stops the run before a
single test executes.

That is worse than it sounds, because it fails at collection rather than at a
test. Running a subdirectory works fine, so the suite passes every time
anybody looks at it, and `pytest tests` - which is what a CI job runs - stops
on an import error. Five basenames had collided; the whole suite had never
run in one command, and "all tests pass" had only ever been true one
directory at a time.

Naming is the fix rather than adding `__init__.py` everywhere: the integration
tests reach their helpers with `from conftest import ...`, which works because
pytest puts the conftest directory on the path, and making every directory a
package changes how that resolves.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]


def test_no_two_test_modules_share_a_basename() -> None:
    by_name: defaultdict[str, list[str]] = defaultdict(list)
    for path in TESTS.rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue
        by_name[path.name].append(str(path.relative_to(TESTS)))

    collisions = {
        name: sorted(paths) for name, paths in by_name.items() if len(paths) > 1
    }

    assert not collisions, (
        "these basenames resolve to one module and stop collection: "
        + "; ".join(
            f"{name}: {', '.join(paths)}" for name, paths in sorted(collisions.items())
        )
    )
