import subprocess
import sys
from pathlib import Path


def test_installed_package_imports_outside_repository_root(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", "import autotrader; print(autotrader.__name__)"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )

    assert root.is_dir()
    assert result.returncode == 0
    assert result.stdout == "autotrader\n"
