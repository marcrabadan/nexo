"""Bounds are checked iteratively before parsing or native value conversion."""
import json
import random
from nexo.json_limits import nesting_within


def test_depth_boundary_and_string_escapes():
    assert nesting_within('[' * 128 + '0' + ']' * 128)
    assert not nesting_within('[' * 129 + '0' + ']' * 129)
    assert nesting_within(json.dumps({'text': '[\\"{}]' * 1000}))
    # Syntax errors remain the parser's job, preserving its error positions.
    assert nesting_within('"unterminated')


def test_seeded_structural_mutations_agree_with_generated_depth():
    rnd = random.Random(0x4E45584F)
    for _ in range(1000):
        depth = rnd.randrange(200)
        value = json.dumps('[' * rnd.randrange(200) + '\\"')
        for _ in range(depth):
            value = '[' + value + ']' if rnd.randrange(2) else '{"v":' + value + '}'
        assert nesting_within(value) == (depth <= 128)


def test_utf8_byte_manifests_and_bom_share_the_same_bound():
    assert nesting_within(b'{"version":1}')
    assert nesting_within(b'\xef\xbb\xbf{"version":1}')
    assert not nesting_within(b'[' * 129 + b'0' + b']' * 129)
