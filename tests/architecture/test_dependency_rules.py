import subprocess
import sys
from pathlib import Path


def test_domain_boundaries_have_no_forbidden_infrastructure_imports():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/check-import-boundaries.py"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == "IMPORT_BOUNDARIES_VALID=1\n"
