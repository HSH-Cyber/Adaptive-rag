"""Providers must not require an already-imported chat service to load."""

import subprocess
import sys


def test_gateway_imports_in_a_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", "from app.providers.business_gateway import DemoBusinessGateway"],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
