"""Run a deterministic partition of pytest items on an isolated CI runner.

Usage: python scripts/ci/pytest_shard.py INDEX COUNT [pytest arguments ...]
"""
import hashlib
import sys
from pathlib import Path

import pytest


class Shard:
    def __init__(self, index, count):
        if count < 1 or not 0 <= index < count:
            raise ValueError("require COUNT > 0 and 0 <= INDEX < COUNT")
        self.index, self.count = index, count

    def owns(self, nodeid):
        return int.from_bytes(hashlib.sha256(nodeid.encode()).digest(), "big") % self.count == self.index

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        selected, deselected = [], []
        for item in items:
            (selected if self.owns(item.nodeid) else deselected).append(item)
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    try:
        shard = Shard(int(sys.argv[1]), int(sys.argv[2]))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    raise SystemExit(pytest.main(sys.argv[3:], plugins=[shard]))
