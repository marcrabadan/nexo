"""docs/libraries.html: the examples of nexo.ai, nexo.db and nexo.stream.

The page is built from docs/libraries/page.tmpl and the programs next to
it by docs/libraries/build.py, which runs each one with the interpreter
and, when it is built, the compiler, and stops if they differ."""
import importlib.util

from phases import ROOT


def test_the_page_shows_what_its_programs_print():
    spec = importlib.util.spec_from_file_location("libraries_build", ROOT / "docs" / "libraries" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    assert (ROOT / "docs" / "libraries.html").read_text(encoding="utf-8") == build.build(), \
        "run .venv/bin/python docs/libraries/build.py"
