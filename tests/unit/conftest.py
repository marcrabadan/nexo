import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def spanish_by_default():
    from nexo import errors
    errors.set_lang("es")
    yield
    errors.set_lang("es")
