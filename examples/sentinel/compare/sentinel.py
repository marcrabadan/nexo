"""The Python comparison of sentinel.nexo: the same rules, the same
prompts, the same output, report.json and audit records on the same
logs. Run it from examples/sentinel: python3 compare/sentinel.py"""
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

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
FAILED_LOGIN = re.compile(r"(?i)login failed|authentication failure|invalid password")
ACCEPTED_LOGIN = re.compile(r"(?i)login accepted|session opened")
NOT_FOUND = re.compile(r" 404( |$)")
STREAM_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
WINDOW = re.compile(r"^[0-9]{1,4}[smhd]$")
ISO_TIME = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?(Z|([+-])([0-9]{2}):?([0-9]{2}))?")
CLF_TIME = re.compile(r"^([0-9]{2})/([A-Z][a-z]{2})/([0-9]{4}):([0-9]{2}):([0-9]{2}):([0-9]{2}) ([+-])([0-9]{2})([0-9]{2})")
SYSLOG_TIME = re.compile(r"^([A-Z][a-z]{2}) +([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SLOTS = 10000000
STAMP = re.compile(r"[0-9A-Za-z][0-9A-Za-z:+/. -]{0,30}")
SYSLOG_SSHD = re.compile(r"^(\S+ +[0-9]{1,2} [0-9:]{8}|[0-9]{4}-\S+) \S+ sshd(\[[0-9]+\])?: (Failed|Accepted) \S+ for (invalid user )?(\S+) from (\S+) port ")
SYSLOG_LINE = re.compile(r"^(\S+ +[0-9]{1,2} [0-9:]{8}|[0-9]{4}-\S+) \S+ [^ :]+: ")
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,29}$")
INDEX_NAME = re.compile(r"^[A-Za-z0-9_.*,-]{1,200}$")
COMBINED = re.compile(r'''^(\S+) \S+ (\S+) \[([^\]]+)\] "([A-Z]+) (\S+)[^"]*" ([0-9]{3}) \S+( "[^"]*" ("[^"]*")( "([^"]*)")?)?''')
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


def canonical(text):
    """An address written one way, by Python's ipaddress: the oracle of addresses.nexo."""
    if "%" in text or "/" in text:
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return address.compressed


def network(text):
    if "%" in text:
        return None
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None


def in_range(address, text):
    found, net = canonical(address), network(text)
    if found is None or net is None:
        return False
    found = ipaddress.ip_address(found)
    return found.version == net.version and found in net


def in_any(address, ranges):
    return any(in_range(address, text) for text in ranges)


def client_of(remote, forwarded, trusted):
    if forwarded is None or forwarded == "-" or not in_any(remote, trusted):
        return remote
    client = remote
    for hop in reversed([hop.strip() for hop in forwarded.split(",")]):
        found = canonical(hop)
        if found is None:
            return client
        client = found
        if not in_any(found, trusted):
            return found
    return client


def fields_of(line, trusted):
    if len(line) > MAX_LINE:
        return None
    web = COMBINED.search(line)
    if web and canonical(web.group(1)):
        message = f"{web.group(4)} {web.group(5)} {web.group(6)}"
        if web.group(8) is not None:
            message += f" {web.group(8)}"
        if web.group(2) != "-":
            message += f" user={web.group(2)}"
        return client_of(canonical(web.group(1)), web.group(10), trusted), message, stamp(web.group(3))
    ssh = SYSLOG_SSHD.search(line)
    if ssh and canonical(ssh.group(6)):
        did = "failed" if ssh.group(3) == "Failed" else "accepted"
        return canonical(ssh.group(6)), f"sshd login {did} user={ssh.group(5)}", stamp(ssh.group(1))
    fields = ENTRY.search(line)
    if fields is None or canonical(fields.group(2)) is None:
        return None
    return canonical(fields.group(2)), fields.group(4), stamp(fields.group(1))


def check_line(file, number, line, trusted):
    if len(line) > MAX_LINE:
        return [finding("SEN-302", "-", file, number, "-", f"{len(line)} characters")]
    if line.strip() == "":
        return []
    found = []
    fields = fields_of(line, trusted)
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


def seconds_of(window):
    return int(window[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[window[-1]]


def days_from_civil(year, month, day):
    y = year - 1 if month <= 2 else year
    era = y // 400
    yoe = y - era * 400
    doy = (153 * ((month + 9) % 12) + 2) // 5 + day - 1
    return era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468


def seconds_at(year, month, day, hour, minute, second):
    return days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second


def month_of(name):
    return MONTHS.index(name) + 1 if name in MONTHS else 0


def epoch_of(text, today):
    """Seconds since 1970 in UTC of a time as a log shows it; -1 when unreadable."""
    iso = ISO_TIME.search(text)
    if iso:
        moment = seconds_at(*(int(iso.group(i)) for i in range(1, 7)))
        if iso.group(9):
            offset = int(iso.group(10)) * 3600 + int(iso.group(11)) * 60
            moment += -offset if iso.group(9) == "+" else offset
        return moment
    clf = CLF_TIME.search(text)
    if clf and month_of(clf.group(2)):
        moment = seconds_at(int(clf.group(3)), month_of(clf.group(2)), int(clf.group(1)), int(clf.group(4)),
                            int(clf.group(5)), int(clf.group(6)))
        offset = int(clf.group(8)) * 3600 + int(clf.group(9)) * 60
        return moment - offset if clf.group(7) == "+" else moment + offset
    sys_time = SYSLOG_TIME.search(text)
    if sys_time and month_of(sys_time.group(1)):
        year = today.year - 1 if month_of(sys_time.group(1)) > today.month else today.year
        return seconds_at(year, month_of(sys_time.group(1)), *(int(sys_time.group(i)) for i in range(2, 6)))
    return -1


def event(kind, key, value, at, time, file, line, source, detail):
    return {"kind": kind, "key": key, "value": value, "at": at, "time": time, "file": file, "line": line,
            "source": source, "detail": detail}


def scan_file(file, skip, given, trusted):
    found, events, foreign, first, number, last_at = [], [], [], "", 0, 0
    today = datetime.now().date()
    for line in (given if given is not None else file_lines(file)):
        number += 1
        if number == 1:
            first = shown(line)
        if number <= skip:
            continue
        found += check_line(file, number, line, trusted)
        fields = fields_of(line, trusted)
        if fields is None:
            continue
        source, message, time = fields
        at = epoch_of(time, today)
        if at < 0:
            at = last_at
        last_at = at
        if FAILED_LOGIN.search(message):
            events.append(event("failed", source, "", at, time, file, number, source, ""))
            user = LOGIN_USER.search(message)
            if user:
                events.append(event("failed_user", shown(user.group(1)), source, at, time, file, number, "-", ""))
        elif ACCEPTED_LOGIN.search(message):
            events.append(event("accepted", source, "", at, time, file, number, source, shown(message)))
        if NOT_FOUND.search(message):
            events.append(event("missing", source, "", at, time, file, number, source, ""))
        asked = object_of(message)
        if asked:
            events.append(event("object", f"{source} {shown(asked[0])}", asked[1], at, time, file, number, source,
                                shown(asked[0])))
        other = foreign_object(message)
        if other and other not in foreign:
            foreign.append(other)
            found.append(finding("SEN-205", time, file, number, source,
                                 f"user {other[0]} was answered an object of user {other[1]}: {shown(message)}"))
    return number, found, first, events


def unseen(lines, mark, trusted):
    """The lines a log source gives that the run before did not see, and the mark for the next run."""
    today, last, times = datetime.now().date(), 0, []
    for line in lines:
        fields = fields_of(line, trusted)
        if fields is not None and epoch_of(fields[2], today) >= 0:
            last = epoch_of(fields[2], today)
        times.append(last)
    fresh, at_mark = [], 0
    for line, t in zip(lines, times):
        if mark is None or t > mark["at"]:
            fresh.append(line)
        elif t == mark["at"]:
            at_mark += 1
            if at_mark > mark["count"]:
                fresh.append(line)
    if not times:
        return fresh, mark or {"at": -1, "count": 0}
    newest = max(times)
    return fresh, {"at": newest, "count": times.count(newest)}


def correlate(events, told, settings, window):
    """Findings of the counted rules, as (rank, finding), and when each rule was last reported for a key."""
    w, found, told, times, values = settings["count_window"], [], dict(told), {}, {}
    for e in sorted(events, key=lambda e: e["pos"]):
        at, kind, rule, detail = e["at"], e["kind"], "", ""
        if kind in ("failed", "missing"):
            name = f'{kind} {e["key"]}'
            kept = times[name] = [t for t in times.get(name, []) if t >= at - window] + [at]
            if kind == "failed" and len(kept) >= settings["max_failed_logins"]:
                rule, detail = "SEN-201", f"{len(kept)} failed logins in {w}"
            if kind == "missing" and len(kept) >= settings["max_not_found"]:
                rule, detail = "SEN-203", f"{len(kept)} requests answered 404 in {w}"
        elif kind == "accepted":
            failures = [t for t in times.get(f'failed {e["key"]}', []) if t >= at - window]
            if len(failures) >= settings["max_failed_logins"]:
                rule, detail = "SEN-202", f'accepted after {len(failures)} failed logins in {w}: {e["detail"]}'
        else:
            name = f'{kind} {e["key"]}'
            kept = {value: t for value, t in values.get(name, {}).items() if t >= at - window}
            kept[e["value"]] = at
            values[name] = kept
            if kind == "failed_user" and len(kept) >= settings["max_login_sources"]:
                rule, detail = "SEN-206", f'user {e["key"]} failed to log in from {len(kept)} addresses in {w}'
            if kind == "object" and len(kept) >= settings["max_object_ids"]:
                rule, detail = "SEN-204", f'{len(kept)} different ids of {e["detail"]} in {w}'
        if rule and e["fresh"]:
            key = f'{rule} {e["key"]}'
            if key not in told or at >= told[key] + window:
                told[key] = at
                found.append((e["rank"], finding(rule, e["time"], e["file"], e["line"], e["source"], detail)))
    return found, told


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
    elif kind == "azure":
        workspace = entry.get("workspace", "")
        if GUID.search(workspace) is None:
            raise ValueError("the workspace of an azure source is its id, such as 0f8fad5b-d9cb-469f-a165-70867728950e")
        span = f"P{window[:-1]}D" if window[-1] == "d" else f"PT{window[:-1]}{window[-1].upper()}"
        field = entry.get("field", "LogMessage")
        body = json.loads(fetch(f"{url}/v1/workspaces/{workspace}/query",
                                {"query": f"{query} | take {limit}", "timespan": span},
                                {"Authorization": os.environ["SENTINEL_AZURE_AUTH"]}, 30))
        lines = []
        for table in body["tables"]:
            names = [column["name"] for column in table["columns"]]
            if field not in names:
                raise ValueError(f"the answer of Azure has no column {field}; name it with field")
            lines += [str(row[names.index(field)]) for row in table["rows"] if row[names.index(field)] is not None]
        return lines
    elif kind == "gcp":
        project = entry.get("project", "")
        if PROJECT_ID.search(project) is None:
            raise ValueError("the project of a gcp source is its id, such as my-project-123")
        amount, unit = int(window[:-1]), {"s": 1, "m": 60, "h": 3600, "d": 86400}[window[-1]]
        since = (datetime.now(timezone.utc) - timedelta(seconds=amount * unit)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        body = json.loads(fetch(f"{url}/v2/entries:list", {
            "resourceNames": [f"projects/{project}"], "filter": f'({query}) AND timestamp>="{since}"',
            "orderBy": "timestamp desc", "pageSize": limit}, {"Authorization": os.environ["SENTINEL_GCP_AUTH"]}, 30))
        lines = []
        for found in body.get("entries", []):
            request, payload = found.get("httpRequest"), found.get("jsonPayload")
            if found.get("textPayload") is not None:
                lines.append(str(found["textPayload"]))
            elif request is not None and request.get("remoteIp") is not None:
                path = re.sub(r"^https?://[^/]+", "", str(request.get("requestUrl", "/")))
                lines.append(f'{found.get("timestamp", "-")} {request["remoteIp"]} gcp '
                             f'{request.get("requestMethod", "GET")} {path} {request.get("status", 0)}')
            elif payload is not None and payload.get("message") is not None:
                lines.append(str(payload["message"]))
    else:
        raise ValueError("the kind of a source is loki, elastic, splunk, datadog, graylog, azure or gcp")
    return lines[::-1]


def first_line(file):
    with open(file, encoding="utf-8") as lines:
        for line in lines:
            return shown(line.rstrip("\r\n"))
    return ""


def scan(settings, state):
    window = seconds_of(settings["count_window"])
    files = state.get("files", state)
    read_files, lines, new_lines, skipped, following, events, scanned = 0, 0, 0, [], {}, [], []
    for old in state.get("events", []):
        events.append({**old, "fresh": False, "rank": 0, "pos": old["at"] * SLOTS + len(events)})
    for file in settings["logs"]:
        if not inside_logs(file):
            skipped.append((shown(file), "outside logs/"))
            continue
        try:
            skip = 0
            before = files.get(file)
            if settings["incremental"] and before is not None and first_line(file) == before["first"]:
                skip = before["lines"]
            result = scan_file(file, skip, None, settings["trusted_proxies"])
            if result[0] < skip:
                skip = 0
                result = scan_file(file, 0, None, settings["trusted_proxies"])
        except UnicodeDecodeError:
            skipped.append((file, f'Invalid data: the file "{file}" is not UTF-8'))
        except FileNotFoundError:
            skipped.append((file, f'File "{file}" not found'))
        else:
            read_files += 1
            lines += result[0]
            new_lines += result[0] - skip
            following[file] = {"lines": result[0], "first": result[2]}
            scanned.append(result)
    marks = {}
    for name, entry in remote_sources(settings):
        try:
            fetched = lines_of(entry, settings["source_window"], settings["source_lines"])
            mark = state.get("remote", {}).get(name) if settings["incremental"] else None
            fresh, marks[name] = unseen(fetched, mark, settings["trusted_proxies"])
            result = scan_file(name, 0, fresh, settings["trusted_proxies"])
        except Exception as e:
            marks.pop(name, None)
            skipped.append((name, str(e)))
            kept_mark = state.get("remote", {}).get(name)
            if settings["incremental"] and kept_mark is not None:
                marks[name] = kept_mark
        else:
            read_files += 1
            lines += len(fetched)
            new_lines += result[0]
            scanned.append(result)
    ranked = []
    for position, result in enumerate(scanned, 1):
        ranked += [(position * SLOTS + f["line"], f) for f in result[1]]
        for e in result[3]:
            events.append({**e, "fresh": True, "rank": position * SLOTS + e["line"], "pos": e["at"] * SLOTS + len(events)})
    counted, told = correlate(events, state.get("reported", {}), settings, window)
    ranked += counted
    newest = max([0] + [e["at"] for e in events])
    kept = [event(e["kind"], e["key"], e["value"], e["at"], e["time"], e["file"], e["line"], e["source"], e["detail"])
            for e in events if e["at"] >= newest - window]
    told = {key: at for key, at in told.items() if at >= newest - window}
    findings = [f for _, f in sorted(ranked, key=lambda r: r[0])]
    return read_files, lines, new_lines, skipped, findings, {"files": following, "remote": marks, "events": kept,
                                                       "reported": told}


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
        proposal = proposal_of(own, in_any(source, never))
        rows.append({"priority": priority_of(proposal, tactics, rules), "address": source, "score": score_of(own),
                     "high": count_of(own, "high"), "medium": count_of(own, "medium"), "low": count_of(own, "low"),
                     "proposal": proposal, "tactics": tactics, "rules": rules, "first": own[0]["time"],
                     "last": own[-1]["time"]})
    return sorted(rows, key=lambda row: (row["priority"], -row["score"]))  # stable: the first found comes first


def suppression_of(f, suppress):
    for rule in suppress:
        if rule["rule"] in ("*", f["rule"]) and (rule["source"] in ("*", f["source"]) or in_range(f["source"], rule["source"])):
            return rule
    return None


def check_suppress(suppress):
    for rule in suppress:
        if not rule.get("rule") or not rule.get("source") or not rule.get("reason", "").strip():
            raise ValueError("every suppress entry needs a rule, a source and a reason")
        if rule["rule"] == "*" and rule["source"] == "*":
            raise ValueError("a suppress entry cannot silence every rule of every source")
        if rule["source"] not in ("*", "-") and network(rule["source"]) is None:
            raise ValueError("the source of a suppress entry is *, - or an address or range, such as 10.0.0.0/8")
        if rule["rule"] == "*" and rule["source"].endswith("/0"):
            raise ValueError("a suppress entry cannot silence every rule of every source")


def check_ranges(settings):
    for text in settings["never_block"] + settings["trusted_proxies"]:
        if network(text) is None:
            raise ValueError("never_block and trusted_proxies hold addresses or ranges, such as 10.0.0.0/8, "
                             f"not {shown(text)}")


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


def block(firewall, source, ttl):
    post_json(f"{firewall}/block", {"address": source, "by": "sentinel", "ttl": ttl})


def lift(firewall, source):
    post_json(f"{firewall}/unblock", {"address": source, "by": "sentinel"})


def read_blocks():
    try:
        with open("blocks.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def expire(firewall, now_seconds):
    in_force, left, events = read_blocks(), {}, []
    for address, entry in in_force.items():
        if entry["until"] <= now_seconds:
            try:
                lift(firewall, address)
                print(f"Unblocked {address}: its block ended")
                events.append(("unblocked", f"{address}: ended"))
            except Exception as e:
                left[address] = entry
                print(f"Could not unblock {address}: {shown(str(e))}")
                events.append(("unblock failed", address))
        else:
            left[address] = entry
    if len(left) != len(in_force):
        write_json("blocks.json", left)
    return events


def block_and_record(settings, row, ttl, now_seconds):
    """Blocks and records; a block that cannot be recorded is lifted, as undo() does in Nexo."""
    source = row["address"]
    try:
        block(settings["firewall"], source, ttl)
    except Exception as e:
        print(f"Could not block {source}: {e}")
        return "block failed", source
    try:
        in_force = read_blocks()
        until = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        in_force[source] = {"until": now_seconds + ttl,
                            "until_text": until.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                            "rules": row["rules"]}
        write_json("blocks.json", in_force)
        print(f'Blocked {source} for {settings["block_ttl"]}')
        return "blocked", source
    except Exception as e:
        try:
            lift(settings["firewall"], source)
            print(f"Could not record the block of {source}, so it was lifted: {shown(str(e))}")
            return "block undone", source
        except Exception as lifted:
            print(f"Could not record nor lift the block of {source}: {shown(str(lifted))}")
            return "block not recorded", source


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
    check_ranges(settings)
    if WINDOW.search(settings["count_window"]) is None:
        raise ValueError("count_window must be a number and s, m, h or d, such as 10m")
    if settings["sources"] and WINDOW.search(settings["source_window"]) is None:
        raise ValueError("source_window must be a number and s, m, h or d, such as 15m")
    if not 1 <= settings["source_lines"] <= 1000:
        raise ValueError("source_lines must be from 1 to 1000")
    if WINDOW.search(settings["block_ttl"]) is None:
        raise ValueError("block_ttl must be a number and s, m, h or d, such as 24h")
    ttl = seconds_of(settings["block_ttl"])
    now_seconds = int(datetime.now(timezone.utc).timestamp())
    ended = expire(settings["firewall"], now_seconds)
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
    rows = sources(findings, settings["never_block"] + settings["trusted_proxies"])
    print_report(files, lines, new_lines, skipped, findings, silenced, rows)
    events = ended + [("scan", f"{files} logs, {lines} lines, {new_lines} new, {len(findings)} findings, {len(silenced)} suppressed")]
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
    in_force = read_blocks()
    addresses = []
    for source in to_block(rows):
        if source in in_force:
            print(f"Already blocked: {source}")
        else:
            addresses.append(source)
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
            for row in rows:
                if row["address"] in addresses:
                    events.append(block_and_record(settings, row, ttl, now_seconds))
        else:
            print("Nothing was changed.")
            events.append(("approval", "no"))
    record(events)


LOCK_STALE = 3600


def take_lock(name, token, now_seconds):
    """The lock of runlock.nexo: the same table of sentinel.lock.db, so the two exclude each other."""
    db = sqlite3.connect("sentinel.lock.db", timeout=5, isolation_level=None)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS "runlock" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, '
                   '"name" TEXT NOT NULL UNIQUE, "holder" TEXT NOT NULL, "since" INTEGER NOT NULL)')
        db.execute("BEGIN IMMEDIATE")
        held = db.execute('SELECT "id", "since" FROM "runlock" WHERE "name" = ?', (name,)).fetchone()
        taken = held is None or held[1] < now_seconds - LOCK_STALE
        if held is None:
            db.execute('INSERT INTO "runlock" ("name", "holder", "since") VALUES (?, ?, ?)', (name, token, now_seconds))
        elif taken:
            db.execute('UPDATE "runlock" SET "holder" = ?, "since" = ? WHERE "id" = ?', (token, now_seconds, held[0]))
        db.execute("COMMIT")
        return taken
    finally:
        db.close()


def release_lock(name, token):
    db = sqlite3.connect("sentinel.lock.db", timeout=5, isolation_level=None)
    try:
        db.execute('DELETE FROM "runlock" WHERE "name" = ? AND "holder" = ?', (name, token))
    finally:
        db.close()


def locked(name):
    token = secrets.token_urlsafe(18)
    if not take_lock(name, token, int(datetime.now(timezone.utc).timestamp())):
        print("Sentinel stopped: another run of Sentinel is under way; a lock left by a run that stopped "
              "ends an hour after it was taken")
        return
    try:
        main()
    finally:
        release_lock(name, token)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    locked("sentinel")
