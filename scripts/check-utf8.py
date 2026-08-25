"""Reject non-UTF-8 text files in the repository working tree."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".json", ".toml", ".yml", ".yaml", ".ps1", ".sh", ".txt"}
TEXT_NAMES = {".gitignore", ".gitattributes", ".python-version"}
EXCLUDED_PARTS = {".git", ".venv", "build", "__pycache__"}


def main() -> None:
    checked = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or EXCLUDED_PARTS.intersection(path.parts):
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            print(f"UTF8_INVALID: {path.relative_to(ROOT)}: {error}", file=sys.stderr)
            raise SystemExit(1) from error
        checked += 1
    print(f"UTF8_VALID={checked}")


if __name__ == "__main__":
    main()
