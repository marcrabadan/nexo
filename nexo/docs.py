"""`nexo docs --for-ai [file]`: the exact list of what a program can use,
for a language model (section "Goals and guarantees", D130). The list is
nexo/stdlib/api.json, shared with the compiler; with a file, the
program's own functions and types follow, with their `##` comments."""

from __future__ import annotations

import json
from pathlib import Path

API = Path(__file__).parent / "stdlib" / "api.json"

DEFS = ("fn ", "pub fn ", "type ", "pub type ", "trait ", "pub trait ")


def api() -> dict:
    return json.loads(API.read_text(encoding="utf-8"))


def outline(source: str) -> list[str]:
    """The definitions at the top of a file: their first line without the
    colon, the `##` comments above them, and the fields and methods of a
    type or trait."""
    out: list[str] = []
    docs: list[str] = []
    inside = False
    for line in source.splitlines():
        text = line.rstrip()
        if text.startswith("##"):
            docs.append(text)
            inside = False
            continue
        if text.startswith(DEFS):
            out.append(text[:-1] if text.endswith(":") else text)
            out.extend("    " + d for d in docs)
            inside = text.startswith(("type ", "pub type ", "trait ", "pub trait ")) and text.endswith(":")
            docs = []
            continue
        if inside and text.startswith("    ") and not text.startswith("     "):
            member = text[4:]
            if member.startswith(("fn ", "pub fn ")):
                out.append("    " + (member[:-1] if member.endswith(":") else member))
            elif ":" in member and not member.startswith(("#", "@")):
                out.append("    " + member)
            continue
        if text and not text.startswith(" "):
            inside = False
        docs = []
    return out


def for_ai(path: str | None = None, source: str | None = None) -> str:
    data = api()
    lines = [f"# Nexo {data['edition']}: what a program can use", "",
             "Only these functions, values, types and methods exist; any other name is defined by the program.", ""]
    for section in data["sections"]:
        lines.append(f"## {section['title']}")
        lines.extend(f"{sig}  # {does}" for sig, does in section["entries"])
        lines.append("")
    lines.append("## Values")
    lines.extend(f"{name}  # {does}" for name, does in data["values"])
    lines.append("")
    lines.append("## Types")
    lines.append(", ".join(data["types"]))
    lines.append("")
    lines.append("## Methods")
    lines.extend(f"{t}: {ms}" for t, ms in data["methods"])
    lines.append("")
    lines.append("## Not available in this edition")
    lines.append(", ".join(data["unavailable"]))
    if path is not None and source is not None:
        lines.append("")
        lines.append(f"## This program ({path})")
        lines.extend(outline(source))
    return "\n".join(lines) + "\n"
