from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_image_does_not_persist_uv_build_cache() -> None:
    dockerfile = (ROOT / "infra" / "docker" / "app.Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "uv sync --frozen --no-cache --no-dev --no-install-project" in dockerfile
    assert "uv sync --frozen --no-cache --no-dev" in dockerfile
    assert "/usr/local/lib/python3.14/site-packages/pip" in dockerfile
    assert "/usr/local/lib/python3.14/ensurepip" in dockerfile
    assert "pip uninstall" not in dockerfile
