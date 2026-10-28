"""Iterative JSON nesting check before parsing and recursive value conversion."""
MAX_DEPTH = 128


def nesting_within(text, maximum=MAX_DEPTH):
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8-sig")
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > maximum:
                return False
        elif char in "]}":
            depth = max(0, depth - 1)
    return True
