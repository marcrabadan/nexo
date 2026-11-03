"""M6 typed public-state boundary. Int64 values use decimal strings on the wire."""
from collections.abc import Mapping
import json

MAX_BYTES = 1024 * 1024
MAX_ITEMS = 10000


def checked(typ, value, depth=0):
    if depth > 32:
        raise ValueError('State nesting exceeds 32 levels')
    if typ == 'int':
        if type(value) is not int or not -(2**63) <= value < 2**63:
            raise ValueError('Expected int64')
        return value
    if typ == 'bool':
        if type(value) is not bool:
            raise ValueError('Expected bool')
        return value
    if typ == 'str':
        if not isinstance(value, str) or '\0' in value or any(0xd800 <= ord(c) <= 0xdfff for c in value):
            raise ValueError('Expected a Unicode scalar string')
        return value
    if typ[0] == 'list':
        if not isinstance(value, (list, tuple)) or len(value) > MAX_ITEMS:
            raise ValueError('Expected a bounded list')
        return [checked(typ[1], item, depth + 1) for item in value]
    fields = dict(typ[2])
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise ValueError('Expected exact record fields')
    return {name: checked(field, value[name], depth + 1) for name, field in fields.items()}


def packed(typ, value):
    if typ == 'int':
        return str(value)
    if isinstance(typ, str):
        return value
    if typ[0] == 'list':
        return [packed(typ[1], item) for item in value]
    return {name: packed(field, value[name]) for name, field in typ[2]}


def patch(fields, values):
    if not isinstance(values, Mapping) or any(name not in fields for name in values):
        raise ValueError('Unknown state field')
    result = {name: checked(fields[name], value) for name, value in values.items()}
    wire = {name: packed(fields[name], value) for name, value in result.items()}
    if len(json.dumps(wire, ensure_ascii=True).encode()) > MAX_BYTES:
        raise ValueError('State payload exceeds 1 MiB')
    return result, wire


def key_token(value):
    if type(value) is int and -(2**63) <= value < 2**63:
        return 'i' + str(value)
    if isinstance(value, str):
        return 's' + value.encode('utf-8').hex()
    raise ValueError('Keys require int64 or str')
