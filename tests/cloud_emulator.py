"""A local stand-in for the APIs `nexo apply` talks to (D120): AWS Cloud
Control and S3 (with SigV4 checked), the Azure token endpoint and Resource
Manager, the REST APIs of Google Cloud (with long-running operations) and
the Kubernetes API (server-side apply). It keeps what is created in memory
and records every request; `GET /__log` returns them.

    python tests/cloud_emulator.py 8765
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AWS_KEY = "AKIDNEXOTEST"
AWS_SECRET = "nexo-test-secret"
AZURE_TOKEN = "azure-test-token"
GCP_TOKEN = "gcp-test-token"
K8S_TOKEN = "k8s-test-token"


def aws_attrs(type_name: str, props: dict, n: int, region: str) -> tuple[str, dict]:
    """(Identifier, read-only properties) of a new resource."""
    short = type_name.split("::")[-1].lower()
    ident = f"{short}-{n:04d}"
    arn = f"arn:aws:{type_name.split('::')[1].lower()}:{region}:123456789012:{short}/{ident}"
    extra = {"Arn": arn, "Id": ident}
    if type_name == "AWS::EC2::VPC":
        extra["VpcId"] = ident
    elif type_name == "AWS::EC2::Subnet":
        extra["SubnetId"] = ident
    elif type_name == "AWS::EC2::SecurityGroup":
        extra["GroupId"] = ident
    elif type_name == "AWS::EC2::InternetGateway":
        extra["InternetGatewayId"] = ident
    elif type_name == "AWS::SQS::Queue":
        ident = f"https://sqs.{region}.amazonaws.com/123456789012/{props.get('QueueName')}"
        extra["QueueUrl"] = ident
        extra["Arn"] = f"arn:aws:sqs:{region}:123456789012:{props.get('QueueName')}"
    elif type_name == "AWS::S3::Bucket":
        ident = props.get("BucketName", ident)
    elif type_name == "AWS::RDS::DBInstance":
        ident = props.get("DBInstanceIdentifier", ident)
        extra["Endpoint"] = {"Address": f"{ident}.abc.{region}.rds.amazonaws.com", "Port": "5432"}
        extra["DBInstanceArn"] = arn
    elif type_name == "AWS::RDS::DBSubnetGroup":
        ident = props.get("DBSubnetGroupName", ident)
    elif type_name == "AWS::ECS::Cluster":
        ident = props.get("ClusterName", ident)
    elif type_name == "AWS::ECS::TaskDefinition":
        extra["TaskDefinitionArn"] = arn
        ident = arn
    elif type_name == "AWS::ElasticLoadBalancingV2::LoadBalancer":
        extra["LoadBalancerArn"] = arn
        extra["DNSName"] = f"{props.get('Name')}-123.{region}.elb.amazonaws.com"
        ident = arn
    elif type_name == "AWS::ElasticLoadBalancingV2::TargetGroup":
        extra["TargetGroupArn"] = arn
        ident = arn
    elif type_name == "AWS::EKS::Cluster":
        extra["Endpoint"] = f"https://{ident}.eks.amazonaws.com"
    elif type_name == "AWS::ElastiCache::ServerlessCache":
        extra["Endpoint"] = {"Address": f"{ident}.cache.amazonaws.com", "Port": "6379"}
    elif type_name == "AWS::IAM::Role":
        ident = props.get("RoleName", ident)
        extra["Arn"] = f"arn:aws:iam::123456789012:role/{ident}"
    elif type_name == "AWS::SecretsManager::Secret":
        extra["Id"] = arn
        ident = arn
    elif type_name == "AWS::EC2::Instance":
        extra["PrivateIp"] = "10.0.1.10"
    return ident, extra


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.log: list = []
        self.aws: dict = {}  # (type, id) -> props
        self.s3: dict = {}  # path -> bytes
        self.azure: dict = {}  # id -> resource
        self.gcp: dict = {}  # path -> resource
        self.iam: dict = {}  # resource path -> policy
        self.k8s: dict = {}  # path -> object
        self.n = 0
        self.tokens: dict = {}


STATE = State()


def check_sigv4(h, method: str, path: str, query: str, body: bytes) -> bool:
    auth = h.get("authorization", "")
    m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/([^/]+)/aws4_request, "
                 r"SignedHeaders=([^,]+), Signature=([0-9a-f]+)$", auth)
    if not m or m.group(1) != AWS_KEY:
        return False
    day, region, service, signed, signature = m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
    names = signed.split(";")
    if hashlib.sha256(body).hexdigest() != h.get("x-amz-content-sha256"):
        return False
    pairs = sorted(urllib.parse.parse_qsl(query, keep_blank_values=True))
    q = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in pairs)
    canonical = "\n".join([method, urllib.parse.quote(urllib.parse.unquote(path or "/"), safe="/-_.~"), q,
                           "".join(f"{n}:{h.get(n, '').strip()}\n" for n in names), signed, h["x-amz-content-sha256"]])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", h.get("x-amz-date", ""), scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + AWS_SECRET).encode()
    for part in (day, region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest(), signature)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status: int, data=None, headers=None):
        body = b"" if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def handle_any(self, method):
        u = urllib.parse.urlsplit(self.path)
        data = self.body()
        h = {k.lower(): v for k, v in self.headers.items()}
        with STATE.lock:
            STATE.log.append({"method": method, "path": u.path, "query": u.query,
                              "target": h.get("x-amz-target", ""), "body": data.decode("utf-8", "replace")})
        if u.path == "/__log":
            return self.reply(200, STATE.log)
        if "x-amz-target" in h:
            return self.aws(h, method, u, data)
        if h.get("authorization", "").startswith("AWS4-HMAC-SHA256"):
            return self.s3(h, method, u, data)
        if u.path.endswith("/oauth2/v2.0/token"):
            form = dict(urllib.parse.parse_qsl(data.decode()))
            if form.get("client_secret") != "azure-test-secret":
                return self.reply(401, {"error": "invalid_client"})
            return self.reply(200, {"access_token": AZURE_TOKEN, "token_type": "Bearer", "expires_in": 3600})
        if u.path.startswith("/subscriptions/"):
            return self.azure(h, method, u, data)
        if u.path.startswith("/api/") or u.path.startswith("/apis/"):
            return self.k8s(h, method, u, data)
        return self.gcp(h, method, u, data)

    do_GET = lambda self: self.handle_any("GET")
    do_PUT = lambda self: self.handle_any("PUT")
    do_POST = lambda self: self.handle_any("POST")
    do_PATCH = lambda self: self.handle_any("PATCH")
    do_DELETE = lambda self: self.handle_any("DELETE")

    # -- aws -------------------------------------------------------------
    def aws(self, h, method, u, data):
        if not check_sigv4(h, method, u.path, u.query, data):
            return self.reply(403, {"Message": "The request signature we calculated does not match"})
        op = h["x-amz-target"].split(".")[-1]
        req = json.loads(data.decode() or "{}")
        region = h["authorization"].split("/")[2]
        with STATE.lock:
            if op == "CreateResource":
                props = json.loads(req["DesiredState"])
                STATE.n += 1
                ident, extra = aws_attrs(req["TypeName"], props, STATE.n, region)
                STATE.aws[(req["TypeName"], ident)] = {**props, **extra}
                STATE.tokens[f"tok-{STATE.n}"] = ident
                return self.reply(200, {"ProgressEvent": {"TypeName": req["TypeName"], "Identifier": ident,
                                                          "OperationStatus": "IN_PROGRESS", "RequestToken": f"tok-{STATE.n}"}})
            if op == "GetResourceRequestStatus":
                return self.reply(200, {"ProgressEvent": {"OperationStatus": "SUCCESS", "RequestToken": req["RequestToken"],
                                                          "Identifier": self.ident_of(req["RequestToken"])}})
            key = (req.get("TypeName"), req.get("Identifier"))
            if key not in STATE.aws:
                return self.reply(400, {"__type": "ResourceNotFoundException", "Message": "ResourceNotFoundException"})
            if op == "GetResource":
                return self.reply(200, {"TypeName": key[0], "ResourceDescription": {"Identifier": key[1],
                                                                                    "Properties": json.dumps(STATE.aws[key])}})
            if op == "UpdateResource":
                props = STATE.aws[key]
                for p in json.loads(req["PatchDocument"]):
                    k = p["path"].lstrip("/")
                    if p["op"] == "remove":
                        props.pop(k, None)
                    else:
                        props[k] = p["value"]
                return self.reply(200, {"ProgressEvent": {"OperationStatus": "SUCCESS", "Identifier": key[1], "RequestToken": "u"}})
            if op == "DeleteResource":
                del STATE.aws[key]
                return self.reply(200, {"ProgressEvent": {"OperationStatus": "SUCCESS", "Identifier": key[1], "RequestToken": "d"}})
        return self.reply(400, {"Message": "unknown operation " + op})

    def ident_of(self, token):
        return STATE.tokens.get(token, token)

    def s3(self, h, method, u, data):
        if not check_sigv4(h, method, u.path, u.query, data):
            return self.reply(403, {"Message": "SignatureDoesNotMatch"})
        with STATE.lock:
            if method == "GET":
                if u.path not in STATE.s3:
                    return self.reply(404, {"Message": "NoSuchKey"})
                return self.reply(200, STATE.s3[u.path])
            if method == "PUT":
                if h.get("if-none-match") == "*" and u.path in STATE.s3:
                    return self.reply(412, {"Message": "PreconditionFailed"})
                STATE.s3[u.path] = data
                return self.reply(200, b"")
            if method == "DELETE":
                STATE.s3.pop(u.path, None)
                return self.reply(204)
        return self.reply(405)

    # -- azure -----------------------------------------------------------
    def azure(self, h, method, u, data):
        if h.get("authorization") != "Bearer " + AZURE_TOKEN:
            return self.reply(401, {"error": {"code": "InvalidAuthenticationToken", "message": "bad token"}})
        rid = u.path
        with STATE.lock:
            if method == "PUT":
                body = json.loads(data.decode() or "{}")
                old = STATE.azure.get(rid.lower(), {})
                res = {**body, "id": rid, "name": rid.rstrip("/").split("/")[-1]}
                props = dict(body.get("properties", {}))
                props["provisioningState"] = "Succeeded"
                if "userAssignedIdentities" in rid:
                    props.setdefault("principalId", "00000000-0000-0000-0000-" + hashlib.sha256(rid.encode()).hexdigest()[:12])
                if "containerApps" in rid:
                    ing = props.setdefault("configuration", {}).setdefault("ingress", {})
                    ing["fqdn"] = f"{res['name']}.happyhill.westeurope.azurecontainerapps.io"
                if "flexibleServers" in rid and "/databases/" not in rid:
                    props["fullyQualifiedDomainName"] = f"{res['name']}.postgres.database.azure.com"
                if "Microsoft.Cache/redis" in rid:
                    props["hostName"] = f"{res['name']}.redis.cache.windows.net"
                if "managedClusters" in rid:
                    props["fqdn"] = f"{res['name']}.hcp.westeurope.azmk8s.io"
                res["properties"] = props
                STATE.azure[rid.lower()] = res
                return self.reply(200 if old else 201, res,
                                  {"Azure-AsyncOperation": f"http://{self.headers['Host']}/subscriptions/op/{len(STATE.log)}?api-version=1"})
            if rid.startswith("/subscriptions/op/"):
                return self.reply(200, {"status": "Succeeded"})
            if method == "GET":
                res = STATE.azure.get(rid.lower())
                if res is None:
                    return self.reply(404, {"error": {"code": "ResourceNotFound", "message": "not found"}})
                return self.reply(200, res)
            if method == "DELETE":
                STATE.azure.pop(rid.lower(), None)
                return self.reply(200, b"")
        return self.reply(405)

    # -- gcp -------------------------------------------------------------
    def gcp(self, h, method, u, data):
        if h.get("authorization") != "Bearer " + GCP_TOKEN:
            return self.reply(401, {"error": {"code": 401, "message": "bad token"}})
        path = u.path
        host = f"http://{self.headers['Host']}"
        q = dict(urllib.parse.parse_qsl(u.query))
        with STATE.lock:
            if "/operations/" in path:
                return self.reply(200, {"name": path, "done": True, "status": "DONE", "kind": "compute#operation"})
            if path.endswith(":getIamPolicy") or (path.endswith("/iam") and method == "GET"):
                key = path.rsplit(":", 1)[0] if path.endswith(":getIamPolicy") else path
                return self.reply(200, STATE.iam.get(key, {"bindings": [], "etag": "x"}))
            if path.endswith(":setIamPolicy") or (path.endswith("/iam") and method == "PUT"):
                body = json.loads(data.decode() or "{}")
                key = path.rsplit(":", 1)[0] if path.endswith(":setIamPolicy") else path
                STATE.iam[key] = body.get("policy", body)
                return self.reply(200, STATE.iam[key])
            if path.endswith(":addVersion"):
                return self.reply(200, {"name": path[:-len(":addVersion")] + "/versions/1", "state": "ENABLED"})
            if method in ("POST", "PUT"):
                body = json.loads(data.decode() or "{}")
                if method == "PUT":
                    self_path = path
                else:
                    name = next((q[k] for k in q if k.endswith("Id")), None) or body.get("name") or body.get("accountId") \
                        or body.get("cluster", {}).get("name") or body.get("metadata", {}).get("name")
                    if path.endswith("/serviceAccounts"):
                        project = path.split("/projects/")[1].split("/")[0]
                        name = f"{body['accountId']}@{project}.iam.gserviceaccount.com"
                    if path.endswith("/rrsets"):
                        name = f"{body['name']}/{body['type']}"
                    self_path = f"{path}/{name}"
                res = {**(body.get("cluster") or body)}
                last = self_path.rstrip("/").split("/")[-1]
                res.setdefault("name", last)
                res["selfLink"] = host + self_path
                if "/serviceAccounts/" in self_path:
                    res["email"] = last
                if "/run/" in self_path and "/services/" in self_path:
                    res["uri"] = f"https://{last}-xyz-ew.a.run.app"
                if "/sqladmin/" in self_path and "/databases/" not in self_path:
                    res["ipAddresses"] = [{"ipAddress": "10.20.0.3", "type": "PRIVATE"}]
                if "/redis/" in self_path:
                    res["host"], res["port"] = "10.20.0.4", 6379
                if "/container/" in self_path:
                    res["endpoint"] = "34.0.0.10"
                STATE.gcp[self_path] = res
                if "/compute/" in path or "/sqladmin/" in path:
                    return self.reply(200, {"kind": "compute#operation", "status": "RUNNING",
                                            "selfLink": f"{host}/compute/v1/operations/{len(STATE.log)}"})
                if "/run/v2/" in path or "/container/" in path or "/redis/" in path:
                    return self.reply(200, {"name": f"projects/p/locations/l/operations/{len(STATE.log)}", "done": False})
                return self.reply(200, res)
            if method == "GET":
                res = STATE.gcp.get(path)
                if res is None:
                    return self.reply(404, {"error": {"code": 404, "message": "not found"}})
                return self.reply(200, res)
            if method == "PATCH":
                if path not in STATE.gcp:
                    return self.reply(404, {"error": {"code": 404, "message": "not found"}})
                STATE.gcp[path].update(json.loads(data.decode() or "{}"))
                return self.reply(200, STATE.gcp[path])
            if method == "DELETE":
                STATE.gcp.pop(path, None)
                return self.reply(200, {})
        return self.reply(405)

    # -- k8s -------------------------------------------------------------
    def k8s(self, h, method, u, data):
        if h.get("authorization") != "Bearer " + K8S_TOKEN:
            return self.reply(401, {"kind": "Status", "message": "Unauthorized"})
        with STATE.lock:
            if method == "PATCH":
                if "fieldManager=nexo" not in u.query or h.get("content-type") != "application/apply-patch+yaml":
                    return self.reply(415, {"message": "server-side apply expected"})
                obj = json.loads(data.decode())
                obj.setdefault("metadata", {})["uid"] = hashlib.sha256(u.path.encode()).hexdigest()[:8]
                if obj.get("kind") == "Service" and obj.get("spec", {}).get("type") == "LoadBalancer":
                    obj["status"] = {"loadBalancer": {"ingress": [{"ip": "203.0.113.10"}]}}
                STATE.k8s[u.path] = obj
                return self.reply(200, obj)
            if method == "GET":
                obj = STATE.k8s.get(u.path)
                if obj is None:
                    return self.reply(404, {"kind": "Status", "message": "not found"})
                return self.reply(200, obj)
            if method == "DELETE":
                STATE.k8s.pop(u.path, None)
                return self.reply(200, {"kind": "Status", "status": "Success"})
        return self.reply(405)


def start(port: int = 0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def environment(server) -> dict:
    """The variables that point both phases at the emulator."""
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return {
        "AWS_ENDPOINT_URL": url, "AWS_ACCESS_KEY_ID": AWS_KEY, "AWS_SECRET_ACCESS_KEY": AWS_SECRET,
        "AZURE_AUTHORITY_HOST": url, "NEXO_AZURE_ENDPOINT": url, "AZURE_TENANT_ID": "tenant",
        "AZURE_CLIENT_ID": "client", "AZURE_CLIENT_SECRET": "azure-test-secret", "AZURE_SUBSCRIPTION_ID": "sub-123",
        "NEXO_GCP_ENDPOINT": url, "GOOGLE_OAUTH_ACCESS_TOKEN": GCP_TOKEN, "GOOGLE_CLOUD_PROJECT": "shop-project",
        "NEXO_K8S_SERVER": url, "NEXO_K8S_TOKEN": K8S_TOKEN,
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }


if __name__ == "__main__":
    s = start(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(json.dumps(environment(s)))
    sys.stdout.flush()
    threading.Event().wait()
