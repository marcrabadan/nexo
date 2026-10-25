import io

from nexo.errors import NexoError
from nexo.interpreter import Interpreter
from nexo.types import NexoFailure


def run(src: str) -> str:
    """Run a program and return what it printed."""
    out = io.StringIO()
    Interpreter(out=out).run(src, "t.nexo")
    return out.getvalue()


def error_of(src: str):
    """Run a program that must fail; return the NexoError or ErrorValue."""
    try:
        Interpreter(out=io.StringIO()).run(src, "t.nexo")
    except NexoError as e:
        return e
    except NexoFailure as f:
        return f.error
    raise AssertionError("the program did not fail")
