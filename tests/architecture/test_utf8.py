import subprocess
import sys
from pathlib import Path


def test_all_tracked_text_files_are_utf8():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/check-utf8.py"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("UTF8_VALID=")
