"""The VS Code extension (editors/vscode): its grammar, activation and the
server tests, when node and its packages are installed (`npm install`)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "editors" / "vscode"


@pytest.mark.skipif(shutil.which("node") is None or not (EXT / "node_modules").exists(),
                    reason="node and `npm install` in editors/vscode are needed")
@pytest.mark.parametrize("script", ["grammar", "extension"])
def test_extension(script):
    proc = subprocess.run(["node", f"test/{script}.test.js"], cwd=EXT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(shutil.which("node") is None or not (EXT / "node_modules").exists(),
                    reason="node and `npm install` in editors/vscode are needed")
def test_server_over_vscode_jsonrpc_with_phase_0():
    nexo = ROOT / ".venv" / "bin" / "nexo"
    if not nexo.exists():
        pytest.skip("the phase 0 virtual environment is needed")
    proc = subprocess.run(["node", "test/server.test.js"], cwd=EXT, capture_output=True, text=True, timeout=300,
                          env=dict(os.environ, NEXO=str(nexo)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
