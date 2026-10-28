"""Packages (section 13, D174): `nexo add`, `nexo publish`, nexo.lock and
the packages a `use` loads.

A registry is a set of static files, a folder or an HTTPS address:
`NAME/index.json` lists the versions of a package, each with the sha256
of its archive, the Ed25519 key that signed it, the signature and the
registry's advisories; `NAME/NAME-VERSION.json` is the archive, the files
of the package. The official registry is REGISTRY; NEXO_REGISTRY names
another. A requirement is `^MAJOR.MINOR` (or `^MAJOR.MINOR.PATCH`), and a
program has one version of each package: the highest one every
requirement accepts. nexo.lock records each version with its sha256 and
key; a later version signed by another key is not installed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from . import errors
from .errors import NexoError

REGISTRY = "https://registry.nexo-lang.org"
NAME = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
REQUIREMENT = re.compile(r"\^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?")
FILE = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*\.nexo|nexo\.toml|README\.md")
LOCK_HEADER = "# nexo.lock: the exact packages of this project, written by nexo add. Do not edit.\n"


def fail(key: str, **args) -> NexoError:
    return NexoError(key, None, **args)


def import_name(package: str) -> str:
    """The name a `use` gives a package: `money-format` is `money_format`."""
    return package.replace("-", "_")


# ---------------------------------------------------------------- versions

def version(text: str) -> tuple[int, int, int] | None:
    m = VERSION.fullmatch(text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def requirement(text: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """[low, high) of `^1.2`: 1.2.0 up to 2.0.0; `^0.3` stays in 0.3."""
    m = REQUIREMENT.fullmatch(text)
    if not m:
        raise fail("package_requirement", requirement=text)
    major, minor, patch = int(m[1]), int(m[2]), int(m[3] or 0)
    low = (major, minor, patch)
    if major > 0:
        return low, (major + 1, 0, 0)
    if minor > 0 or m[3] is None:
        return low, (0, minor + 1, 0)
    return low, (0, 0, patch + 1)


def accepts(req: str, v: tuple[int, int, int]) -> bool:
    low, high = requirement(req)
    return low <= v < high


def shown_version(v: tuple[int, int, int]) -> str:
    return ".".join(map(str, v))


# -------------------------------------------------------------------- toml

def toml_value(text: str):
    text = text.strip()
    if text in ("true", "false"):
        return text == "true"
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items = [toml_value(p) for p in inner.split(",") if p.strip()]
        return items if all(isinstance(i, str) for i in items) else None
    return None


def without_comment(line: str) -> str:
    quoted = False
    for i, c in enumerate(line):
        if c == '"':
            quoted = not quoted
        elif c == "#" and not quoted:
            return line[:i]
    return line


def read_manifest(text: str, path: str) -> dict:
    """The tables of a nexo.toml that packages use: {table: {key: value}}."""
    tables: dict[str, dict] = {}
    table = ""
    for n, raw in enumerate(text.splitlines(), 1):
        line = without_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            table = line.strip("[]").strip()
            tables.setdefault(table, {})
            continue
        key, eq, value = line.partition("=")
        if not eq:
            raise fail("package_manifest_line", path=path, line=n)
        tables.setdefault(table, {})[key.strip().strip('"')] = toml_value(value)
    return tables


def dependencies(manifest: dict, path: str) -> dict[str, str]:
    deps = manifest.get("dependencies", {})
    for name, req in deps.items():
        if not isinstance(req, str):
            raise fail("package_manifest_line", path=path, line=0)
        requirement(req)
    return dict(deps)


def permissions(manifest: dict) -> list[tuple[str, str | None]]:
    """(kind, target) pairs of `[permissions]`; `shell = true` has no target."""
    from .stdlib.policy import KINDS
    out: list[tuple[str, str | None]] = []
    for kind, value in manifest.get("permissions", {}).items():
        if kind not in KINDS:
            continue
        if kind == "shell":
            if value is True:
                out.append(("shell", None))
        elif isinstance(value, list):
            out.extend((kind, t) for t in value)
    return out


def set_dependency(path: Path, name: str, req: str) -> None:
    """Write `name = "req"` in the [dependencies] table of nexo.toml."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "[dependencies]"), None)
    entry = f'{name} = "{req}"'
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", "[dependencies]", entry]
    else:
        end = start + 1
        while end < len(lines) and not lines[end].strip().startswith("["):
            end += 1
        for i in range(start + 1, end):
            if lines[i].split("=", 1)[0].strip().strip('"') == name:
                lines[i] = entry
                break
        else:
            last = end
            while last > start + 1 and not lines[last - 1].strip():
                last -= 1
            lines.insert(last, entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# -------------------------------------------------------------------- json

def canonical(value) -> str:
    """The JSON both phases write byte for byte: keys sorted, no spaces."""
    if isinstance(value, dict):
        return "{" + ",".join(canonical(k) + ":" + canonical(value[k]) for k in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, str):
        out = ['"']
        for c in value:
            if c == '"':
                out.append('\\"')
            elif c == "\\":
                out.append("\\\\")
            elif c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            elif ord(c) < 0x20:
                out.append(f"\\u{ord(c):04x}")
            else:
                out.append(c)
        return "".join(out) + '"'
    raise TypeError(value)


# ---------------------------------------------------------------- registry

def registry() -> str:
    return os.environ.get("NEXO_REGISTRY") or REGISTRY


def is_url(place: str) -> bool:
    return place.startswith("https://") or place.startswith("http://")


def fetch(name: str, file: str) -> bytes | None:
    """A file of the registry, or None when it has no such file."""
    place = registry()
    if not is_url(place):
        try:
            return (Path(place) / name / file).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise fail("package_registry", registry=place, detail=e.strerror or str(e)) from None
    host = (urllib.parse.urlsplit(place).hostname or "").lower()
    if place.startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        raise fail("package_registry", registry=place, detail="HTTPS is needed for any host but this machine")
    url = place.rstrip("/") + "/" + urllib.parse.quote(f"{name}/{file}")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise fail("package_registry", registry=place, detail=f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise fail("package_registry", registry=place, detail=str(getattr(e, "reason", e))) from None


def index(name: str) -> list[dict]:
    data = fetch(name, "index.json")
    if data is None:
        raise fail("package_unknown", name=name, registry=registry())
    try:
        doc = json.loads(data)
        entries = [e for e in doc["versions"] if version(e["version"])]
    except (ValueError, KeyError, TypeError):
        raise fail("package_registry", registry=registry(), detail=f"{name}/index.json is not an index") from None
    return entries


def verify(data: bytes, key: str, signature: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True)).verify(
            base64.b64decode(signature, validate=True), data)
        return True
    except (InvalidSignature, ValueError):
        return False


class Package:
    """One version of a package, downloaded and checked."""

    def __init__(self, name: str, entry: dict, data: bytes, files: dict[str, str]):
        self.name = name
        self.version = entry["version"]
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.key = entry["key"]
        self.advisories = entry.get("advisories") or []
        self.files = files
        manifest = read_manifest(files.get("nexo.toml", ""), f"{name} {self.version}/nexo.toml")
        project = manifest.get("project", {})
        if project.get("name") != name or project.get("version") != self.version:
            raise fail("package_bad", name=name, version=self.version,
                       detail="its nexo.toml names another package or version")
        self.requires = dependencies(manifest, f"{name} {self.version}/nexo.toml")
        self.permissions = permissions(manifest)


def download(name: str, entry: dict, locked: dict | None) -> Package:
    ver = entry["version"]
    data = fetch(name, f"{name}-{ver}.json")
    if data is None:
        raise fail("package_bad", name=name, version=ver, detail="the registry has no archive of it")
    digest = hashlib.sha256(data).hexdigest()
    if locked and locked["version"] == ver and locked["sha256"] != digest:
        raise fail("package_hash", name=name, version=ver)
    if digest != entry.get("sha256") or not verify(data, entry.get("key", ""), entry.get("signature", "")):
        raise fail("package_signature", name=name, version=ver)
    if locked and locked["key"] != entry["key"]:
        raise fail("package_key", name=name, version=ver)
    try:
        files = json.loads(data.decode("utf-8"))["files"]
        ok = isinstance(files, dict) and all(
            isinstance(p, str) and FILE.fullmatch(p) and ".." not in p.split("/") and isinstance(t, str)
            for p, t in files.items())
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        ok = False
    if not ok:
        raise fail("package_bad", name=name, version=ver, detail="its archive is not a list of package files")
    return Package(name, entry, data, files)


# -------------------------------------------------------------- resolution

def resolve(wanted: dict[str, str], locked: dict[str, dict]) -> dict[str, Package]:
    """One version of each package: the highest that every requirement on
    it accepts, or the locked one while it still does."""
    chosen: dict[str, Package] = {}
    indexes: dict[str, list[dict]] = {}
    for _ in range(100):
        reqs: dict[str, list[tuple[str, str]]] = {}
        for name, req in wanted.items():
            reqs.setdefault(name, []).append((req, "nexo.toml"))
        for p in chosen.values():
            for name, req in p.requires.items():
                reqs.setdefault(name, []).append((req, f"{p.name} {p.version}"))
        changed = False
        for name in sorted(reqs):
            if name not in indexes:
                indexes[name] = index(name)
            versions = sorted((version(e["version"]), e) for e in indexes[name])
            fits = [(v, e) for v, e in versions if all(accepts(r, v) for r, _ in reqs[name])]
            if not fits:
                raise no_version(name, reqs[name], [v for v, _ in versions])
            lock = locked.get(name)
            pick = next((f for f in fits if lock and shown_version(f[0]) == lock["version"]), fits[-1])
            if name not in chosen or chosen[name].version != pick[1]["version"]:
                chosen[name] = download(name, pick[1], lock)
                changed = True
        for name in [n for n in chosen if n not in reqs]:
            del chosen[name]
            changed = True
        if not changed:
            return chosen
    raise fail("package_conflict_loop")


def no_version(name: str, reqs: list[tuple[str, str]], versions) -> NexoError:
    for i, (a, who_a) in enumerate(reqs):
        for b, who_b in reqs[i + 1:]:
            if not any(accepts(a, v) and accepts(b, v) for v in versions):
                return fail("package_conflict", name=name, first=who_a, a=a, second=who_b, b=b)
    req, _ = reqs[0]
    shown = ", ".join(shown_version(v) for v in versions) or "none"
    return fail("package_no_version", name=name, requirement=req, versions=shown)


# -------------------------------------------------------------------- lock

def read_lock(root: Path) -> dict[str, dict]:
    path = root / "nexo.lock"
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    entry: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line == "[[package]]":
            entry = {}
            continue
        key, eq, value = line.partition("=")
        if entry is not None and eq:
            entry[key.strip()] = toml_value(value)
            if "name" in entry and isinstance(entry["name"], str):
                out[entry["name"]] = entry
    return out


def write_lock(root: Path, chosen: dict[str, Package]) -> None:
    parts = [LOCK_HEADER]
    for name in sorted(chosen):
        p = chosen[name]
        deps = ", ".join(f'"{d} {r}"' for d, r in sorted(p.requires.items()))
        parts.append(f'\n[[package]]\nname = "{name}"\nversion = "{p.version}"\nsha256 = "{p.sha256}"\n'
                     f'key = "{p.key}"\ndependencies = [{deps}]\n')
    (root / "nexo.lock").write_text("".join(parts), encoding="utf-8")


def installed_dir(root: Path, name: str) -> Path:
    return root / ".nexo" / "packages" / name


def install(root: Path, chosen: dict[str, Package]) -> None:
    base = root / ".nexo" / "packages"
    if base.is_dir():
        for old in base.iterdir():
            if old.name not in chosen:
                shutil.rmtree(old, ignore_errors=True)
    for name, p in chosen.items():
        target = installed_dir(root, name)
        shutil.rmtree(target, ignore_errors=True)
        for rel, text in sorted(p.files.items()):
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


# ------------------------------------------------------- permissions, audit

def program_policies(root: Path) -> list[tuple[str, list]]:
    """(file, rules) of every program of the project that has a policy."""
    from .parser import parse
    from .stdlib.policy import declared
    out = []
    for path in sorted(root.rglob("*.nexo")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            found = declared(parse(path.read_text(encoding="utf-8"), str(rel)))
        except (NexoError, OSError, UnicodeDecodeError):
            continue
        if found:
            out.append((str(rel), found[1]))
    return out


def allowed(rules: list, kind: str, target: str | None) -> bool:
    from .stdlib.policy import matches
    ok = False
    for rule, k, targets in rules:
        if k != kind:
            continue
        hit = not targets or target is None or any(matches(kind, target, t) for t in targets)
        if hit and rule == "deny":
            return False
        ok = ok or hit
    return ok


def permission_text(kind: str, target: str | None) -> str:
    if target is None:
        return errors.msg("detail.package_shell")
    return errors.msg(f"detail.policy_{kind}", target=target)


def check_permissions(root: Path, chosen: dict[str, Package]) -> None:
    from .stdlib.policy import rule_text
    policies = program_policies(root)
    for name in sorted(chosen):
        for kind, target in chosen[name].permissions:
            for file, rules in policies:
                if not allowed(rules, kind, target):
                    raise fail("package_permissions", name=name, file=file,
                               what=permission_text(kind, target),
                               line=rule_text("allow", kind, target))


def check_advisories(chosen: dict[str, Package], accept_risk: bool) -> list[str]:
    warnings = []
    for name in sorted(chosen):
        p = chosen[name]
        for a in p.advisories:
            severity = str(a.get("severity", "")).lower()
            if severity == "critical" and not accept_risk:
                raise fail("package_advisory", name=name, version=p.version, id=a.get("id", ""),
                           summary=a.get("summary", ""))
            warnings.append(errors.msg("info.package_advisory", name=name, version=p.version, id=a.get("id", ""),
                                       severity=severity, summary=a.get("summary", "")))
    return warnings


# ------------------------------------------------------------------ add

def project() -> tuple[Path, dict]:
    from .modules import project_root
    root = project_root(str(Path.cwd() / "x.nexo"))
    path = root / "nexo.toml"
    if not path.is_file():
        raise fail("package_project")
    return root, read_manifest(path.read_text(encoding="utf-8"), "nexo.toml")


def add(name: str | None, accept_risk: bool) -> list[str]:
    """`nexo add [name] [--accept-risk]`: the lines it prints."""
    root, manifest = project()
    wanted = dependencies(manifest, "nexo.toml")
    locked = read_lock(root)
    if name is not None:
        if not NAME.fullmatch(name):
            raise fail("package_name", name=name)
        if name not in wanted:
            entries = index(name)
            newest = max(version(e["version"]) for e in entries) if entries else None
            if newest is None:
                raise fail("package_no_version", name=name, requirement="^0.0", versions="none")
            wanted[name] = f"^{newest[0]}.{newest[1]}"
    chosen = resolve(wanted, locked)
    check_permissions(root, chosen)
    lines = check_advisories(chosen, accept_risk)
    if name is not None:
        set_dependency(root / "nexo.toml", name, wanted[name])
    install(root, chosen)
    write_lock(root, chosen)
    for n in sorted(chosen):
        p = chosen[n]
        lines.append(errors.msg("info.package_line", name=n, version=p.version))
        for kind, target in p.permissions:
            lines.append(errors.msg("info.package_permission",
                                    what=permission_text(kind, target)))
    if name is not None:
        lines.append(errors.msg("info.package_added", name=name, version=chosen[name].version))
    else:
        lines.append(errors.msg("info.package_installed", count=len(chosen)))
    return lines


# ------------------------------------------------------------------ publish

def publish_key() -> tuple[bytes, str]:
    """The private key of this user (made on the first publish) and its
    public half in base64."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from .stdlib.ai import config_dir
    path = config_dir() / "publish.key"
    if path.is_file():
        seed = base64.b64decode(path.read_text(encoding="utf-8").strip())
    else:
        seed = os.urandom(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(base64.b64encode(seed).decode() + "\n")
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return seed, base64.b64encode(pub).decode()


def package_files(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and FILE.fullmatch(rel) and not any(p.startswith(".") for p in rel.split("/")):
            files[rel] = path.read_text(encoding="utf-8")
    return files


def publish() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    root, manifest = project()
    place = registry()
    if is_url(place):
        raise fail("publish_registry", registry=place)
    info = manifest.get("project", {})
    name, ver = info.get("name"), info.get("version")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise fail("publish_manifest", field='name = "letters-and-digits"')
    if not isinstance(ver, str) or not version(ver):
        raise fail("publish_manifest", field='version = "MAJOR.MINOR.PATCH"')
    dependencies(manifest, "nexo.toml")
    folder = Path(place) / name
    index_path = folder / "index.json"
    entries = json.loads(index_path.read_text(encoding="utf-8"))["versions"] if index_path.is_file() else []
    if any(e.get("version") == ver for e in entries):
        raise fail("publish_exists", name=name, version=ver)
    data = canonical({"files": package_files(root)}).encode("utf-8")
    seed, key = publish_key()
    signature = base64.b64encode(Ed25519PrivateKey.from_private_bytes(seed).sign(data)).decode()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}-{ver}.json").write_bytes(data)
    entries.append({"version": ver, "sha256": hashlib.sha256(data).hexdigest(), "key": key, "signature": signature})
    entries.sort(key=lambda e: version(e["version"]) or (0, 0, 0))
    index_path.write_text(canonical({"name": name, "versions": entries}) + "\n", encoding="utf-8")
    return errors.msg("info.package_published", name=name, version=ver, registry=place, public=key)


# ------------------------------------------------------------- for `use`

def scope(root: Path, manifest_path: Path) -> dict[str, str]:
    """{import name: package} of the dependencies a nexo.toml lists."""
    try:
        manifest = read_manifest(manifest_path.read_text(encoding="utf-8"), str(manifest_path))
    except OSError:
        return {}
    return {import_name(n): n for n in manifest.get("dependencies", {})}
