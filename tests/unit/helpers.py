import io

from nexo import errors
from nexo.errors import NexoError
from nexo.interpreter import Interpreter
from nexo.types import NexoFailure


def run(src: str, lang: str = "es") -> str:
    """Run a program and return what it printed."""
    errors.set_lang(lang)
    out = io.StringIO()
    Interpreter(out=out).run(src, "t.nexo")
    return out.getvalue()


def error_of(src: str, lang: str = "es"):
    """Run a program that must fail; return the NexoError or ErrorValue."""
    errors.set_lang(lang)
    try:
        Interpreter(out=io.StringIO()).run(src, "t.nexo")
    except NexoError as e:
        return e
    except NexoFailure as f:
        return f.error
    raise AssertionError("the program did not fail")
