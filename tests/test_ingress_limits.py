"""Verify kernel limits on a live payload, not only CLI flag acceptance."""
from pathlib import Path
import pytest
resource = pytest.importorskip("resource")
from test_security_ingress import backend, serving, raw


def test_loopback_payload_has_real_hard_limits(tmp_path, backend):
    with serving(tmp_path, backend) as (process, port, _):
        pid = process.pid
        # Python delegates to a native launcher; that launcher owns one payload.
        children = {}
        for status in Path("/proc").glob("[0-9]*/stat"):
            try:
                # comm is parenthesized and may contain spaces or parentheses.
                fields = status.read_text().rsplit(")", 1)[1].split()
                children.setdefault(int(fields[1]), []).append(int(status.parent.name))
            except (OSError, ValueError, IndexError):
                continue
        descendants = []
        pending = [pid]
        while pending:
            current = pending.pop()
            pending.extend(children.get(current, []))
            descendants.extend(children.get(current, []))
        assert descendants
        payload = descendants[-1]
        expected = {
            resource.RLIMIT_CPU: (6, 7),
            resource.RLIMIT_AS: (256 * 1024**2, 256 * 1024**2),
            resource.RLIMIT_FSIZE: (16 * 1024**2, 16 * 1024**2),
            resource.RLIMIT_NOFILE: (64, 64),
            resource.RLIMIT_CORE: (0, 0),
        }
        for kind, pair in expected.items():
            assert resource.prlimit(payload, kind) == pair
        assert raw(port).endswith(b"HELLO")
