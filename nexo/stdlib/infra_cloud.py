"""The commands of nexo.infra (D120, D121) and the clients of each target's
API: AWS Cloud Control (signed with SigV4), Azure Resource Manager, the
REST APIs of Google Cloud and the Kubernetes API.

Credentials and addresses come from the environment, as each cloud's own
tools read them: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
AWS_SESSION_TOKEN and AWS_ENDPOINT_URL; AZURE_TENANT_ID, AZURE_CLIENT_ID,
AZURE_CLIENT_SECRET, AZURE_SUBSCRIPTION_ID and AZURE_AUTHORITY_HOST;
GOOGLE_OAUTH_ACCESS_TOKEN and GOOGLE_CLOUD_PROJECT; the service account of
a pod (KUBERNETES_SERVICE_HOST). NEXO_AZURE_ENDPOINT, NEXO_GCP_ENDPOINT,
NEXO_K8S_SERVER and NEXO_K8S_TOKEN point them elsewhere (an emulator);
NEXO_K8S_CA names the certificate authority of a cluster."""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import errors
from ..errors import NexoError
from . import infra as I

POLL = (0.2, 0.5, 1, 2, 5)
MAX_WAIT = 1800


CAPTURE: list = []  # plan() in a pipeline step keeps the lines (D122)


def say(text: str = "") -> None:
    if CAPTURE:
        CAPTURE[-1].append(text)
        return
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# ------------------------------------------------------------------ http

def http(method: str, url: str, headers: dict, body: bytes | None = None, context=None):
    """(status, headers, body); a network failure is a diagnostic."""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120, context=context) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise NexoError("infra_network", None, url=url, reason=str(reason))


def error_text(body: bytes) -> str:
    try:
        d = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace").strip()[:300]
    if isinstance(d, dict):
        e = d.get("error")
        if isinstance(e, dict) and isinstance(e.get("message"), str):
            return e["message"]
        for k in ("Message", "message"):
            if isinstance(d.get(k), str):
                return d[k]
    return json.dumps(d, ensure_ascii=False)[:300]


def fail(what: str, status: int, body: bytes):
    raise NexoError("infra_api", None, what=what, status=status, reason=error_text(body))


def compact(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def need_env(*names) -> list[str]:
    out = []
    for n in names:
        v = os.environ.get(n)
        if not v:
            raise NexoError("infra_credentials", None, name=n)
        out.append(v)
    return out


# ------------------------------------------------------------------- aws

def sigv4(method: str, url: str, headers: dict, body: bytes, service: str, region: str) -> dict:
    """The headers of a request signed with AWS Signature Version 4."""
    key_id, secret = need_env("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    token = os.environ.get("AWS_SESSION_TOKEN")
    now = dt.datetime.now(dt.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    u = urllib.parse.urlsplit(url)
    payload = hashlib.sha256(body).hexdigest()
    h = {k.lower(): v for k, v in headers.items()}
    h["host"] = u.netloc
    h["x-amz-date"] = amz_date
    h["x-amz-content-sha256"] = payload
    if token:
        h["x-amz-security-token"] = token
    path = urllib.parse.quote(urllib.parse.unquote(u.path or "/"), safe="/-_.~")
    pairs = sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in pairs)
    names = sorted(h)
    canonical = "\n".join([method, path, query, "".join(f"{n}:{h[n].strip()}\n" for n in names), ";".join(names), payload])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + secret).encode()
    for part in (day, region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = (f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={';'.join(names)}, "
                          f"Signature={signature}")
    return h


class Aws:
    """AWS Cloud Control: one API for every resource type."""

    def __init__(self, plan):
        self.region = plan.region
        self.endpoint = os.environ.get("AWS_ENDPOINT_URL") or f"https://cloudcontrolapi.{self.region}.amazonaws.com"

    def call(self, op: str, payload: dict, what: str) -> dict:
        body = compact(payload).encode()
        headers = sigv4("POST", self.endpoint.rstrip("/") + "/", {
            "Content-Type": "application/x-amz-json-1.0", "X-Amz-Target": f"CloudApiService.{op}"}, body,
            "cloudcontrolapi", self.region)
        status, _, data = http("POST", self.endpoint.rstrip("/") + "/", headers, body)
        if status >= 400:
            fail(what, status, data)
        return json.loads(data.decode("utf-8") or "{}")

    def wait(self, event: dict, what: str) -> dict:
        waited, i = 0.0, 0
        while event.get("OperationStatus") in ("PENDING", "IN_PROGRESS", "CANCEL_IN_PROGRESS"):
            pause = POLL[min(i, len(POLL) - 1)]
            time.sleep(pause)
            waited += pause
            i += 1
            if waited > MAX_WAIT:
                raise NexoError("infra_timeout", None, what=what)
            event = self.call("GetResourceRequestStatus", {"RequestToken": event["RequestToken"]}, what)["ProgressEvent"]
        if event.get("OperationStatus") != "SUCCESS":
            raise NexoError("infra_api", None, what=what, status=event.get("ErrorCode", "?"),
                            reason=event.get("StatusMessage", ""))
        return event

    def read(self, entry_desc: dict, ident: str, what: str):
        try:
            d = self.call("GetResource", {"TypeName": entry_desc["type"], "Identifier": ident}, what)
        except NexoError as e:
            if e.args_.get("status") == 400 and "NotFound" in str(e.args_.get("reason", "")) or e.args_.get("status") == 404:
                return None
            raise
        return json.loads(d["ResourceDescription"]["Properties"])

    def create(self, desc: dict, body: dict, what: str):
        e = self.call("CreateResource", {"TypeName": desc["type"], "DesiredState": compact(body),
                                         "ClientToken": token_of(desc, body)}, what)["ProgressEvent"]
        e = self.wait(e, what)
        ident = e["Identifier"]
        return ident, self.read(desc, ident, what)

    def update(self, desc: dict, ident: str, old: dict, body: dict, what: str):
        patch = []
        for k, v in body.items():
            if k not in (old or {}):
                patch.append({"op": "add", "path": "/" + k, "value": v})
            elif old[k] != v:
                patch.append({"op": "replace", "path": "/" + k, "value": v})
        for k in old or {}:
            if k not in body:
                patch.append({"op": "remove", "path": "/" + k})
        e = self.call("UpdateResource", {"TypeName": desc["type"], "Identifier": ident, "PatchDocument": compact(patch),
                                         "ClientToken": token_of(desc, body)}, what)["ProgressEvent"]
        self.wait(e, what)
        return ident, self.read(desc, ident, what)

    def delete(self, desc: dict, ident: str, what: str) -> None:
        e = self.call("DeleteResource", {"TypeName": desc["type"], "Identifier": ident,
                                         "ClientToken": token_of(desc, {"delete": ident})}, what)["ProgressEvent"]
        self.wait(e, what)


def token_of(desc: dict, body) -> str:
    """The same request gets the same ClientToken (retries are safe)."""
    return hashlib.sha256((desc.get("type", "") + desc.get("name", "") + compact(body)).encode()).hexdigest()[:36]


class S3Store:
    """The state as an S3 object, encrypted, locked with a conditional
    write of `<key>.lock`."""

    def __init__(self, plan, bucket: str):
        self.region = plan.region
        self.bucket = bucket
        self.key = "nexo/" + I.state_name(plan)
        endpoint = os.environ.get("AWS_ENDPOINT_URL")
        self.base = f"{endpoint.rstrip('/')}/{bucket}" if endpoint else f"https://{bucket}.s3.{self.region}.amazonaws.com"

    def where(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    def request(self, method, key, body=b"", extra=None):
        url = f"{self.base}/{key}"
        headers = sigv4(method, url, extra or {}, body, "s3", self.region)
        return http(method, url, headers, body if method == "PUT" else None)

    def load(self):
        status, _, data = self.request("GET", self.key)
        if status == 404:
            return None
        if status >= 400:
            fail(self.where(), status, data)
        return json.loads(data.decode("utf-8"))

    def save(self, data: dict) -> None:
        body = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
        status, _, out = self.request("PUT", self.key, body, {"Content-Type": "application/json",
                                                               "x-amz-server-side-encryption": "AES256"})
        if status >= 400:
            fail(self.where(), status, out)

    def lock(self) -> bool:
        status, _, out = self.request("PUT", self.key + ".lock", b"nexo", {"If-None-Match": "*"})
        if status in (409, 412):
            return False
        if status >= 400:
            fail(self.where(), status, out)
        return True

    def unlock(self) -> None:
        self.request("DELETE", self.key + ".lock")


# ----------------------------------------------------------------- azure

class Azure:
    """Azure Resource Manager: PUT, GET and DELETE of each resource's id."""

    def __init__(self, plan):
        self.plan = plan
        self.endpoint = (os.environ.get("NEXO_AZURE_ENDPOINT") or "https://management.azure.com").rstrip("/")
        self.token = None
        need_env("AZURE_SUBSCRIPTION_ID") if plan.subscription == "?" else None

    def auth(self) -> str:
        if self.token is None:
            tenant, client, secret = need_env("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
            host = (os.environ.get("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com").rstrip("/")
            url = f"{host}/{tenant}/oauth2/v2.0/token"
            form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client, "client_secret": secret,
                                           "scope": "https://management.azure.com/.default"}).encode()
            status, _, data = http("POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, form)
            if status >= 400:
                fail(url, status, data)
            self.token = json.loads(data.decode("utf-8"))["access_token"]
        return self.token

    def path(self, desc: dict) -> str:
        if desc.get("path"):
            return desc["path"]
        scope = desc.get("scope") or f"/subscriptions/{self.plan.subscription}/resourceGroups/{self.plan.infra.name}"
        types = desc["type"].split("/")
        names = desc["name"].split("/")
        return f"{scope}/providers/{types[0]}/" + "/".join(f"{t}/{n}" for t, n in zip(types[1:], names))

    def url(self, desc: dict, path: str | None = None) -> str:
        return f"{self.endpoint}{path or self.path(desc)}?api-version={desc.get('api', '')}"

    def send(self, method, url, body=None):
        headers = {"Authorization": "Bearer " + self.auth()}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = compact(body).encode()
        return http(method, url, headers, data)

    def wait(self, headers: dict, what: str) -> None:
        url = headers.get("azure-asyncoperation") or headers.get("location")
        if not url:
            return
        waited, i = 0.0, 0
        while True:
            status, h, data = self.send("GET", url)
            if status >= 400:
                fail(what, status, data)
            if headers.get("azure-asyncoperation"):
                state = json.loads(data.decode("utf-8") or "{}").get("status", "Succeeded")
                if state == "Succeeded":
                    return
                if state in ("Failed", "Canceled"):
                    raise NexoError("infra_api", None, what=what, status=state, reason=error_text(data))
            elif status != 202:
                return
            pause = POLL[min(i, len(POLL) - 1)]
            time.sleep(pause)
            waited += pause
            i += 1
            if waited > MAX_WAIT:
                raise NexoError("infra_timeout", None, what=what)

    def read(self, desc: dict, ident: str, what: str):
        status, _, data = self.send("GET", self.url(desc, ident))
        if status == 404:
            return None
        if status >= 400:
            fail(what, status, data)
        return json.loads(data.decode("utf-8"))

    def create(self, desc: dict, body: dict, what: str):
        path = self.path(desc)
        status, headers, data = self.send("PUT", self.url(desc, path), body)
        if status >= 400:
            fail(what, status, data)
        self.wait(headers, what)
        return path, self.read(desc, path, what)

    def update(self, desc: dict, ident: str, old, body: dict, what: str):
        return self.create(desc, body, what)

    def delete(self, desc: dict, ident: str, what: str) -> None:
        status, headers, data = self.send("DELETE", self.url(desc, ident))
        if status >= 400 and status != 404:
            fail(what, status, data)
        self.wait(headers, what)


# ------------------------------------------------------------------- gcp

class Gcp:
    """The REST API of each Google Cloud service."""

    def __init__(self, plan):
        self.plan = plan
        if plan.project == "?":
            need_env("GOOGLE_CLOUD_PROJECT")
        self.endpoint = (os.environ.get("NEXO_GCP_ENDPOINT") or "").rstrip("/")

    def url(self, url: str) -> str:
        if self.endpoint and url.startswith("https://"):
            host, _, rest = url[8:].partition("/")
            if host.endswith(".googleapis.com"):
                return f"{self.endpoint}/{host[:-len('.googleapis.com')]}/{rest}"
        return url

    def send(self, method, url, body=None):
        token, = need_env("GOOGLE_OAUTH_ACCESS_TOKEN")
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = compact(body).encode()
        return http(method, self.url(url), headers, data)

    def wait(self, create_url: str, op: dict, what: str) -> None:
        waited, i = 0.0, 0
        while True:
            if op.get("done") is False and op.get("name"):
                u = urllib.parse.urlsplit(create_url)
                version = u.path.strip("/").split("/")[0]
                poll = f"{u.scheme}://{u.netloc}/{version}/{op['name']}"
            elif str(op.get("kind", "")).endswith("#operation") and op.get("status") not in (None, "DONE") and op.get("selfLink"):
                poll = op["selfLink"]
            else:
                if op.get("error"):
                    raise NexoError("infra_api", None, what=what, status="error", reason=compact(op["error"]))
                return
            pause = POLL[min(i, len(POLL) - 1)]
            time.sleep(pause)
            waited += pause
            i += 1
            if waited > MAX_WAIT:
                raise NexoError("infra_timeout", None, what=what)
            status, _, data = self.send("GET", poll)
            if status >= 400:
                fail(what, status, data)
            op = json.loads(data.decode("utf-8") or "{}")

    def read(self, desc: dict, ident: str, what: str):
        if desc.get("op") == "iam":
            policy = self.policy(ident, what)
            want = desc.get("body", {})
            for b in policy.get("bindings", []):
                if b.get("role") == want.get("role") and want.get("member") in b.get("members", []):
                    return dict(want)
            return None
        status, _, data = self.send("GET", ident)
        if status == 404:
            return None
        if status >= 400:
            fail(what, status, data)
        return json.loads(data.decode("utf-8"))

    def policy(self, resource: str, what: str) -> dict:
        if resource.endswith("/iam"):
            status, _, data = self.send("GET", resource)
        else:
            status, _, data = self.send("POST", resource + ":getIamPolicy", {})
        if status >= 400:
            fail(what, status, data)
        return json.loads(data.decode("utf-8") or "{}")

    def set_policy(self, resource: str, policy: dict, what: str) -> None:
        if resource.endswith("/iam"):
            status, _, data = self.send("PUT", resource, policy)
        else:
            status, _, data = self.send("POST", resource + ":setIamPolicy", {"policy": policy})
        if status >= 400:
            fail(what, status, data)

    def member(self, desc: dict, body: dict, add: bool, what: str) -> None:
        resource = desc["self"]
        policy = self.policy(resource, what)
        bindings = policy.setdefault("bindings", [])
        b = next((x for x in bindings if x.get("role") == body["role"]), None)
        if add:
            if b is None:
                b = {"role": body["role"], "members": []}
                bindings.append(b)
            if body["member"] not in b["members"]:
                b["members"].append(body["member"])
        elif b is not None and body["member"] in b.get("members", []):
            b["members"].remove(body["member"])
            if not b["members"]:
                bindings.remove(b)
        self.set_policy(resource, policy, what)

    def add_version(self, desc: dict, what: str) -> None:
        data = base64.b64encode(str(desc.get("value", "")).encode()).decode()
        status, _, out = self.send("POST", desc["self"] + ":addVersion", {"payload": {"data": data}})
        if status >= 400:
            fail(what, status, out)

    def create(self, desc: dict, body: dict, what: str):
        if desc.get("op") == "iam":
            self.member(desc, body, True, what)
            return desc["self"], dict(body)
        method, _, url = desc["create"].partition(" ")
        status, _, data = self.send(method, url, body)
        if status >= 400:
            fail(what, status, data)
        self.wait(url, json.loads(data.decode("utf-8") or "{}"), what)
        if desc.get("op") == "secret":
            self.add_version(desc, what)
        return desc["self"], self.read(desc, desc["self"], what)

    def update(self, desc: dict, ident: str, old, body: dict, what: str):
        if desc.get("op") == "iam":
            return self.create(desc, body, what)
        if desc.get("op") == "secret":
            self.add_version(desc, what)
            return ident, self.read(desc, ident, what)
        status, _, data = self.send(desc.get("update", "PATCH"), ident, body)
        if status >= 400:
            fail(what, status, data)
        self.wait(desc["create"].partition(" ")[2], json.loads(data.decode("utf-8") or "{}"), what)
        return ident, self.read(desc, ident, what)

    def delete(self, desc: dict, ident: str, what: str) -> None:
        if desc.get("op") == "iam":
            self.member(desc, desc.get("body", {}), False, what)
            return
        status, _, data = self.send("DELETE", ident)
        if status >= 400 and status != 404:
            fail(what, status, data)
        if status < 300:
            self.wait(desc.get("create", " ").partition(" ")[2], json.loads(data.decode("utf-8") or "{}"), what)


# ------------------------------------------------------------------- k8s

class K8s:
    """The Kubernetes API, with server-side apply."""

    def __init__(self, plan):
        self.plan = plan
        server = os.environ.get("NEXO_K8S_SERVER")
        if not server and os.environ.get("KUBERNETES_SERVICE_HOST"):
            server = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
        if not server:
            raise NexoError("infra_credentials", None, name="NEXO_K8S_SERVER")
        self.server = server.rstrip("/")
        self.token = os.environ.get("NEXO_K8S_TOKEN")
        self.context = None
        sa = "/var/run/secrets/kubernetes.io/serviceaccount"
        if not self.token and os.path.exists(f"{sa}/token"):
            self.token = open(f"{sa}/token", encoding="utf-8").read().strip()
            if os.path.exists(f"{sa}/ca.crt"):
                self.context = ssl.create_default_context(cafile=f"{sa}/ca.crt")
        # the certificate authority of a cluster outside (kind, minikube...)
        ca = os.environ.get("NEXO_K8S_CA")
        if ca:
            if not os.path.exists(ca):
                raise NexoError("infra_ca", None, path=ca)
            self.context = ssl.create_default_context(cafile=ca)

    def path(self, desc: dict) -> str:
        api = desc["api"]
        base = "/api/v1" if api == "v1" else f"/apis/{api}"
        ns = "" if desc.get("cluster_scope") else f"/namespaces/{self.plan.namespace}"
        return f"{base}{ns}/{desc['plural']}/{desc['name']}"

    def send(self, method, path, body=None, content="application/json"):
        headers = {}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            headers["Content-Type"] = content
            data = compact(body).encode()
        return http(method, self.server + path, headers, data, self.context)

    def read(self, desc: dict, ident: str, what: str):
        status, _, data = self.send("GET", ident)
        if status == 404:
            return None
        if status >= 400:
            fail(what, status, data)
        return json.loads(data.decode("utf-8"))

    def create(self, desc: dict, body: dict, what: str):
        path = self.path(desc)
        status, _, data = self.send("PATCH", path + "?fieldManager=nexo&force=true", body, "application/apply-patch+yaml")
        if status >= 400:
            fail(what, status, data)
        return path, json.loads(data.decode("utf-8"))

    def update(self, desc: dict, ident: str, old, body: dict, what: str):
        return self.create(desc, body, what)

    def delete(self, desc: dict, ident: str, what: str) -> None:
        status, _, data = self.send("DELETE", ident)
        if status >= 400 and status != 404:
            fail(what, status, data)


CLIENTS = {"aws": Aws, "azure": Azure, "gcp": Gcp, "k8s": K8s}


# -------------------------------------------------------------- commands

def secret_values(plan) -> dict:
    """The value of each secret a program uses -> what the state keeps."""
    out = {}
    for r in plan.infra.resources:
        for item in plan.ctx[r.name].get("secrets", []):
            name = item["value"][len("${secret."):-1]
            value = os.environ.get(name)
            if value:
                h = I.secret_hash(name)
                out[value] = h
                out[base64.b64encode(value.encode()).decode()] = h
    return out


def scrub(v, secrets: dict):
    """What the cloud answers, without the secrets it echoes."""
    if isinstance(v, str):
        return secrets.get(v, v)
    if isinstance(v, list):
        return [scrub(x, secrets) for x in v]
    if isinstance(v, dict):
        return {k: scrub(x, secrets) for k, x in v.items()}
    return v


def native_by_id(plan, nid: str, reveal: bool):
    plan.reveal = reveal
    try:
        return next((n for n in plan.natives() if n.id == nid), None)
    finally:
        plan.reveal = False


def confirm(prompt: str, answers: tuple) -> bool:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    return line.strip().lower() in answers


def take_lock(plan, store) -> None:
    """Lock the state, and check that no other command changed it since
    this one read it (while it asked for confirmation, for example)."""
    if not store.lock():
        raise NexoError("state_locked", None, where=store.where())
    if (store.load() or {}).get("serial", 0) != plan.state.serial:
        store.unlock()
        raise NexoError("state_changed", None, where=store.where())


def outputs_lines(plan) -> list[str]:
    out = []
    for o in plan.infra.outputs:
        if o.secret:
            out.append(f"{o.name} = {I.m('info.secret_output')}")
            continue
        v = plan.peer_attr(o.ref.parts) if len(o.ref.parts) == 3 else plan.attr(o.ref.parts[0], o.ref.parts[1], set())
        out.append(f"{o.name} = {I.text_of(v) if not (isinstance(v, str) and v.startswith(I.UNKNOWN)) else I.m('info.unknown_value')}")
    return out


def apply(plan, store, yes: bool) -> int:
    I.check_plan(plan)
    actions = I.diff(plan)
    for line in I.plan_lines(plan, actions):
        say(line)
    if not any(a.op != "keep" for a in actions):
        for line in outputs_lines(plan):
            say(line)
        return 0
    if not yes:
        if os.environ.get("CI"):
            raise NexoError("apply_needs_yes", None)
        if not confirm(I.m("info.apply_confirm"), ("sí", "si", "s", "yes", "y")):
            say(I.m("info.apply_cancelled"))
            return 1
    take_lock(plan, store)
    counts = {"create": 0, "update": 0, "delete": 0}
    try:
        # inside: a client that cannot start leaves no lock behind
        client = CLIENTS[plan.cloud](plan)
        secrets = secret_values(plan)
        for a in actions:
            if a.op == "keep":
                continue
            if a.op in ("delete", "replace"):
                entry = a.entry
                what = f"{entry['desc'].get('type')} {entry['desc'].get('name')}"
                client.delete(entry["desc"], entry["identifier"], what)
                del plan.state.entries[entry["id"]]
                plan.fresh.pop(entry["id"], None)
                plan.state.serial += 1
                store.save(plan.state.data())
                say(f"  - {what}")
                counts["delete"] += 1
                if a.op == "delete":
                    continue
            n = native_by_id(plan, a.native.id, True)
            shown = native_by_id(plan, a.native.id, False)
            what = f"{n.type} {n.name}"
            if a.op == "update":
                ident, read = client.update(n.desc, a.entry["identifier"], a.entry["desc"].get("body"), n.desc.get("body"), what)
                counts["update"] += 1
                sym = "~"
            else:
                ident, read = client.create(n.desc, n.desc.get("body"), what)
                counts["create"] += 1
                sym = "+"
            plan.fresh[n.id] = read
            plan.state.entries[n.id] = {"id": n.id, "resource": n.resource, "kind": n.kind, "desc": shown.desc,
                                        "identifier": ident, "read": scrub(read, secrets)}
            plan.state.serial += 1
            store.save(plan.state.data())
            say(f"  {sym} {what}")
        plan.state.outputs = {}
        for line in outputs_lines(plan):
            name, _, value = line.partition(" = ")
            plan.state.outputs[name] = value
        store.save(plan.state.data())
    finally:
        store.unlock()
    for line in outputs_lines(plan):
        say(line)
    say(I.m("info.apply_done", create=counts["create"], change=counts["update"], delete=counts["delete"]))
    return 0


def destroy(plan, store) -> int:
    entries = plan.state.items()
    if not entries:
        say(I.m("info.destroy_none", infra=plan.infra.name))
        return 0
    say(I.m("info.destroy_list", infra=plan.infra.name, n=len(entries)))
    for _, e in reversed(entries):
        say(f"  - {e['desc'].get('type')} {e['desc'].get('name')}")
    if not confirm(I.m("info.destroy_confirm", infra=plan.infra.name), (plan.infra.name.lower(),)):
        say(I.m("info.apply_cancelled"))
        return 1
    take_lock(plan, store)
    try:
        client = CLIENTS[plan.cloud](plan)
        for nid, e in reversed(entries):
            what = f"{e['desc'].get('type')} {e['desc'].get('name')}"
            client.delete(e["desc"], e["identifier"], what)
            del plan.state.entries[nid]
            plan.state.serial += 1
            store.save(plan.state.data())
            say(f"  - {what}")
        plan.state.outputs = {}
        store.save(plan.state.data())
    finally:
        store.unlock()
    say(I.m("info.destroy_done", infra=plan.infra.name, n=len(entries)))
    return 0


def differences(want, have, prefix=""):
    """The paths whose value in the cloud is not the one Nexo set."""
    if isinstance(want, dict):
        if not isinstance(have, dict):
            return [(prefix, want, have)]
        out = []
        for k, v in want.items():
            out += differences(v, have.get(k), f"{prefix}.{k}" if prefix else k)
        return out
    if isinstance(want, list) and isinstance(have, list) and len(want) == len(have):
        out = []
        for i, (v, h) in enumerate(zip(want, have)):
            out += differences(v, h, f"{prefix}.{i}" if prefix else str(i))
        return out
    if isinstance(want, str) and (want.startswith(I.UNKNOWN) or want.startswith("sha256:")):
        return []
    if want != have:
        return [(prefix, want, have)]
    return []


def drift(plan) -> int:
    client = CLIENTS[plan.cloud](plan)
    say(I.m("info.drift_header", infra=plan.infra.name, cloud=plan.cloud, stage=plan.stage or "default"))
    found = 0
    for nid, e in plan.state.items():
        desc = e["desc"]
        what = f"{desc.get('type')} {desc.get('name')}"
        have = scrub(client.read(desc, e["identifier"], what), secret_values(plan))
        if have is None:
            say(f"  - {what}: {I.m('info.drift_gone')}")
            found += 1
            continue
        for path, want, now in differences(desc.get("body", {}), have):
            say(f"  ~ {what}: {path} ({I.text_of(want)} -> {I.text_of(now) if now is not None else '-'})")
            found += 1
    say(I.m("info.drift_none") if not found else I.m("info.drift_found", n=found))
    return 0


def import_(plan, store, target: str, ident: str) -> int:
    name, _, nid = target.partition(".")
    r = plan.infra.resource(name)
    if r is None:
        raise NexoError("import_resource", None, name=name, infra=plan.infra.name)
    natives = [n for n in plan.natives() if n.resource == name and (not nid or n.id == f"{name}.{nid}")]
    if len(natives) != 1:
        raise NexoError("import_which", None, name=name, natives=", ".join(n.id for n in plan.natives() if n.resource == name))
    n = natives[0]
    what = f"{n.type} {n.name}"
    client = CLIENTS[plan.cloud](plan)
    read = client.read(n.desc, ident, what)
    if read is None:
        raise NexoError("import_missing", None, what=what, id=ident)
    take_lock(plan, store)
    try:
        plan.state.entries[n.id] = {"id": n.id, "resource": n.resource, "kind": n.kind, "desc": n.desc,
                                    "identifier": ident, "read": scrub(read, secret_values(plan))}
        plan.state.serial += 1
        store.save(plan.state.data())
    finally:
        store.unlock()
    say(I.m("info.imported", what=what, id=ident))
    return 0


def command(interp, name: str, args: list, stage, yes: bool) -> int:
    status = 0
    infras = list(I.INFRAS)
    if name == "import":
        cloud = args[0]
        infras = [x for x in infras if I.infra_settings(x, stage)["cloud"] == cloud]
        if not infras:
            raise NexoError("import_cloud", None, cloud=cloud)
        infras = [next((x for x in infras if x.resource(args[1].partition(".")[0])), infras[0])]
    # destroy: the blocks that use values of others go first
    infras = I.infra_order(infras[::-1])[::-1] if name == "destroy" else I.infra_order(infras)
    for infra in infras:
        plan = I.Plan(infra, stage)
        store = I.store_of(plan)
        plan.state = I.State(plan, store.load())
        if name == "cost":
            for line in I.cost_lines(plan):
                say(line)
        elif name == "plan":
            I.check_plan(plan)
            for line in I.plan_lines(plan, I.diff(plan)):
                say(line)
        elif name == "apply":
            status |= apply(plan, store, yes)
        elif name == "destroy":
            status |= destroy(plan, store)
        elif name == "drift":
            status |= drift(plan)
        else:
            status |= import_(plan, store, args[1], args[2])
    return status
