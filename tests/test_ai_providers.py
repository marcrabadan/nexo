"""The AI providers of D173 (mistral, azure, gemini, bedrock) in both
phases, against a local server that answers as each provider's
documentation says: an answer, a streamed answer (server-sent events, or
AWS's binary event stream for Bedrock) and an agent that uses a tool. The
server checks the requests: keys and headers, the shape of the body, the
results of the tools, and Bedrock's SigV4 signature, computed here on its
own from AWS's documentation."""
import hashlib
import hmac
import json
import struct
import threading
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phases import commands, run

KEY = "k3y"
AWS_ID, AWS_SECRET = "AKIDTEST", "aws-test-secret"
TOKEN = "aad-test-token"

PROGRAM = """ai:
    provider = "{provider}"
    model = "{model}"
{key}    url = "{base}/{provider}"

@tool "Looks up the price of a product"
fn price(product: str) -> int:
    return len(product) * 10

agent shop:
    tools = [price]
    goal = "Answer questions about prices"
    max_steps = 3

print(ask("Summarise this text", "Nexo"))
for chunk in ask("Tell me a story", stream: true):
    print(chunk, end: "|")
print()
print(shop.handle("How much is a pen?"))
"""

EXPECTED = """[{provider} {model}] Summarise this text
Once| upon| a time|
A pen costs 30.
"""


def frame(event: str, payload: dict) -> bytes:
    """One message of an AWS event stream (prelude, headers, payload, CRCs)."""
    headers = b""
    for name, value in ((":event-type", event), (":content-type", "application/json"), (":message-type", "event")):
        headers += bytes([len(name)]) + name.encode() + b"\x07" + struct.pack(">H", len(value)) + value.encode()
    body = json.dumps(payload).encode()
    total = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total, len(headers))
    message = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + body
    return message + struct.pack(">I", zlib.crc32(message))


def sigv4_ok(method: str, raw_path: str, headers: dict, body: bytes) -> bool:
    """Whether a request carries a valid SigV4 signature for bedrock."""
    auth = headers.get("authorization", "")
    if not auth.startswith("AWS4-HMAC-SHA256 "):
        return False
    parts = dict(p.strip().split("=", 1) for p in auth[len("AWS4-HMAC-SHA256 "):].split(","))
    key_id, day, region, service, _ = parts["Credential"].split("/")
    if key_id != AWS_ID or service != "bedrock":
        return False
    names = parts["SignedHeaders"].split(";")
    if "host" not in names or "x-amz-date" not in names:
        return False
    path, _, query = raw_path.partition("?")
    # services other than S3: the path as sent, each byte but the unreserved
    # ones encoded once more
    canonical_path = urllib.parse.quote(path, safe="/-_.~")
    payload = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method, canonical_path, query,
                           "".join(f"{n}:{headers[n].strip()}\n" for n in names), ";".join(names), payload])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", headers["x-amz-date"], scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + AWS_SECRET).encode()
    for part in (day, region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest(), parts["Signature"])


class Provider(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def sse(self, events: list):
        self.reply(200, "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n", "text/event-stream")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        headers = {k.lower(): v for k, v in self.headers.items()}
        u = urllib.parse.urlsplit(self.path)
        if u.path.startswith("/azure-login/"):
            form = dict(urllib.parse.parse_qsl(raw.decode()))
            ok = (u.path == "/azure-login/tenant-1/oauth2/v2.0/token" and form.get("client_secret") == "client-secret"
                  and form.get("scope") == "https://cognitiveservices.azure.com/.default"
                  and form.get("grant_type") == "client_credentials")
            return self.reply(200 if ok else 401, {"access_token": TOKEN, "token_type": "Bearer"} if ok else {})
        body = json.loads(raw)
        Provider.seen.append((self.path, headers, body))
        provider = u.path.split("/")[1]
        if provider in ("mistral", "azure"):
            return self.chat(provider, u, headers, body)
        if provider == "gemini":
            return self.gemini(u, headers, body)
        if provider == "bedrock":
            return self.bedrock(u, headers, body, raw)
        self.reply(404, {})

    # ---------------------------------------------- OpenAI's chat format
    def chat(self, provider, u, headers, body):
        if provider == "mistral":
            assert u.path == "/mistral/v1/chat/completions" and headers["authorization"] == f"Bearer {KEY}"
            assert "stream_options" not in body
            model = body["model"]
        else:
            deployment = u.path.split("/")[4]
            assert u.path == f"/azure/openai/deployments/{deployment}/chat/completions"
            assert u.query == "api-version=2024-10-21"
            assert headers.get("api-key") == KEY or headers.get("authorization") == f"Bearer {TOKEN}"
            model = deployment
        msgs = body["messages"]
        first = [m for m in msgs if m["role"] == "user"][0]["content"].split("\n")[0]
        if body.get("stream"):
            if provider == "azure":
                assert body["stream_options"] == {"include_usage": True}
            events = [{"choices": [{"delta": {"content": p}}]} for p in ("Once", " upon", " a time")]
            events.append({"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 3}})
            return self.sse(events)
        if "tools" in body:
            assert body["tools"][0]["function"]["name"] == "price"
            results = [m for m in msgs if m["role"] == "tool"]
            if not results:
                call = {"id": "c1", "type": "function",
                        "function": {"name": "price", "arguments": json.dumps({"product": "pen"})}}
                return self.reply(200, {"choices": [{"message": {"role": "assistant", "content": None,
                                                                 "tool_calls": [call]}}],
                                        "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
            assert results[0]["tool_call_id"] == "c1"
            return self.reply(200, {"choices": [{"message": {"role": "assistant",
                                                             "content": f"A pen costs {results[0]['content']}."}}]})
        return self.reply(200, {"choices": [{"message": {"content": f"[{provider} {model}] {first}"}}],
                                "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

    # ---------------------------------------------------------- Gemini
    def gemini(self, u, headers, body):
        assert headers["x-goog-api-key"] == KEY
        model, _, method = u.path.split("/models/")[1].partition(":")
        assert u.path == f"/gemini/v1beta/models/{model}:{method}"
        contents = body["contents"]
        assert all(c["role"] in ("user", "model") for c in contents)
        assert "temperature" in body["generationConfig"]
        first = contents[0]["parts"][0]["text"].split("\n")[0]
        if method == "streamGenerateContent":
            assert u.query == "alt=sse"
            events = [{"candidates": [{"content": {"role": "model", "parts": [{"text": p}]}}]}
                      for p in ("Once", " upon", " a time")]
            events[-1]["usageMetadata"] = {"promptTokenCount": 4, "candidatesTokenCount": 3}
            return self.reply(200, "".join(f"data: {json.dumps(e)}\r\n\r\n" for e in events), "text/event-stream")
        assert method == "generateContent"
        if "tools" in body:
            assert body["systemInstruction"]["parts"][0]["text"].startswith("Answer questions about prices")
            assert body["tools"][0]["functionDeclarations"][0]["name"] == "price"
            answers = [p["functionResponse"] for c in contents for p in c["parts"] if "functionResponse" in p]
            if not answers:
                part = {"functionCall": {"name": "price", "args": {"product": "pen"}}}
                return self.reply(200, {"candidates": [{"content": {"role": "model", "parts": [part]}}]})
            assert answers[0]["name"] == "price"
            text = f"A pen costs {answers[0]['response']['content']}."
            return self.reply(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]})
        text = f"[gemini {model}] {first}"
        return self.reply(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}],
                                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}})

    # --------------------------------------------------------- Bedrock
    def bedrock(self, u, headers, body, raw):
        if "authorization" in headers and headers["authorization"].startswith("Bearer "):
            assert headers["authorization"] == f"Bearer {KEY}"
        else:
            assert sigv4_ok("POST", self.path, headers, raw), "a bad signature"
        quoted, _, method = u.path[len("/bedrock/model/"):].partition("/")
        model = urllib.parse.unquote(quoted)
        assert ":" not in quoted, "the model id is sent encoded"
        msgs = body["messages"]
        assert all(isinstance(m["content"], list) for m in msgs)
        assert "temperature" in body["inferenceConfig"]
        first = msgs[0]["content"][0]["text"].split("\n")[0]
        if method == "converse-stream":
            out = frame("messageStart", {"role": "assistant"})
            for p in ("Once", " upon", " a time"):
                out += frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": p}})
            out += frame("contentBlockStop", {"contentBlockIndex": 0})
            out += frame("messageStop", {"stopReason": "end_turn"})
            out += frame("metadata", {"usage": {"inputTokens": 4, "outputTokens": 3}, "metrics": {"latencyMs": 1}})
            return self.reply(200, out, "application/vnd.amazon.eventstream")
        assert method == "converse"
        if "toolConfig" in body:
            assert body["system"][0]["text"].startswith("Answer questions about prices")
            spec = body["toolConfig"]["tools"][0]["toolSpec"]
            assert spec["name"] == "price" and spec["inputSchema"]["json"]["type"] == "object"
            results = [b["toolResult"] for m in msgs for b in m["content"] if "toolResult" in b]
            if not results:
                use = {"toolUse": {"toolUseId": "tu1", "name": "price", "input": {"product": "pen"}}}
                return self.reply(200, {"output": {"message": {"role": "assistant", "content": [use]}},
                                        "stopReason": "tool_use", "usage": {"inputTokens": 5, "outputTokens": 2}})
            assert results[0]["toolUseId"] == "tu1"
            text = f"A pen costs {results[0]['content'][0]['text']}."
            return self.reply(200, {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                                    "stopReason": "end_turn"})
        text = f"[bedrock {model}] {first}"
        return self.reply(200, {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                                "stopReason": "end_turn", "usage": {"inputTokens": 3, "outputTokens": 2}})


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


KEY_LINE = '    key = secret("NEXO_AI_KEY")\n'
CASES = {
    "mistral": ("mistral-small", KEY_LINE, {}),
    "azure": ("shop-gpt", KEY_LINE, {}),
    "azure-ad": ("shop-gpt", "", {"AZURE_TENANT_ID": "tenant-1", "AZURE_CLIENT_ID": "client-1",
                                   "AZURE_CLIENT_SECRET": "client-secret"}),
    "gemini": ("gemini-2.0-flash", KEY_LINE, {}),
    "bedrock": ("anthropic.claude-3-haiku-20240307-v1:0", KEY_LINE, {}),
    "bedrock-sigv4": ("anthropic.claude-3-haiku-20240307-v1:0", "",
                      {"AWS_ACCESS_KEY_ID": AWS_ID, "AWS_SECRET_ACCESS_KEY": AWS_SECRET, "AWS_REGION": "eu-west-1"}),
}


@pytest.mark.parametrize("case", list(CASES))
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_provider_answers_streams_and_uses_tools(phase, server, case, tmp_path):
    provider = case.split("-")[0]
    model, key, env = CASES[case]
    if provider == "azure":
        env = dict(env, AZURE_AUTHORITY_HOST=f"{server}/azure-login")
    (tmp_path / "app.nexo").write_text(PROGRAM.format(provider=provider, model=model, key=key, base=server))
    config = tmp_path / "config"
    proc = run(phase[1], ["app.nexo"], tmp_path, env=dict(env, NEXO_AI_KEY=KEY, XDG_CONFIG_HOME=str(config)))
    assert proc.stdout.decode() == EXPECTED.format(provider=provider, model=model)
    usage = (config / "nexo" / "usage.csv").read_text().splitlines()
    assert len(usage) == 4 and usage[0].endswith(f",{provider}:{model},3,2") and usage[1].endswith(",4,3")


MISSING = """ai:
    provider = "{provider}"
    model = "m"
{url}
try:
    ask("hi")
else e:
    print(e.message)
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_missing_credentials_and_address(phase, tmp_path):
    out = []
    for provider, url in (("bedrock", ""), ("azure", '    url = "https://shop.openai.azure.com"'), ("azure", ""),
                          ("gemini", "")):
        (tmp_path / "m.nexo").write_text(MISSING.format(provider=provider, url=url))
        # (without the credentials this machine may have)
        env = {"XDG_CONFIG_HOME": str(tmp_path / "c"), "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "",
               "AZURE_TENANT_ID": "", "AZURE_CLIENT_ID": "", "AZURE_CLIENT_SECRET": ""}
        out.append(run(phase[1], ["m.nexo"], tmp_path, env=env).stdout.decode())
    assert out == [
        "The bedrock key is missing, and so are AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY\n",
        "The azure key is missing, and so are AZURE_TENANT_ID and AZURE_CLIENT_ID and AZURE_CLIENT_SECRET\n",
        "azure needs the address of its resource\n",
        "The gemini key is missing\n",
    ]

