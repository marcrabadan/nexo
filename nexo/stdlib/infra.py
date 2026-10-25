"""nexo.infra (section 11, D118-D121): `infra` and `stage` blocks and the
commands `nexo plan`, `apply`, `destroy`, `drift`, `cost` and `import`.

An infra block declares portable resources; each one becomes the native
resources of its target (infra_providers.json, shared with the compiler),
which `nexo apply` creates through the target's own API: AWS Cloud Control,
Azure Resource Manager, the REST APIs of Google Cloud, and the Kubernetes
API. What was created is kept in a state (a local file or an S3 object),
and a plan is the difference between the resources the program declares
and the state."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from .. import errors
from ..errors import NexoError
from ..types import Builtin, Money, Native, Quantity, Range, show
from .cfg import Cfg, Cond, Ref, atom_text

PROVIDERS = json.loads((Path(__file__).parent / "infra_providers.json").read_text(encoding="utf-8"))
CLOUDS = ("aws", "azure", "gcp", "k8s")
KINDS = tuple(PROVIDERS["kinds"])
INFRA_KEYS = ("cloud", "region", "state", "budget", "data_region", "tags", "project", "namespace", "cluster",
              "subscription")
UNSUPPORTED_KEYS = ("spread", "min_available", "regions", "traffic", "failover", "flags_store", "backup",
                    "restore_test", "read_replicas", "replicas_read")
UNKNOWN = "\x00unknown"

INFRAS: list = []
STAGES: dict = {}
IMAGE: list = [None]  # apply(image:) in a pipeline step (D122)


def reset() -> None:
    INFRAS.clear()
    STAGES.clear()


def m(key: str, **args) -> str:
    return errors.msg(key, **args)


def slug(text: str, keep: str = "-") -> str:
    out = re.sub(r"[^a-z0-9]+", keep, text.lower()).strip(keep)
    return out or "nexo"


# ------------------------------------------------------------ the model

class Resource:
    def __init__(self, kind, name, span):
        self.kind, self.name, self.span = kind, name, span
        self.values: dict = {}  # key -> (value, span)
        self.env: list = []  # (name, value, span)
        self.scale = None  # (min, max, cpu target)
        self.lines: list = []  # allow / can lines: (words, options, span)
        self.stages: dict = {}  # stage -> [Cfg]
        self.target = None  # dns: the app
        self.https = False
        self.native_type = None


class Output:
    def __init__(self, name, ref: Ref, secret: bool, span):
        self.name, self.ref, self.secret, self.span = name, ref, secret, span


class Infra(Native):
    type_name = "infra"

    def __init__(self, name, span):
        self.name, self.span = name, span
        self.values: dict = {}
        self.policy: list = []  # (words, options, span)
        self.resources: list[Resource] = []
        self.outputs: list[Output] = []
        self.peer_refs: list = []  # (ref, at): values of other infra blocks (D121)

    def repr(self) -> str:
        return f"infra({self.name})"

    def resource(self, name):
        return next((r for r in self.resources if r.name == name), None)


def key_error(entry: Cfg, key: str, choices, kind: str):
    s = errors.suggest(key, list(choices))
    if s:
        return NexoError("infra_key", entry.span, fix="suggest", key=key, kind=kind, suggestion=s)
    return NexoError("infra_key", entry.span, key=key, kind=kind)


def b_infra(call, name, entries):
    infra = Infra(name, call.span)
    if any(i.name == name for i in INFRAS):
        raise NexoError("duplicate_name", call.span, name=name)
    for entry in entries:
        read_infra_entry(infra, entry)
    if "cloud" not in infra.values:
        raise NexoError("infra_cloud", call.span, infra=name)
    check_refs(infra)
    INFRAS.append(infra)
    return None


def b_stage(call, name, entries):
    for entry in entries:
        if entry.key() is None or entry.body is not None:
            raise entry.error("stage_entry")
    STAGES.setdefault(name, []).extend(entries)
    return None


def word_of(v) -> str | None:
    return v if isinstance(v, str) else None


def read_infra_entry(infra: Infra, entry: Cfg) -> None:
    key = entry.key()
    if key is not None:
        if key in UNSUPPORTED_KEYS:
            raise NexoError("not_supported", entry.span, what=f'"{key} ="')
        if key not in INFRA_KEYS:
            raise key_error(entry, key, INFRA_KEYS + KINDS + ("policy", "output"), "infra")
        if key in infra.values:
            raise NexoError("duplicate_name", entry.span, name=key)
        infra.values[key] = (entry_value(entry), entry)
        return
    words = entry.words()
    head = word_of(words[0]) if words else None
    if head == "policy" and len(words) == 1 and isinstance(entry.body, list):
        for line in entry.body:
            if line.values is not None or line.body is not None:
                raise line.error("policy_rule", rule=show_phrase(line))
            infra.policy.append(line)
        return
    if head == "output":
        if len(words) != 2 or not isinstance(words[1], str) or entry.values is None:
            raise entry.error("output_form")
        ref = entry.values[0][0] if len(entry.values[0]) == 1 else None
        secret = [p for p in entry.values[1:]] == [["secret"]]
        if not isinstance(ref, Ref) or len(ref.parts) not in (2, 3) or (len(entry.values) > 1 and not secret):
            raise entry.error("output_form")
        infra.outputs.append(Output(words[1], ref, secret, entry.span))
        return
    if head not in KINDS:
        raise key_error(entry, head or atom_text(words[0]) if words else "?", INFRA_KEYS + KINDS + ("policy", "output"),
                        "infra")
    read_resource(infra, head, entry)


def show_phrase(entry: Cfg) -> str:
    from .cfg import show_entry
    return show_entry(entry)


def entry_value(entry: Cfg):
    """The value of `key = value`: one atom, or the atoms of a phrase."""
    v = entry.value()
    if len(entry.values) > 1 or entry.options:
        return [list(p) for p in entry.values]
    return v


def read_resource(infra: Infra, kind: str, entry: Cfg) -> None:
    words = entry.words()
    spec = PROVIDERS["kinds"][kind]
    if kind == "dns":
        # dns "api.shop.com" -> api, https
        if len(words) != 4 or not isinstance(words[1], str) or words[2] != "->" or not isinstance(words[3], str):
            raise entry.error("dns_form")
        r = Resource(kind, words[1], entry.span)
        r.target = words[3]
        extra = entry.head[1:]
        if extra not in ([], [["https"]]) or entry.options or entry.body is not None:
            raise entry.error("dns_form")
        r.https = extra == [["https"]]
    elif kind == "native":
        if len(words) != 3 or not isinstance(words[1], str) or not isinstance(words[2], str):
            raise entry.error("native_form")
        r = Resource(kind, words[2], entry.span)
        r.native_type = words[1]
    else:
        if len(words) != 2 or not isinstance(words[1], str) or entry.values is not None or len(entry.head) > 1:
            raise entry.error("resource_form", kind=kind)
        r = Resource(kind, words[1], entry.span)
    if infra.resource(r.name) is not None:
        raise NexoError("duplicate_name", entry.span, name=r.name)
    for sub in entry.entries():
        read_resource_entry(r, spec, sub)
    infra.resources.append(r)


def read_resource_entry(r: Resource, spec: dict, entry: Cfg) -> None:
    key = entry.key()
    if key is not None:
        if r.kind == "native":
            r.values[key] = (entry_value(entry), entry)
            return
        if key in UNSUPPORTED_KEYS:
            raise NexoError("not_supported", entry.span, what=f'"{key} ="')
        if key not in spec["keys"]:
            raise key_error(entry, key, list(spec["keys"]) + spec.get("blocks", []) + spec.get("lines", []), r.kind)
        if key in r.values:
            raise NexoError("duplicate_name", entry.span, name=key)
        r.values[key] = (entry_value(entry), entry)
        return
    words = entry.words()
    head = word_of(words[0]) if words else None
    if head in spec.get("lines", []) and entry.body is None:
        r.lines.append(entry)
        return
    if head == "env" and "env" in spec.get("blocks", []) and len(words) == 1 and isinstance(entry.body, list):
        for e in entry.body:
            k = e.key()
            if k is None or e.body is not None or len(e.values) != 1 or e.options:
                raise e.error("env_entry")
            r.env.append((k, e.value(), e))
        return
    if head == "scale" and "scale" in spec.get("blocks", []):
        rng = words[1] if len(words) == 2 else None
        if not isinstance(rng, Range) or entry.values is not None:
            if len(words) >= 2 and words[1] == "at":
                raise NexoError("not_supported", entry.span, what='"scale at"')
            raise entry.error("scale_form")
        target = 70
        for c in entry.entries():
            w = c.words()
            cond = w[0] if len(w) == 1 else None
            if (isinstance(cond, Cond) and cond.op == ">" and cond.args[0] == "cpu" and isinstance(cond.args[1], Quantity)
                    and cond.args[1].unit == "%" and c.values is None and not c.options):
                target = int(cond.args[1].value)
                continue
            raise NexoError("not_supported", c.span, what=f'"{show_phrase(c)}"')
        r.scale = (rng.start, rng.end, target)
        return
    if head == "release" and "release" in spec.get("blocks", []):
        if len(words) != 2 or words[1] not in ("rolling", "canary", "blue_green"):
            raise entry.error("release_form")
        if words[1] != "rolling":
            raise NexoError("not_supported", entry.span, what=f'"release {words[1]}"')
        if entry.entries():
            raise NexoError("not_supported", entry.entries()[0].span, what=f'"{show_phrase(entry.entries()[0])}"')
        return
    if head == "stage" and "stage" in spec.get("blocks", []) and len(words) == 2 and isinstance(entry.body, list):
        for e in entry.body:
            if e.key() is None or e.body is not None:
                raise e.error("stage_entry")
            if e.key() not in spec["keys"]:
                raise key_error(e, e.key(), list(spec["keys"]), r.kind)
        r.stages.setdefault(words[1], []).extend(entry.body)
        return
    if head == "calls":
        raise NexoError("not_supported", entry.span, what='"calls"')
    choices = list(spec["keys"]) + spec.get("blocks", []) + spec.get("lines", [])
    raise key_error(entry, head or (atom_text(words[0]) if words else "?"), choices, r.kind)


def check_refs(infra: Infra) -> None:
    """References between the resources of a block (refs to other infra
    blocks are checked when planning)."""
    for r in infra.resources:
        spec = PROVIDERS["kinds"][r.kind]
        for key, (value, entry) in r.values.items():
            k = spec["keys"].get(key)
            if k and k["type"] == "ref":
                target = infra.resource(value) if isinstance(value, str) else None
                if target is None or target.kind != k["kind"]:
                    raise entry.error("infra_ref", key=key, kind=k["kind"], name=atom_text(value))
        if r.kind == "dns":
            t = infra.resource(r.target)
            if t is None or t.kind != "app":
                raise NexoError("infra_ref", r.span, key="->", kind="app", name=r.target)
        for _, value, e in r.env:
            if isinstance(value, Ref):
                check_attr(infra, value, e)
    for o in infra.outputs:
        check_attr(infra, o.ref, o)


def check_attr(infra: Infra, ref: Ref, at, shown: Ref | None = None) -> None:
    if len(ref.parts) == 3 and shown is None and infra.resource(ref.parts[0]) is None:
        # another infra block, maybe declared later: checked by infra_order
        infra.peer_refs.append((ref, at))
        return
    shown = shown or ref
    if len(ref.parts) != 2:
        raise NexoError("infra_attr", at.span, ref=shown.repr())
    target = infra.resource(ref.parts[0])
    if target is None:
        raise NexoError("infra_attr", at.span, ref=shown.repr())
    attrs = PROVIDERS["kinds"][target.kind]["attrs"]
    if ref.parts[1] not in attrs:
        s = errors.suggest(ref.parts[1], attrs)
        if s:
            raise NexoError("infra_attr_name", at.span, fix="suggest", ref=shown.repr(), kind=target.kind, suggestion=s)
        raise NexoError("infra_attr_name", at.span, ref=shown.repr(), kind=target.kind)


def infra_order(infras: list) -> list:
    """The infra blocks in the order they are applied: each one after the
    blocks whose values it uses (D121)."""
    by_name = {i.name: i for i in INFRAS}
    needs = {}
    for infra in infras:
        needs[infra.name] = []
        for ref, at in infra.peer_refs:
            peer = by_name.get(ref.parts[0])
            if peer is None or peer is infra:
                raise NexoError("infra_attr", at.span, ref=ref.repr())
            check_attr(peer, Ref(ref.parts[1:]), at, ref)
            if peer.name not in needs[infra.name]:
                needs[infra.name].append(peer.name)
    out, done, visiting = [], set(), []

    def visit(infra):
        if infra.name in done:
            return
        if infra.name in visiting:
            names = visiting[visiting.index(infra.name):]
            raise NexoError("infra_cycle", by_name[names[0]].span, names=", ".join(names))
        visiting.append(infra.name)
        for name in needs.get(infra.name, []):
            visit(by_name[name])
        visiting.pop()
        done.add(infra.name)
        if infra in infras:
            out.append(infra)
    for infra in infras:
        visit(infra)
    return out


# ---------------------------------------------------------------- values

class PlanError(Exception):
    """A mistake found while planning: shown as a diagnostic."""


def expects(entry, key: str, what: str):
    return NexoError("infra_value", entry.span, key=key, what=errors.msg(f"detail.infra_{what}"))


def setting(entry, key: str, spec: dict, value):
    """A setting checked against the table of its kind, as plain data."""
    t = spec["type"]
    if t == "text" or t == "cidr":
        if not isinstance(value, str):
            raise expects(entry, key, "text")
        if t == "cidr" and not re.match(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$", value):
            raise expects(entry, key, "cidr")
        return value
    if t == "int":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise expects(entry, key, "int")
        return value
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or value <= 0:
            raise expects(entry, key, "number")
        return float(value)
    if t == "bool":
        if not isinstance(value, bool):
            raise expects(entry, key, "bool")
        return value
    if t == "size":
        if not isinstance(value, Quantity) or value.dimension != "size" or value.base() <= 0:
            raise expects(entry, key, "size")
        return int(value.base() / 1_000_000)
    if t == "count":
        if isinstance(value, Range) and 0 <= value.start <= value.end:
            return {"min": value.start, "max": value.end}
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return {"min": value, "max": value}
        raise expects(entry, key, "count")
    if t == "word":
        if value not in spec["words"]:
            raise NexoError("infra_word", entry.span, key=key, words=", ".join(spec["words"]))
        return value
    if t == "ref":
        return value
    if t == "tags":
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise expects(entry, key, "tags")
        return dict(value)
    return value


def effective(infra: Infra, r: Resource, stage: str | None) -> dict:
    """Defaults, then the resource's values, then the stage's (D119)."""
    spec = PROVIDERS["kinds"][r.kind]
    out = {}
    for key, k in spec["keys"].items():
        if "default" in k:
            d = k["default"]
            out[key] = {"min": d, "max": d} if k["type"] == "count" else d
    entries = {key: (v, e) for key, (v, e) in r.values.items()}
    if stage is not None:
        for e in STAGES.get(stage, []):
            if e.key() in spec["keys"]:
                entries[e.key()] = (entry_value(e), e)
        for e in r.stages.get(stage, []):
            entries[e.key()] = (entry_value(e), e)
    for key, (v, e) in entries.items():
        if r.kind == "native":
            out[key] = v
            continue
        out[key] = setting(e, key, spec["keys"][key], v)
    for key, k in spec["keys"].items():
        if k.get("required") and key not in out:
            raise NexoError("infra_required", r.span, key=key, kind=r.kind, name=r.name)
    if r.kind == "app" and IMAGE[0] is not None and image_name(out["image"]) == image_name(IMAGE[0]):
        out["image"] = IMAGE[0]
    return out


def image_name(image: str) -> str:
    """`registry:5000/shop-api:2.4` -> `registry:5000/shop-api`."""
    head, sep, tag = image.rpartition(":")
    return head if sep and "/" not in tag else image


def infra_settings(infra: Infra, stage: str | None) -> dict:
    values = {k: v for k, (v, _) in infra.values.items()}
    entries = dict(infra.values)
    if stage is not None:
        for e in STAGES.get(stage, []):
            if e.key() in INFRA_KEYS:
                values[e.key()] = entry_value(e)
                entries[e.key()] = (entry_value(e), e)
    cloud = values.get("cloud")
    if cloud not in CLOUDS:
        raise NexoError("infra_word", infra.values["cloud"][1].span, key="cloud", words=", ".join(CLOUDS))
    for key in ("region", "data_region", "project", "namespace", "cluster", "subscription"):
        if key in values and not isinstance(values[key], str):
            raise expects(entries[key][1], key, "text")
    if "tags" in values:
        values["tags"] = setting(entries["tags"][1], "tags", {"type": "tags"}, values["tags"])
    return values


# ------------------------------------------------------------- templates

class Scope:
    """What the templates of one resource can read."""

    def __init__(self, plan, resource, ctx, setting_values):
        self.plan, self.resource, self.ctx, self.set = plan, resource, ctx, setting_values


def lookup(path: str, root: dict):
    v: Any = root
    for part in path.split("."):
        if isinstance(v, dict):
            if part not in v:
                return None
            v = v[part]
        elif isinstance(v, list) and part.isdigit() and int(part) < len(v):
            v = v[int(part)]
        else:
            return None
    return v


def text_of(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


PLACE = re.compile(r"\$\{([^${}]*)\}")


def expand_text(s: str, resolve):
    """`${path}` placeholders, innermost first; a text that is only one
    placeholder keeps the value's type."""
    while True:
        found = list(PLACE.finditer(s))
        if not found:
            return s
        if len(found) == 1 and found[0].span() == (0, len(s)):
            v = resolve(found[0].group(1))
            # a value can hold placeholders of its own (ctx.env, ctx.grants)
            return expand(v, resolve) if isinstance(v, (str, list, dict)) and "${" in json.dumps(v) else v
        out, last = [], 0
        for f in found:
            out.append(s[last:f.start()])
            v = resolve(f.group(1))
            if isinstance(v, str) and v.startswith(UNKNOWN):
                return UNKNOWN + ":" + f.group(1)
            out.append(text_of(v) if v is not None else "")
            last = f.end()
        out.append(s[last:])
        s = "".join(out)


OMIT = object()


def expand(t, resolve, items=None):
    """A template with its placeholders filled in."""
    if isinstance(t, str):
        return expand_text(t, resolve)
    if isinstance(t, list):
        out = []
        for x in t:
            if isinstance(x, dict) and "$each" in x:
                seq = resolve(x["$each"]) or []
                for i, item in enumerate(seq):
                    def inner(path, item=item, i=i):
                        if path == "item" or path.startswith("item."):
                            return lookup(path[5:], item) if path != "item" else item
                        return resolve(path)
                    v = expand(x["do"], inner)
                    if v is not OMIT:
                        out.append(v)
                continue
            v = expand(x, resolve)
            if v is not OMIT:
                out.append(v)
        return out
    if isinstance(t, dict):
        if "$if" in t:
            cond = resolve(t["$if"])
            branch = t.get("then") if truthy(cond) else t.get("else", OMIT)
            return OMIT if branch is OMIT else expand(branch, resolve)
        out = {}
        for k, v in t.items():
            key = expand_text(k, resolve)
            val = expand(v, resolve)
            if val is not OMIT and val is not None:
                out[key] = val
        return out
    return t


def truthy(v) -> bool:
    if isinstance(v, str) and v.startswith(UNKNOWN):
        return True
    return bool(v)


# --------------------------------------------------------------- context

def cidr_parts(text: str):
    ip, prefix = text.split("/")
    n = 0
    for part in ip.split("."):
        n = n * 256 + int(part)
    return n, int(prefix)


def cidr_text(n: int, prefix: int) -> str:
    return ".".join(str((n >> s) & 255) for s in (24, 16, 8, 0)) + f"/{prefix}"


def guid(text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


AWS_WORDS = {"acl": "ACL", "api": "API", "arn": "ARN", "dns": "DNS", "ec2": "EC2", "ecs": "ECS", "ecr": "ECR",
             "eks": "EKS", "efs": "EFS", "elb": "ELB", "iam": "IAM", "id": "Id", "ip": "IP", "kms": "KMS",
             "rds": "RDS", "s3": "S3", "sns": "SNS", "sqs": "SQS", "ssm": "SSM", "vpc": "VPC", "waf": "WAF",
             "wafv2": "WAFv2", "elasticloadbalancingv2": "ElasticLoadBalancingV2", "apigateway": "ApiGateway",
             "cloudfront": "CloudFront", "cloudwatch": "CloudWatch", "dynamodb": "DynamoDB", "route53": "Route53",
             "secretsmanager": "SecretsManager", "stepfunctions": "StepFunctions", "elasticache": "ElastiCache",
             "opensearchservice": "OpenSearchService", "eventbridge": "EventBridge", "lambda": "Lambda"}


def pascal(word: str) -> str:
    return AWS_WORDS.get(word, word[:1].upper() + word[1:])


def aws_type(name: str) -> str:
    """`aws_wafv2_web_acl` -> `AWS::WAFv2::WebACL` (D120)."""
    if "::" in name:
        return name
    parts = name.split("_")
    if parts[0] != "aws" or len(parts) < 3:
        return name
    return f"AWS::{pascal(parts[1])}::" + "".join(pascal(p) for p in parts[2:])


def native_value(v):
    """A setting of a native resource as JSON data."""
    from .crypto import Secret
    if isinstance(v, dict):
        return {str(k): native_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [native_value(x) for x in v]
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, Secret):
        return "${secret." + v.name + "}"
    if isinstance(v, Ref):
        return "${attr." + ".".join(v.parts) + "}"
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return show(v)


def build_ctx(plan, r: Resource, st: dict) -> dict:
    from .crypto import Secret
    cloud = plan.cloud
    infra = plan.infra
    sizes = PROVIDERS["sizes"].get(cloud, {})
    base = slug(f"{infra.name}-{r.name}")
    ctx: dict = {"base": base, "ident": re.sub(r"[^a-z0-9]", "", r.name.lower()) or "nexo", "short": base[:32].strip("-")}
    ctx["bucket"] = base[:63]
    ctx["account"] = re.sub(r"[^a-z0-9]", "", base)[:24]
    aid = base[:30].strip("-")
    ctx["account_id"] = aid if len(aid) >= 6 else (aid + "-nexo")[:30]
    public = st.get("public", False)
    ctx["private"] = not public
    ctx["public_access"] = "Enabled" if public else "Disabled"
    ctx["blob_access"] = "Blob" if public else "None"
    ctx["access_prevention"] = "inherited" if public else "enforced"
    ctx["s3_encryption"] = "AES256"
    ctx["scheme"] = "internet-facing" if public else "internal"
    ctx["k8s_service_type"] = "LoadBalancer" if public else "ClusterIP"
    ctx["run_ingress"] = "INGRESS_TRAFFIC_ALL" if public else "INGRESS_TRAFFIC_INTERNAL_ONLY"
    size = st.get("size")
    for table, values in sizes.items():
        if size in values:
            ctx[table] = values[size]
    if "storage" in st:
        gb = max(1, -(-st["storage"] // 1024))
        ctx["storage_gb"], ctx["storage_gb_text"], ctx["storage_gi"] = gb, str(gb), f"{gb}Gi"
    if r.kind == "storage":
        ctx["size_gi"] = f"{max(1, -(-st['capacity'] // 1024))}Gi"
    if r.kind == "database":
        pg = st["type"] == "postgres"
        ctx["az_db_type"] = "Microsoft.DBforPostgreSQL/flexibleServers" if pg else "Microsoft.DBforMySQL/flexibleServers"
        ctx["az_db_version"] = "16" if pg else "8.0.21"
        ctx["gcp_db_version"] = "POSTGRES_16" if pg else "MYSQL_8_0"
        ctx["k8s_db_image"] = "postgres:16" if pg else "mysql:8.4"
        ctx["db_port"] = 5432 if pg else 3306
        ctx["k8s_db_name_var"] = "POSTGRES_DB" if pg else "MYSQL_DATABASE"
        ctx["k8s_db_trust_var"] = "POSTGRES_HOST_AUTH_METHOD" if pg else "MYSQL_ALLOW_EMPTY_PASSWORD"
        ctx["k8s_db_trust"] = "trust" if pg else "yes"
    for key in ("replicas", "nodes"):
        if key in st:
            lo, hi = st[key]["min"], st[key]["max"]
            if r.scale is not None and key == "replicas":
                lo, hi = r.scale[0], r.scale[1]
            ctx[f"{key}_min"], ctx[f"{key}_max"] = lo, hi
    if r.kind == "app":
        ctx["autoscale"] = ctx["replicas_max"] > ctx["replicas_min"]
        target = r.scale[2] if r.scale is not None else 70
        ctx["cpu_target"], ctx["cpu_target_text"] = target, str(target)
        cpu, ram = st["cpu"], st["ram"]
        ctx["ecs_cpu"] = str(int(round(cpu * 1024)))
        ctx["ram_text"] = str(ram)
        ctx["ram_mi"] = f"{ram}Mi"
        gi = Decimal(ram) / Decimal(1024)
        ctx["ram_gi"] = f"{gi.normalize():f}Gi"
        ctx["run_cpu"] = "1" if cpu <= 1 else str(int(-(-cpu // 1)))
        ctx["k8s_cpu"] = f"{int(round(cpu * 1000))}m"
        # the program knows its stage (D115)
        env, secrets = [{"name": "NEXO_STAGE", "value": plan.stage or "default"}], []
        for name, value, e in r.env:
            if isinstance(value, Secret):
                secrets.append({"key": slug(name), "name": name, "value": "${secret." + value.name + "}"})
            elif isinstance(value, Ref):
                head = "${peer." if len(value.parts) == 3 else "${attr."
                env.append({"name": name, "value": head + ".".join(value.parts) + "}"})
            elif isinstance(value, (str, int, float, bool, Decimal)):
                env.append({"name": name, "value": text_of(value) if not isinstance(value, Decimal) else str(value)})
            else:
                raise e.error("env_value", name=name)
        ctx["env"], ctx["secrets"] = env, secrets
        ctx["secret_data"] = {s["name"]: s["value"] for s in secrets}
    if r.kind == "network":
        n, prefix = cidr_parts(st["range"])
        if prefix > 19:
            raise NexoError("infra_range", r.values["range"][1].span if "range" in r.values else r.span, range=st["range"])
        n &= ~((1 << (32 - prefix)) - 1) & 0xFFFFFFFF
        ctx["subnet_a"] = cidr_text(n + 256, 24)
        ctx["subnet_b"] = cidr_text(n + 512, 24)
        ctx["subnet_wide"] = cidr_text(n + 4096, 20)
        ctx["public_net"] = not st["private"]
        ingress = []
        for i, line in enumerate(r.lines):
            w = line.words()
            port = line.options.get("port")
            if (len(w) != 5 or w[0] != "allow" or w[1] != "from" or w[3] != "to" or not isinstance(w[2], str)
                    or not isinstance(port, int) or isinstance(port, bool) or set(line.options) != {"port"}
                    or len(line.head) != 1):
                raise line.error("allow_form")
            src = w[2]
            if src == "internet":
                cidr, source = "0.0.0.0/0", "Internet"
            elif re.match(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$", src):
                cidr = source = src
            elif infra.resource(src) is not None:
                cidr = source = st["range"]
            else:
                raise line.error("infra_ref", key="from", kind="network", name=src)
            if infra.resource(w[4]) is None:
                raise line.error("infra_ref", key="to", kind="app", name=w[4])
            ingress.append({"port": port, "port_text": str(port), "cidr": cidr, "source": source, "priority": 100 + 10 * i})
        ctx["ingress"] = ingress
    if r.kind == "identity":
        ctx["grants"] = grants(plan, r)
    if r.kind == "dns":
        domain = r.name
        ctx["domain"] = domain
        ctx["host"], _, zone = domain.partition(".")
        ctx["zone"] = zone or domain
        ctx["zone_id"] = slug(ctx["zone"])
        ctx["target"] = r.target
        ctx["target_base"] = slug(f"{infra.name}-{r.target}")
        ctx["https"] = r.https
        ctx["scheme_dns"] = "https" if r.https else "http"
        ctx["cert_mode"] = "AUTOMATIC" if r.https else "NONE"
    if r.kind == "server" and cloud == "azure":
        parts = st["image"].split(":")
        if len(parts) != 4:
            raise NexoError("server_image", r.values["image"][1].span, image=st["image"])
        ctx["az_image"] = dict(zip(("publisher", "offer", "sku", "version"), parts))
    if r.kind == "native":
        values = {k: native_value(v) for k, v in st.items()}
        if cloud == "aws":
            ctx["native_type"] = aws_type(r.native_type)
            ctx["native_body"] = {"".join(pascal(p) for p in k.split("_")): v for k, v in values.items()}
        else:
            ctx["native_type"] = r.native_type
            ctx["native_api"] = values.pop("api", "")
            ctx["native_plural"] = values.pop("plural", "")
            ctx["native_url"] = values.pop("url", "")
            ctx["native_body"] = values
    return ctx


def grants(plan, r: Resource) -> list:
    out = []
    cloud = plan.cloud
    roles = PROVIDERS["roles"][cloud]
    for i, line in enumerate(r.lines):
        w = line.words()
        if (len(w) != 4 or w[0] != "can" or w[1] not in ("read", "write") or w[2] not in roles
                or not isinstance(w[3], str) or line.options or len(line.head) != 1):
            raise line.error("can_form")
        verb, kind, name = w[1], w[2], w[3]
        target = plan.infra.resource(name)
        if target is None or target.kind != kind:
            raise line.error("infra_ref", key="can", kind=kind, name=name)
        tbase = slug(f"{plan.infra.name}-{name}")
        role = roles[kind][verb]
        g = {"index": i}
        if cloud == "aws":
            res = {"storage": [f"arn:aws:s3:::{tbase[:63]}", f"arn:aws:s3:::{tbase[:63]}/*"],
                   "queue": ["${res." + name + ".queue.Arn}"], "database": ["${res." + name + ".db.DBInstanceArn}"],
                   "cache": ["${res." + name + ".cache.ARN}"]}[kind]
            g.update(actions=role, resources=res)
        elif cloud == "azure":
            main = {"storage": "account", "queue": "namespace", "database": "server", "cache": "cache"}[kind]
            g.update(role=role, scope="${res." + name + "." + main + ".id}",
                     guid=guid(f"{plan.infra.name}/{r.name}/{i}/{kind}/{name}"))
        elif cloud == "gcp":
            p = plan.project
            res = {"storage": f"https://storage.googleapis.com/storage/v1/b/{tbase[:63]}/iam",
                   "queue": f"https://pubsub.googleapis.com/v1/projects/{p}/topics/{tbase}"}.get(
                kind, f"https://cloudresourcemanager.googleapis.com/v1/projects/{p}")
            g.update(role=role, resource=res)
        else:
            group, plural = ("", "persistentvolumeclaims") if kind == "storage" else ("apps", "statefulsets")
            g.update(group=group, plural=plural, target=tbase, verbs=role)
        out.append(g)
    return out


# ------------------------------------------------------------------ plan

class NativeRes:
    """One native resource: its place in the plan and what to send."""

    def __init__(self, nid, resource, kind, desc, deps):
        self.id, self.resource, self.kind = nid, resource, kind
        self.desc = desc  # the expanded template: type, name, body and the API's fields
        self.deps = deps

    @property
    def type(self):
        return self.desc.get("type", "")

    @property
    def name(self):
        return self.desc.get("name", "")


def secret_hash(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return UNKNOWN + ":secret." + name
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class Plan:
    def __init__(self, infra: Infra, stage: str | None):
        self.infra, self.stage = infra, stage
        self.values = infra_settings(infra, stage)
        self.cloud = self.values["cloud"]
        self.region = self.values.get("region", "")
        if self.cloud != "k8s" and not self.region:
            raise NexoError("infra_region", infra.span, infra=infra.name, cloud=self.cloud)
        self.project = self.values.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "?"
        self.subscription = self.values.get("subscription") or os.environ.get("AZURE_SUBSCRIPTION_ID") or "?"
        self.namespace = self.values.get("namespace") or slug(infra.name)
        self.settings = {}
        self.ctx = {}
        self.peers: dict = {}
        self.state = None  # loaded by the commands
        self.fresh: dict = {}  # outputs read while applying: native id -> read
        self.reveal = False  # apply: secrets are their values
        for r in infra.resources:
            if self.cloud == "k8s" and r.kind in ("server", "cluster"):
                raise NexoError("infra_target", r.span, kind=r.kind, cloud=self.cloud)
            st = effective(infra, r, stage)
            if r.kind in ("app", "database", "server", "cluster", "cache") and self.cloud == "aws" and "network" not in st:
                nets = [x for x in infra.resources if x.kind == "network"]
                if len(nets) != 1:
                    raise NexoError("infra_needs_network", r.span, kind=r.kind, name=r.name)
                st["network"] = nets[0].name
            if self.cloud == "k8s" and r.kind == "database" and stage == "prod":
                raise NexoError("infra_dev_only", r.span, name=r.name)
            if r.kind == "app" and "cluster" in st and self.cloud != "k8s":
                raise NexoError("not_supported", r.values["cluster"][1].span, what='"app ... cluster ="')
            self.settings[r.name] = st
        for r in infra.resources:
            self.ctx[r.name] = build_ctx(self, r, self.settings[r.name])

    # -- what the templates read ---------------------------------------
    def tags(self, r: Resource | None) -> dict:
        out = {"nexo": self.infra.name}
        out.update(self.values.get("tags", {}))
        if r is not None:
            out.update(self.settings[r.name].get("tags", {}))
        return out

    def output(self, nid: str, path: str, deps: set):
        deps.add(nid)
        read = self.fresh.get(nid)
        if read is None and self.state is not None:
            entry = self.state.get(nid)
            read = entry.get("read") if entry else None
        if read is None:
            return UNKNOWN + ":" + nid + "." + path
        v = lookup(path, read) if path else read
        return v if v is not None else UNKNOWN + ":" + nid + "." + path

    def resolver(self, r: Resource | None, deps: set):
        tags = self.tags(r)

        def resolve(path: str):
            head, _, rest = path.partition(".")
            if head == "name":
                return r.name if r else self.infra.name
            if head == "infra":
                return self.infra.name
            if head == "region":
                return self.region
            if head == "project":
                return self.project
            if head == "subscription":
                return self.subscription
            if head == "namespace":
                return self.namespace
            if head == "stage":
                return self.stage or "default"
            if head == "tags":
                return dict(tags)
            if head in ("aws_tags", "aws_tags_named"):
                items = dict(tags)
                if head == "aws_tags_named" and r is not None:
                    items["Name"] = self.ctx[r.name]["base"]
                return [{"Key": k, "Value": v} for k, v in sorted(items.items())]
            if head == "labels":
                return {slug(k, "_"): slug(v, "_") for k, v in sorted(tags.items())}
            if head == "k8s_labels":
                return {"app.kubernetes.io/managed-by": "nexo", "nexo.dev/infra": slug(self.infra.name)}
            if head == "set":
                return lookup(rest, self.settings[r.name])
            if head == "ctx":
                return lookup(rest, self.ctx[r.name])
            if head == "own":
                nid, _, p = rest.partition(".")
                return self.output(f"{r.name}.{nid}", p, deps)
            if head == "link":
                key, _, more = rest.partition(".")
                target = self.settings[r.name].get(key)
                if target is None:
                    return None
                nid, _, p = more.partition(".")
                return self.output(f"{target}.{nid}", p, deps)
            if head == "res":
                name, _, more = rest.partition(".")
                nid, _, p = more.partition(".")
                return self.output(f"{name}.{nid}", p, deps)
            if head == "attr":
                name, _, attr = rest.partition(".")
                return self.attr(name, attr, deps)
            if head == "peer":
                return self.peer_attr(rest.split("."))
            if head == "secret":
                if self.reveal:
                    value = os.environ.get(rest)
                    if value is None:
                        raise NexoError("infra_secret", self.infra.span, name=rest)
                    return value
                return secret_hash(rest)
            return None
        return resolve

    def peer_attr(self, parts: list):
        """A value of another infra block, read from its state (D121)."""
        peer = self.peers.get(parts[0])
        if peer is None:
            infra = next(i for i in INFRAS if i.name == parts[0])
            peer = Plan(infra, self.stage)
            peer.state = State(peer, store_of(peer).load())
            self.peers[parts[0]] = peer
        return peer.attr(parts[1], parts[2], set())

    def attr(self, name: str, attr: str, deps: set):
        r = self.infra.resource(name)
        t = PROVIDERS["resources"][self.cloud][r.kind]["attrs"].get(attr)
        if t is None:
            return None
        return expand(t, self.resolver(r, deps))

    # -- the native resources -------------------------------------------
    def natives(self) -> list[NativeRes]:
        out: list[NativeRes] = []
        for t in PROVIDERS["infra"][self.cloud]:
            deps: set = set()
            desc = expand(t, self.resolver(None, deps))
            out.append(NativeRes(desc["id"], "", "infra", desc, deps))
        top = [n.id for n in out]
        for r in self.infra.resources:
            templates = PROVIDERS["resources"][self.cloud][r.kind]["natives"]
            for t in templates:
                for desc, deps in self.expand_native(r, t):
                    out.append(NativeRes(f"{r.name}.{desc['id']}", r.name, r.kind, desc, deps | set(top)))
        return order(out)

    def expand_native(self, r: Resource, t: dict):
        if "$each" in t:
            seq = self.resolver(r, set())(t["$each"]) or []
            for item in seq:
                deps: set = set()
                base = self.resolver(r, deps)

                def inner(path, item=item, base=base):
                    if path == "item" or path.startswith("item."):
                        return item if path == "item" else lookup(path[5:], item)
                    return base(path)
                yield expand(t["do"], inner), deps
            return
        if "$if" in t:
            deps = set()
            if truthy(self.resolver(r, deps)(t["$if"])):
                yield from self.expand_native(r, t["then"])
            return
        deps = set()
        yield expand(t, self.resolver(r, deps)), deps


def order(natives: list[NativeRes]) -> list[NativeRes]:
    """Each native after the ones it reads from, otherwise in the order
    the program writes them."""
    ids = {n.id for n in natives}
    done: set = set()
    out = []
    pending = list(natives)
    while pending:
        for i, n in enumerate(pending):
            if all(d in done or d not in ids or d == n.id for d in n.deps):
                out.append(n)
                done.add(n.id)
                pending.pop(i)
                break
        else:  # a cycle: keep the written order
            out.extend(pending)
            break
    return out


def has_unknown(v) -> bool:
    if isinstance(v, str):
        return v.startswith(UNKNOWN)
    if isinstance(v, list):
        return any(has_unknown(x) for x in v)
    if isinstance(v, dict):
        return any(has_unknown(x) for x in v.values())
    return False


def changed_keys(old, new, prefix="") -> list[str]:
    """The paths that differ between two bodies (an unknown value differs)."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in list(new) + [k for k in old if k not in new]:
            out += changed_keys(old.get(k), new.get(k), f"{prefix}.{k}" if prefix else k)
        return out
    if has_unknown(new) or old != new:
        return [prefix or "(body)"]
    return []


class Action:
    def __init__(self, op, native, keys=(), entry=None):
        self.op, self.native, self.keys, self.entry = op, native, list(keys), entry


def diff(plan: Plan) -> list[Action]:
    natives = plan.natives()
    actions = []
    for n in natives:
        entry = plan.state.get(n.id)
        if entry is None:
            actions.append(Action("create", n))
            continue
        keys = changed_keys(entry.get("desc", {}).get("body"), n.desc.get("body"))
        # a value kept beside the body (a GCP secret version: its hash)
        if entry.get("desc", {}).get("value") != n.desc.get("value"):
            keys.append("value")
        if entry.get("desc", {}).get("type") != n.type or entry.get("desc", {}).get("name") != n.name:
            actions.append(Action("replace", n, ["type" if entry["desc"].get("type") != n.type else "name"], entry))
        elif keys:
            actions.append(Action("update", n, keys, entry))
        else:
            actions.append(Action("keep", n, (), entry))
    wanted = {n.id for n in natives}
    for nid, entry in reversed(list(plan.state.items())):
        if nid not in wanted:
            actions.append(Action("delete", None, (), entry))
    return actions


# ----------------------------------------------------- checks and costs

def policy_errors(plan: Plan):
    infra = plan.infra
    from . import privacy
    allowed_areas = []
    for line in infra.policy:
        w = line.words()
        if len(w) == 3 and w[0] == "deny" and w[1] == "public" and w[2] in KINDS and not line.options:
            for r in infra.resources:
                if r.kind == w[2] and plan.settings[r.name].get("public"):
                    raise NexoError("policy_public", r.span, infra=infra.name, kind=r.kind, name=r.name)
        elif w == ["require", "encryption"] and not line.options:
            for r in infra.resources:
                if plan.settings[r.name].get("encryption") is False:
                    raise NexoError("policy_encryption", r.span, infra=infra.name, kind=r.kind, name=r.name)
        elif len(w) == 3 and w[:2] == ["require", "tags"] and isinstance(w[2], list) and all(isinstance(t, str) for t in w[2]):
            for r in infra.resources:
                missing = [t for t in w[2] if t not in plan.tags(r)]
                if missing:
                    raise NexoError("policy_tags", r.span, infra=infra.name, kind=r.kind, name=r.name,
                                    tags=", ".join(missing))
        elif len(w) == 5 and w[:4] == ["allow", "personal", "data", "to"] and isinstance(w[4], str):
            allowed_areas.append(w[4])
        else:
            raise line.error("policy_rule", rule=show_phrase(line))
    # section 23, rule 3
    want = plan.values.get("data_region")
    if want and privacy.PRIVACY and plan.cloud != "k8s":
        area = area_of(plan.cloud, plan.region)
        if area != want and area not in allowed_areas:
            span = infra.values.get("region", infra.values["cloud"])[1].span
            raise NexoError("data_region", span, infra=infra.name, region=plan.region, area=want)


def area_of(cloud: str, region: str) -> str:
    a = PROVIDERS["areas"][cloud]
    if "names" in a:
        return a["names"].get(region, region)
    head = region.split("-")[0]
    return a["prefix"].get(head, head)


def cents(x: float) -> int:
    return int(round(x * 100))


def resource_cost(plan: Plan, r: Resource) -> int:
    """Estimated cents of EUR per month."""
    c = PROVIDERS["cost"][plan.cloud]
    st = plan.settings[r.name]
    ctx = plan.ctx[r.name]
    if r.kind == "app":
        per = int(round(st["cpu"] * 1000)) * cents(c["app_vcpu"]) // 1000 + st["ram"] * cents(c["app_gb"]) // 1024
        return (per + cents(c["app_fixed"]) // max(1, ctx["replicas_min"])) * max(1, ctx["replicas_min"])
    if r.kind in ("server", "database", "cache"):
        price = cents(c[r.kind].get(st["size"], 0.0))
        if r.kind == "database":
            price += ctx["storage_gb"] * cents(c["database_gb"])
        return price
    if r.kind == "cluster":
        return cents(c["cluster"]) + ctx["nodes_min"] * cents(c["cluster_node"].get(st["size"], 0.0))
    return cents(c.get(r.kind, 0.0)) if isinstance(c.get(r.kind, 0.0), (int, float)) else 0


def availability(plan: Plan) -> float:
    sla = PROVIDERS["sla"][plan.cloud]
    a = 1.0
    for r in plan.infra.resources:
        if r.kind in ("app", "server", "database", "storage", "cache", "queue", "cluster", "dns"):
            a *= sla[r.kind] / 100.0
    return a * 100.0


def money_text(c: int) -> str:
    return f"{c // 100}.{c % 100:02d}"


def budget_cents(plan: Plan):
    v = plan.values.get("budget")
    if v is None:
        return None
    entry = plan.infra.values["budget"][1]
    if not (isinstance(v, list) and len(v) == 3 and isinstance(v[0], Money) and v[1] == "per" and v[2] == "month"
            and v[0].currency == "EUR"):
        raise entry.error("budget_form")
    return int(v[0].amount * 100)


# ----------------------------------------------------------------- state

STATE_DIR = Path(".nexo") / "state"


class State:
    """What was created, native by native, in the order it was created."""

    def __init__(self, plan: Plan, data: dict | None):
        self.plan = plan
        data = data or {}
        self.serial = data.get("serial", 0)
        self.entries: dict = {e["id"]: e for e in data.get("resources", [])}
        self.outputs: dict = data.get("outputs", {})

    def get(self, nid):
        return self.entries.get(nid)

    def items(self):
        return list(self.entries.items())

    def data(self) -> dict:
        p = self.plan
        return {"infra": p.infra.name, "cloud": p.cloud, "stage": p.stage or "default", "serial": self.serial,
                "resources": list(self.entries.values()), "outputs": self.outputs}


def state_name(plan: Plan) -> str:
    return f"{slug(plan.infra.name)}-{slug(plan.stage or 'default')}.json"


class LocalStore:
    """The state in a local file, locked while a command changes it."""

    def __init__(self, plan: Plan):
        self.path = STATE_DIR / state_name(plan)
        self.lock_path = self.path.with_suffix(".lock")

    def where(self) -> str:
        return str(self.path)

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def lock(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.close(fd)
        return True

    def unlock(self) -> None:
        try:
            self.lock_path.unlink()
        except OSError:
            pass


def store_of(plan: Plan):
    v = plan.values.get("state")
    if v is None:
        return LocalStore(plan)
    from .infra_cloud import S3Store
    if isinstance(v, StateTarget) and v.kind == "s3":
        return S3Store(plan, v.name)
    entry = plan.infra.values["state"][1]
    raise entry.error("state_form")


class StateTarget(Native):
    """`s3("bucket")`: where the state of an infra block lives."""

    type_name = "state"
    __slots__ = ("kind", "name")

    def __init__(self, kind, name):
        self.kind, self.name = kind, name

    def repr(self) -> str:
        return f'{self.kind}("{self.name}")'


def make_state_target(kind):
    def b(call, name):
        if not isinstance(name, str):
            from .core import expects
            raise expects(call, kind, "str", name)
        if kind != "s3":
            raise NexoError("not_supported", call.span, what=f'"state = {kind}(...)"')
        return StateTarget(kind, name)
    return b


# ---------------------------------------------------------------- output

SYMBOLS = {"create": "+", "update": "~", "replace": "-/+", "delete": "-", "keep": "="}


def shown(v) -> str:
    """A value of a plan as text; one only known after applying says so."""
    if isinstance(v, str) and UNKNOWN in v:
        return m("info.unknown_value")
    return v if isinstance(v, str) else text_of(v)


def plan_lines(plan: Plan, actions: list[Action]) -> list[str]:
    where = f", {plan.region}" if plan.region else ""
    out = [m("info.plan_header", infra=plan.infra.name, cloud=plan.cloud, region=where, stage=plan.stage or "default")]
    groups: dict = {}
    for a in actions:
        n = a.native
        rid = n.resource if n else a.entry["resource"]
        kind = n.kind if n else a.entry["kind"]
        groups.setdefault(rid, (kind, []))[1].append(a)
    names = [""] + [r.name for r in plan.infra.resources]
    for rid in names + [g for g in groups if g not in names]:
        if rid not in groups:
            continue
        kind, acts = groups[rid]
        ops = {a.op for a in acts}
        if ops == {"keep"}:
            sym = "="
        elif ops == {"create"}:
            sym = "+"
        elif ops == {"delete"}:
            sym = "-"
        else:
            sym = "~"
        title = f"infra {plan.infra.name}" if rid == "" else f"{kind} {rid}"
        out.append(f"  {sym} {title}")
        if sym == "=":
            continue
        for a in acts:
            if a.op == "keep":
                continue
            d = a.native.desc if a.native else a.entry["desc"]
            keys = f": {', '.join(a.keys)}" if a.op in ("update", "replace") and a.keys else ""
            out.append(f"      {SYMBOLS[a.op]} {shown(d.get('type', ''))} {shown(d.get('name', ''))}{keys}")
    count = lambda *ops: sum(1 for a in actions if a.op in ops)
    if not count("create", "update", "replace", "delete"):
        out.append(m("info.plan_none"))
    else:
        out.append(m("info.plan_summary", create=count("create"), change=count("update", "replace"), delete=count("delete")))
    total = sum(resource_cost(plan, r) for r in plan.infra.resources)
    budget = budget_cents(plan)
    if budget is None:
        out.append(m("info.plan_cost", cost=money_text(total)))
    else:
        out.append(m("info.plan_cost_budget", cost=money_text(total), budget=money_text(budget)))
    out.append(m("info.plan_availability", pct=f"{availability(plan):.2f}"))
    return out


def check_plan(plan: Plan) -> None:
    """The mistakes that stop a plan: policy, data region, budget."""
    policy_errors(plan)
    budget = budget_cents(plan)
    total = sum(resource_cost(plan, r) for r in plan.infra.resources)
    if budget is not None and total > budget:
        raise NexoError("over_budget", plan.infra.values["budget"][1].span, infra=plan.infra.name,
                        cost=money_text(total), budget=money_text(budget))


def cost_lines(plan: Plan) -> list[str]:
    out = [m("info.cost_header", infra=plan.infra.name, cloud=plan.cloud, stage=plan.stage or "default")]
    total = 0
    width = max([len(f"{r.kind} {r.name}") for r in plan.infra.resources] + [0])
    for r in plan.infra.resources:
        c = resource_cost(plan, r)
        total += c
        out.append(f"  {(r.kind + ' ' + r.name).ljust(width)}  {money_text(c).rjust(9)} EUR")
    out.append(m("info.cost_total", cost=money_text(total)))
    return out


def exports() -> dict:
    return {
        "__infra": Builtin("__infra", b_infra),
        "__stage": Builtin("__stage", b_stage),
        "s3": Builtin("s3", make_state_target("s3")),
        "gcs": Builtin("gcs", make_state_target("gcs")),
        "blob": Builtin("blob", make_state_target("blob")),
    }
