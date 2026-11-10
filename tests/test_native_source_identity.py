"""Run both payloads to verify the immutable native source identity."""

from pathlib import Path
import platform
import subprocess  # nosec B404

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "06ea49d9a487a72c22419b5a8d5c22c11f0d7485"


@pytest.mark.packaged
@pytest.mark.skipif(platform.system() != "Linux", reason="Linux native payloads")
@pytest.mark.parametrize("target", ["linux-amd64", "linux-arm64"])
def test_native_version_is_reviewed_source_and_supports_offline_startup(target):
    completed = subprocess.run(  # nosec B603: fixed packaged executable and arguments.
        [str(ROOT / "bin" / target / "toolbox"), "--version", "--disable-version-check"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    expected = "toolbox version 1.16.0+flowsteward.dataset-connectors."
    assert completed.stdout.strip() == expected + target.replace("-", ".") + "." + COMMIT
