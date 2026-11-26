"""Pytest adapter around the stdlib-only worker integration check."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_worker_smoke_script_passes() -> None:
    script = Path(__file__).with_name("worker_smoke.py")
    result = subprocess.run(
        [sys.executable, "-I", str(script)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
