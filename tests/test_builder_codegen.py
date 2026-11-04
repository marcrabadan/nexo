"""The builder must emit components accepted by the real web compiler."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nexo.web_frontend import compile_web

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which('node') is None, reason='node is needed for the builder generator')
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('mode', [None, 'light', 'dark'])
def test_builder_output_compiles(empty, mode):
    script = """
const {generate, tags, defaultDesignSystem} = require('./editors/vscode/builder/model');
const elements = tags.map((tag, index) => ({
  id: 'n' + index, tag, text: 'Hello "Nexo"\\nNext line',
  x: 20, y: index * 60, width: 240, height: 48,
  color: '#172033', background: '#ffffff'
}));
const model = {version: 1, elements: EMPTY ? [] : elements};
const mode = MODE;
if (mode) { model.designSystem = defaultDesignSystem(); model.designSystem.mode = mode; for (const item of elements) item.useTheme = true; }
console.log(generate(model));
""".replace('EMPTY', json.dumps(empty)).replace('MODE', json.dumps(mode))
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, check=True, timeout=20)
    js, css, exports = compile_web(result.stdout)
    assert exports == ['App']
    assert 'nexo-canvas' in css
    assert 'App' in js
