"""The Python comparison of sentinel.nexo: the same rules, the same
prompts, the same output, report.json and audit records on the same
logs. Run it from examples/sentinel: python3 compare/sentinel.py"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MAX_LINE = 4096
SPACE = r"(\s|\+|%20)"

# id, name, severity, confidence, tactic, reference, pattern of the message (or ""), action
RULES = [
    ("SEN-101", "sql_injection", "high", "high", "Initial Access", "CWE-89, ATT&CK T1190", rf"(?i)union{SPACE}+select|{SPACE}or{SPACE}+1=1|(%27|'){SPACE}*(--|;|%3b)", "Review the queries of the endpoint; use parameters, never text put together."),
    ("SEN-102", "command_injection", "high", "high", "Initial Access", "CWE-78, ATT&CK T1190", rf"(?i)(;|\||%3b|%7c|&&|%26%26|\$\(|`){SPACE}*(cat|wget|curl|nc|bash|sh|id|whoami|uname)({SPACE}|$|&|;)", "Check the host for commands run by the web service; never pass input to a shell."),
    ("SEN-103", "cross_site_scripting", "medium", "high", "Initial Access", "CWE-79", rf"(?i)(<|%3c)script|javascript:|on(error|load){SPACE}*(=|%3d)", "Encode output in the page named; add a Content-Security-Policy."),
    ("SEN-104", "path_traversal", "medium", "high", "Initial Access", "CWE-22", r"(?i)\.\./|\.\.\\|%2e%2e(/|%2f)|\.\.%2f", "Resolve file names inside one folder and refuse the rest."),
    ("SEN-105", "sensitive_file", "medium", "high", "Reconnaissance", "CWE-538", r"(?i)/etc/(passwd|shadow)|/\.env|/\.git/|/\.ssh/|/wp-config\.php", "Make sure the file is not served; rotate any secret that was answered with 200."),
    ("SEN-106", "scanner", "medium", "high", "Reconnaissance", "ATT&CK T1595.002", r"(?i)sqlmap|nikto|nmap|masscan|dirbuster|gobuster|wpscan|acunetix|nuclei", "Expect more requests from this address; rate-limit it."),
    ("SEN-107", "server_side_request", "high", "high", "Credential Access", "CWE-918 (SSRF)", r"(?i)(url|uri|dest|destination|redirect|next|target|callback|webhook)=(https?(:|%3a)(/|%2f)(/|%2f))?(169\.254\.169\.254|localhost|127\.0\.0\.1|0\.0\.0\.0|metadata\.google\.internal)", "Allow only the hosts the feature needs; never fetch an address a request gives."),
    ("SEN-108", "jndi_injection", "high", "high", "Initial Access", "CWE-917, CVE-2021-44228", r"(?i)(\$|%24)(\{|%7b)jndi(:|%3a)", "Update Log4j and check the host for connections to the address named."),
    ("SEN-109", "secret_in_request", "medium", "high", "Credential Access", "CWE-598", r"""(?i)(password|passwd|pwd|token|api_?key|secret|access_key)=[^& "]+|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+""", "Rotate the secret: it is now in every log and proxy on the way; send secrets in a header or the body."),
    ("SEN-201", "brute_force", "high", "high", "Credential Access", "CWE-307, ATT&CK T1110", "", "Lock the accounts tried and require a second factor."),
    ("SEN-202", "login_after_failures", "high", "high", "Initial Access", "ATT&CK T1078", "", "Treat the account as compromised: end its sessions and reset its password."),
    ("SEN-203", "enumeration", "medium", "high", "Reconnaissance", "ATT&CK T1595.003", "", "Rate-limit requests that are answered 404."),
    ("SEN-204", "object_enumeration", "high", "low", "Collection", "CWE-639 (IDOR)", "", "Check that the endpoint answers only for objects of the user who asks; use ids that cannot be guessed."),
    ("SEN-205", "foreign_object", "high", "low", "Collection", "CWE-639 (IDOR)", "", "Confirm the user may read that object; if not, the endpoint does not check the owner."),
    ("SEN-206", "login_from_many", "high", "high", "Credential Access", "ATT&CK T1110", "", "Lock the account and require a second factor; blocking one address does not stop it."),
    ("SEN-301", "hidden_text", "medium", "high", "Defense Evasion", "CWE-117, ATT&CK T1036.002", "", "Open the line with a tool that shows every character before trusting it."),
    ("SEN-302", "oversized_line", "low", "high", "Impact", "CWE-400", "", "Limit the length of a request at the proxy."),
    ("SEN-303", "malformed_line", "low", "high", "Defense Evasion", "CWE-117", "", "Check what writes this log; a broken line can hide an entry."),
]
BY_ID = {rule[0]: rule for rule in RULES}
PATTERNS = [(rule[0], re.compile(rule[6])) for rule in RULES if rule[6]]

ENTRY = re.compile(r"^(\S+) (\S+) (\S+) (.*)$")
ADDRESS = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")
FAILED_LOGIN = re.compile(r"(?i)login failed|authentication failure|invalid password")
ACCEPTED_LOGIN = re.compile(r"(?i)login accepted|session opened")
NOT_FOUND = re.compile(r" 404( |$)")
STREAM_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
WINDOW = re.compile(r"^[0-9]{1,4}[smhd]$")
STAMP = re.compile(r"[0-9A-Za-z][0-9A-Za-z:+/. -]{0,30}")
SYSLOG_SSHD = re.compile(r"^(\S+ +[0-9]{1,2} [0-9:]{8}|[0-9]{4}-\S+) \S+ sshd(\[[0-9]+\])?: (Failed|Accepted) \S+ for (invalid user )?(\S+) from (\S+) port ")
SYSLOG_LINE = re.compile(r"^(\S+ +[0-9]{1,2} [0-9:]{8}|[0-9]{4}-\S+) \S+ [^ :]+: ")
INDEX_NAME = re.compile(r"^[A-Za-z0-9_.*,-]{1,200}$")
COMBINED = re.compile(r'''^(\S+) \S+ (\S+) \[([^\]]+)\] "([A-Z]+) (\S+)[^"]*" ([0-9]{3}) \S+( "[^"]*" ("[^"]*"))?''')
NAMED_SECRET = re.compile(r'''(?i)(password|passwd|pwd|token|api_?key|secret|access_key)=[^& "]+''')
BARE_SECRET = re.compile(r"AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
ID = "[0-9]+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
LOGIN_USER = re.compile(r" user=([^ ]+)")
OBJECT_IN_PATH = re.compile(r"^[A-Z]+ ((/[^/ ?]+)*?)/(" + ID + r")(/[^ ?]*)?(\?[^ ]*)? [0-9]{3}( |$)")
OBJECT_IN_QUERY = re.compile(r"^[A-Z]+ ([^ ?]+)\?([^ ]*&)?([a-z_]*id)=(" + ID + r")(&[^ ]*)? [0-9]{3}( |$)")
OWNED_OBJECT = re.compile(r"^[A-Z]+ [^ ?]*/(users|accounts|customers|profiles|members)/([0-9]+)[^ ]* 2[0-9]{2}( .*)? user=([0-9]+)( |$)")
HIDDEN = re.compile("[​-‏‪-‮⁦-⁩﻿]")
CONTROL = re.compile("[\u0000-\u0008\u000b-\u001f\u007f]")


def redacted(text):
    out = BARE_SECRET.sub("<redacted>", text)
    for found in [m.group(0) for m in NAMED_SECRET.finditer(out)]:
        out = out.replace(found, found.split("=")[0] + "=<redacted>")
    return out


def shown(text):
    clean = CONTROL.sub("<control>", HIDDEN.sub("<hidden>", redacted(text)))
    return clean[:100] + "..." if len(clean) > 100 else clean


def inside_logs(path):
    return path.startswith("logs/") and ".." not in path and "\\" not in path


def finding(rule, time, file, line, source, evidence):
    return {"rule": rule, "name": BY_ID[rule][1], "severity": BY_ID[rule][2], "confidence": BY_ID[rule][3],
            "tactic": BY_ID[rule][4], "time": time, "file": file, "line": line, "source": source,
            "evidence": evidence}


def stamp(text):
    found = STAMP.search(text)
    return found.group(0) if found else "-"


def fields_of(line):
    if len(line) > MAX_LINE:
        return None
    web = COMBINED.search(line)
    if web and ADDRESS.search(web.group(1)):
        message = f"{web.group(4)} {web.group(5)} {web.group(6)}"
        if web.group(8) is not None:
            message += f" {web.group(8)}"
        if web.group(2) != "-":
            message += f" user={web.group(2)}"
        return web.group(1), message, stamp(web.group(3))
    ssh = SYSLOG_SSHD.search(line)
    if ssh and ADDRESS.search(ssh.group(6)):
        did = "failed" if ssh.group(3) == "Failed" else "accepted"
        return ssh.group(6), f"sshd login {did} user={ssh.group(5)}", stamp(ssh.group(1))
    fields = ENTRY.search(line)
    if fields is None or ADDRESS.search(fields.group(2)) is None:
        return None
    return fields.group(2), fields.group(4), stamp(fields.group(1))


def check_line(file, number, line):
    if len(line) > MAX_LINE:
        return [finding("SEN-302", "-", file, number, "-", f"{len(line)} characters")]
    if line.strip() == "":
        return []
    found = []
    fields = fields_of(line)
    source, message, time = "-", line, "-"
    if fields is None:
        if SYSLOG_LINE.search(line) is None:
            found.append(finding("SEN-303", time, file, number, source, shown(line)))
    else:
        source, message, time = fields
    if HIDDEN.search(line) or CONTROL.search(line):
        found.append(finding("SEN-301", time, file, number, source, shown(message)))
    if fields is None:
        return found
    for rule, pattern in PATTERNS:
        if pattern.search(message):
            found.append(finding(rule, time, file, number, source, shown(message)))
    return found


def object_of(message):
    path = OBJECT_IN_PATH.search(message)
    if path:
        return f"{path.group(1)}/{{id}}{path.group(4) or ''}", path.group(3)
    query = OBJECT_IN_QUERY.search(message)
    if query:
        return f"{query.group(1)}?{query.group(3)}={{id}}", query.group(4)
    return None


def foreign_object(message):
    found = OWNED_OBJECT.search(message)
    if found is None or found.group(2) == found.group(4):
        return None
    return found.group(4), found.group(2)


def scan_file(file, settings, skip, given=None):
    found, failed, missing, number = [], {}, {}, 0
    objects, foreign, targets, first = {}, [], {}, ""
    for line in (given if given is not None else file_lines(file)):
        number += 1
        if number == 1:
            first = shown(line)
        if number <= skip:
            continue
        found += check_line(file, number, line)
        fields = fields_of(line)
        if fields is None:
            continue
        source, message, time = fields
        if FAILED_LOGIN.search(message):
            failed[source] = failed.get(source, 0) + 1
            if failed[source] == settings["max_failed_logins"]:
                found.append(finding("SEN-201", time, file, number, source, f"{failed[source]} failed logins"))
            user = LOGIN_USER.search(message)
            if user:
                origins = targets.setdefault(user.group(1), [])
                if source not in origins:
                    origins.append(source)
                    if len(origins) == settings["max_login_sources"]:
                        found.append(finding("SEN-206", time, file, number, "-",
                                             f"user {shown(user.group(1))} failed to log in from {len(origins)} addresses"))
        elif ACCEPTED_LOGIN.search(message) and failed.get(source, 0) >= settings["max_failed_logins"]:
            found.append(finding("SEN-202", time, file, number, source,
                                 f"accepted after {failed[source]} failed logins: {shown(message)}"))
        if NOT_FOUND.search(message):
            missing[source] = missing.get(source, 0) + 1
            if missing[source] == settings["max_not_found"]:
                found.append(finding("SEN-203", time, file, number, source, f"{missing[source]} requests answered 404"))
        asked = object_of(message)
        if asked:
            ids = objects.setdefault(f"{source} {asked[0]}", [])
            if asked[1] not in ids:
                ids.append(asked[1])
                if len(ids) == settings["max_object_ids"]:
                    found.append(finding("SEN-204", time, file, number, source,
                                         f"{len(ids)} different ids of {shown(asked[0])}"))
        other = foreign_object(message)
        if other and other not in foreign:
            foreign.append(other)
            found.append(finding("SEN-205", time, file, number, source,
                                 f"user {other[0]} was answered an object of user {other[1]}: {shown(message)}"))
    return number, found, first


def file_lines(file):
    with open(file, encoding="utf-8") as lines:
        for line in lines:
            yield line.rstrip("\r\n")


def remote_sources(settings):
    out = []
    for entry in settings["sources"]:
        if STREAM_NAME.search(entry.get("name", "")) is None:
            print(f'Left out a source without a plain name: {shown(entry.get("name", ""))}')
        else:
            out.append((f'{entry.get("kind", "source")}:{entry["name"]}', entry))
    return out


def fetch(url, body=None, headers=None, timeout=15):
    if isinstance(body, (dict, list)):
        body, headers = json.dumps(body).encode(), {**(headers or {}), "Content-Type": "application/json"}
    request = urllib.request.Request(url, body, headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return answer.read().decode("utf-8")


def authorization(entry, variable):
    return {"Authorization": os.environ[variable]} if entry.get("auth") else {}


def lines_of(entry, window, limit):
    """The lines of a source of sentinel.json, the oldest first."""
    kind, url, query = entry.get("kind", ""), entry.get("url", ""), entry.get("query", "")
    if not url or not query:
        raise ValueError("a source needs a url and a query")
    if kind == "loki":
        asked = urllib.parse.urlencode({"query": query, "since": window, "limit": limit, "direction": "backward"})
        body = json.loads(fetch(f"{url}/loki/api/v1/query_range?{asked}",
                                headers=authorization(entry, "SENTINEL_LOKI_AUTH")))
        lines = [value[1] for stream in body["data"]["result"] for value in stream["values"]]
    elif kind == "elastic":
        index = entry.get("index", "")
        if INDEX_NAME.search(index) is None:
            raise ValueError("the index of an elastic source is letters, digits and . _ - * ,")
        search = {"size": limit, "sort": [{"@timestamp": {"order": "desc"}}], "query": {"bool": {"filter": [
            {"query_string": {"query": query}}, {"range": {"@timestamp": {"gte": f"now-{window}"}}}]}}}
        body = json.loads(fetch(f"{url}/{index}/_search", search, authorization(entry, "SENTINEL_ELASTIC_AUTH")))
        field = entry.get("field", "message")
        lines = [str(hit["_source"][field]) for hit in body["hits"]["hits"] if hit["_source"].get(field) is not None]
    elif kind == "splunk":
        form = urllib.parse.urlencode({"search": f"search {query} | head {limit}", "earliest_time": f"-{window}",
                                       "latest_time": "now", "output_mode": "json"}).encode()
        text = fetch(f"{url}/services/search/jobs/export", form,
                     {**authorization(entry, "SENTINEL_SPLUNK_AUTH"),
                      "Content-Type": "application/x-www-form-urlencoded"}, 30)
        rows = [json.loads(row) for row in text.split("\n") if row.strip()]
        lines = [str(row["result"]["_raw"]) for row in rows if row.get("result") is not None]
    elif kind == "datadog":
        search = {"filter": {"query": query, "from": f"now-{window}", "to": "now"}, "sort": "-timestamp",
                  "page": {"limit": limit}}
        body = json.loads(fetch(f"{url}/api/v2/logs/events/search", search, {
            "DD-API-KEY": os.environ["SENTINEL_DATADOG_API_KEY"],
            "DD-APPLICATION-KEY": os.environ["SENTINEL_DATADOG_APP_KEY"]}))
        lines = [str(event["attributes"]["message"]) for event in body["data"]
                 if event["attributes"].get("message") is not None]
    elif kind == "graylog":
        amount, unit = int(window[:-1]), {"s": 1, "m": 60, "h": 3600, "d": 86400}[window[-1]]
        asked = urllib.parse.urlencode({"query": query, "range": amount * unit, "limit": limit,
                                        "sort": "timestamp:desc", "fields": "message"})
        body = json.loads(fetch(f"{url}/api/search/universal/relative?{asked}",
                                headers={**authorization(entry, "SENTINEL_GRAYLOG_AUTH"), "Accept": "application/json"}))
        lines = [str(found["message"]["message"]) for found in body["messages"]
                 if found["message"].get("message") is not None]
    else:
        raise ValueError("the kind of a source is loki, elastic, splunk, datadog or graylog")
    return lines[::-1]


def first_line(file):
    with open(file, encoding="utf-8") as lines:
        for line in lines:
            return shown(line.rstrip("\r\n"))
    return ""


def scan(settings, state):
    read_files, lines, new_lines, skipped, findings, following = 0, 0, 0, [], [], {}
    for file in settings["logs"]:
        if not inside_logs(file):
            skipped.append((shown(file), "outside logs/"))
            continue
        try:
            skip = 0
            before = state.get(file)
            if settings["incremental"] and before is not None and first_line(file) == before["first"]:
                skip = before["lines"]
            number, found, first = scan_file(file, settings, skip)
            if number < skip:
                skip = 0
                number, found, first = scan_file(file, settings, 0)
        except UnicodeDecodeError:
            skipped.append((file, f'Invalid data: the file "{file}" is not UTF-8'))
        except FileNotFoundError:
            skipped.append((file, f'File "{file}" not found'))
        else:
            read_files += 1
            lines += number
            new_lines += number - skip
            following[file] = {"lines": number, "first": first}
            findings += found
    for name, entry in remote_sources(settings):
        try:
            number, found, first = scan_file(name, settings, 0, lines_of(entry, settings["source_window"],
                                                                         settings["source_lines"]))
        except Exception as e:
            skipped.append((name, str(e)))
        else:
            read_files += 1
            lines += number
            new_lines += number
            findings += found
    return read_files, lines, new_lines, skipped, findings, following


def count_of(findings, severity):
    return sum(1 for f in findings if f["severity"] == severity)


def score_of(findings):
    return 10 * count_of(findings, "high") + 4 * count_of(findings, "medium") + count_of(findings, "low")


def proposal_of(own, protected):
    if any(f["severity"] == "high" and f["confidence"] == "high" for f in own):
        return "protected" if protected else "block"
    return "review" if count_of(own, "high") > 0 else "watch"


def priority_of(proposal, tactics, rules):
    if proposal in ("block", "protected"):
        return "P1" if len(tactics) > 1 or "SEN-202" in rules else "P2"
    return "P3" if proposal == "review" else "P4"


def sources(findings, never):
    rows = []
    for source in dict.fromkeys(f["source"] for f in findings if f["source"] != "-"):
        own = [f for f in findings if f["source"] == source]
        rules = list(dict.fromkeys(f["rule"] for f in own))
        tactics = list(dict.fromkeys(f["tactic"] for f in own))
        proposal = proposal_of(own, source in never)
        rows.append({"priority": priority_of(proposal, tactics, rules), "address": source, "score": score_of(own),
                     "high": count_of(own, "high"), "medium": count_of(own, "medium"), "low": count_of(own, "low"),
                     "proposal": proposal, "tactics": tactics, "rules": rules, "first": own[0]["time"],
                     "last": own[-1]["time"]})
    return sorted(rows, key=lambda row: (row["priority"], -row["score"]))  # stable: the first found comes first


def suppression_of(f, suppress):
    for rule in suppress:
        if rule["rule"] in ("*", f["rule"]) and rule["source"] in ("*", f["source"]):
            return rule
    return None


def check_suppress(suppress):
    for rule in suppress:
        if not rule.get("rule") or not rule.get("source") or not rule.get("reason", "").strip():
            raise ValueError("every suppress entry needs a rule, a source and a reason")
        if rule["rule"] == "*" and rule["source"] == "*":
            raise ValueError("a suppress entry cannot silence every rule of every source")


def to_block(rows):
    return [row["address"] for row in rows if row["proposal"] == "block"]


def digest(findings):
    return "".join(f'{f["severity"]} {f["name"]} from {f["source"]} at {f["file"]}:{f["line"]}\n' for f in findings)


def advice(findings):
    """The answer of the local model (Ollama's API), as ask gives it."""
    body = {"model": "llama3.2", "stream": False, "messages": [
        {"role": "system", "content": "Summarise these security findings for the engineer on call, in two sentences"},
        {"role": "user", "content": digest(findings)}]}
    request = urllib.request.Request(os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/api/chat",
                                     json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as answer:
        return json.load(answer)["message"]["content"]


def block(firewall, source):
    body = json.dumps({"address": source, "by": "sentinel"}).encode()
    request = urllib.request.Request(f"{firewall}/block", body, {"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=5).close()


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(value, indent=2, ensure_ascii=False))


def alert_text(rows):
    urgent = [row for row in rows if row["priority"] in ("P1", "P2")]
    if not urgent:
        return ""
    return "\n".join([f"Nexo Sentinel: {len(urgent)} addresses need attention"] + [
        f'{row["priority"]} {row["address"]} {row["proposal"]} ({", ".join(row["tactics"])}) {", ".join(row["rules"])}'
        for row in urgent])


def alerts_of(rows):
    return [{"labels": {"alertname": "SentinelAttack", "severity": "critical" if row["priority"] == "P1" else "warning",
                        "source": "nexo-sentinel", "instance": row["address"], "priority": row["priority"],
                        "proposal": row["proposal"]},
             "annotations": {"summary": f'{row["address"]}: {", ".join(row["tactics"])} ({", ".join(row["rules"])})'}}
            for row in rows if row["priority"] in ("P1", "P2")]


def post_json(url, value):
    request = urllib.request.Request(url, json.dumps(value).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=5).close()


def alert(webhook, text):
    request = urllib.request.Request(webhook, json.dumps({"text": text}).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=5).close()


def record(events):
    try:
        with open("audit.json", encoding="utf-8") as f:
            records = json.load(f)
    except FileNotFoundError:
        records = []
    for event, detail in events:
        at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        records.append({"number": len(records) + 1, "at": at, "event": event, "detail": detail})
    write_json("audit.json", records)


def yes(question):
    print(f"{question} (yes/no) ", end="", flush=True)
    return sys.stdin.readline().strip().lower() == "yes"


def print_report(files, lines, new_lines, skipped, findings, silenced, rows):
    print("NEXO SENTINEL - security report of the logs")
    print()
    print(f"Scanned {files} logs and {lines} lines ({new_lines} new); skipped {len(skipped)}.")
    print(f'Findings: {len(findings)} (high {count_of(findings, "high")}, medium {count_of(findings, "medium")}, '
          f'low {count_of(findings, "low")}); suppressed {len(silenced)}')
    print(f'Sources: {len(rows)}; to look at now (P1): {sum(1 for row in rows if row["priority"] == "P1")}')
    if skipped:
        print("\nSKIPPED")
        for file, reason in skipped:
            print(f"  {file:<24}{reason}")
    if findings:
        print("\nFINDINGS")
        print(f'  {"SEVERITY":<10}{"RULE":<9}{"NAME":<22}{"SURE":<6}{"WHERE":<23}{"SOURCE":<17}EVIDENCE')
        for f in findings:
            where = f'{f["file"]}:{f["line"]}'
            sure = "no" if f["confidence"] == "low" else "yes"
            print(f'  {f["severity"]:<10}{f["rule"]:<9}{f["name"]:<22}{sure:<6}{where:<23}{f["source"]:<17}{f["evidence"]}')
    if rows:
        print("\nSOURCES")
        print(f'  {"PRI":<5}{"ADDRESS":<17}{"SCORE":<7}{"PROPOSAL":<11}{"FIRST":<28}{"LAST":<28}TACTICS')
        for row in rows:
            print(f'  {row["priority"]:<5}{row["address"]:<17}{row["score"]:<7}{row["proposal"]:<11}{row["first"]:<28}'
                  f'{row["last"]:<28}{", ".join(row["tactics"])}')
    if silenced:
        print("\nSUPPRESSED")
        for entry in silenced:
            where = f'{entry["file"]}:{entry["line"]}'
            print(f'  {entry["rule"]:<9}{where:<23}{entry["source"]:<17}{shown(entry["reason"])}')
    if findings:
        print("\nRECOMMENDED")
        for rule in RULES:
            if any(f["rule"] == rule[0] for f in findings):
                print(f"  {rule[0]:<9}{rule[1]:<22}{rule[7]} ({rule[5]})")
    print()


def main():
    with open("sentinel.json", encoding="utf-8") as f:
        settings = json.load(f)
    check_suppress(settings["suppress"])
    if settings["sources"] and WINDOW.search(settings["source_window"]) is None:
        raise ValueError("source_window must be a number and s, m, h or d, such as 15m")
    if not 1 <= settings["source_lines"] <= 1000:
        raise ValueError("source_lines must be from 1 to 1000")
    state = {}
    if settings["incremental"]:
        try:
            with open("state.json", encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            pass
    files, lines, new_lines, skipped, found, following = scan(settings, state)
    findings, silenced = [], []
    for f in found:
        rule = suppression_of(f, settings["suppress"])
        if rule is None:
            findings.append(f)
        else:
            silenced.append({"rule": f["rule"], "file": f["file"], "line": f["line"], "source": f["source"],
                             "reason": rule["reason"]})
    rows = sources(findings, settings["never_block"])
    print_report(files, lines, new_lines, skipped, findings, silenced, rows)
    events = [("scan", f"{files} logs, {lines} lines, {new_lines} new, {len(findings)} findings, {len(silenced)} suppressed")]
    events += [("skipped", f"{file}: {reason}") for file, reason in skipped]
    events += [("suppressed", f'{e["rule"]} of {e["source"]} at {e["file"]}:{e["line"]}: {shown(e["reason"])}')
               for e in silenced]
    write_json("report.json", {"tool": "sentinel", "schema": 2, "files": files, "lines": lines, "new_lines": new_lines,
                               "skipped": [{"file": file, "reason": reason} for file, reason in skipped],
                               "findings": findings, "suppressed": silenced, "sources": rows})
    print("The report is in report.json.")
    events.append(("report", "report.json"))
    if settings["incremental"]:
        write_json("state.json", following)
    text = alert_text(rows)
    if settings["alert_webhook"] and text:
        try:
            alert(settings["alert_webhook"], text)
            print("Alert sent.")
            events.append(("alert", "sent"))
        except Exception as e:
            print(f"Alert not sent: {e}")
            events.append(("alert", "failed"))
    if settings["alertmanager"] and text:
        try:
            post_json(f'{settings["alertmanager"]}/api/v2/alerts', alerts_of(rows))
            print("Alertmanager told.")
            events.append(("alertmanager", "sent"))
        except Exception as e:
            print(f"Alertmanager not told: {e}")
            events.append(("alertmanager", "failed"))
    addresses = to_block(rows)
    if findings and yes("Ask the local model for a summary?"):
        try:
            print(f"Advice (not acted on): {advice(findings)}")
            events.append(("advice", "given"))
        except Exception as e:
            print(f"No advice: {e}")
            events.append(("advice", "failed"))
    if not addresses:
        print("Nothing to block.")
    else:
        print(f'Proposed: block {", ".join(addresses)} on the firewall.')
        if yes("Block them?"):
            events.append(("approval", "yes"))
            for source in addresses:
                try:
                    block(settings["firewall"], source)
                    print(f"Blocked {source}")
                    events.append(("blocked", source))
                except Exception as e:
                    print(f"Could not block {source}: {e}")
                    events.append(("block failed", source))
        else:
            print("Nothing was changed.")
            events.append(("approval", "no"))
    record(events)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
