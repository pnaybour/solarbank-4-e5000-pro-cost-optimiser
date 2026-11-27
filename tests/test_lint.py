"""The source has no unused imports, undefined names or redefinitions (GitHub 45): ruff's pyflakes rules, when ruff is installed."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff is not installed here")
def test_source_passes_the_pyflakes_rules():
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(["ruff", "check", "--select", "F", str(root / "src")], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout[-2000:]
