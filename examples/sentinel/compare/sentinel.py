"""The Python comparison of sentinel.nexo: the same rules, the same
prompts, the same output, report.json and audit records on the same
logs. Run it from examples/sentinel: python3 compare/sentinel.py"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

MAX_LINE = 4096
SPACE = r"(\s|\+|%20)"

# id, name, severity, reference, pattern of the message (or ""), action
RULES = [
    ("SEN-101", "sql_injection", "high", "CWE-89, ATT&CK T1190", rf"(?i)union{SPACE}+select|{SPACE}or{SPACE}+1=1|(%27|'){SPACE}*(--|;|%3b)", "Review the queries of the endpoint; use parameters, never text put together."),
    ("SEN-102", "command_injection", "high", "CWE-78, ATT&CK T1190", rf"(?i)(;|\||%3b|%7c|&&|%26%26|\$\(|`){SPACE}*(cat|wget|curl|nc|bash|sh|id|whoami|uname)({SPACE}|$|&|;)", "Check the host for commands run by the web service; never pass input to a shell."),
    ("SEN-103", "cross_site_scripting", "medium", "CWE-79", rf"(?i)(<|%3c)script|javascript:|on(error|load){SPACE}*(=|%3d)", "Encode output in the page named; add a Content-Security-Policy."),
    ("SEN-104", "path_traversal", "medium", "CWE-22", r"(?i)\.\./|\.\.\\|%2e%2e(/|%2f)|\.\.%2f", "Resolve file names inside one folder and refuse the rest."),
    ("SEN-105", "sensitive_file", "medium", "CWE-538", r"(?i)/etc/(passwd|shadow)|/\.env|/\.git/|/\.ssh/|/wp-config\.php", "Make sure the file is not served; rotate any secret that was answered with 200."),
    ("SEN-106", "scanner", "medium", "ATT&CK T1595.002", r"(?i)sqlmap|nikto|nmap|masscan|dirbuster|gobuster|wpscan|acunetix|nuclei", "Expect more requests from this address; rate-limit it."),
    ("SEN-107", "server_side_request", "high", "CWE-918 (SSRF)", r"(?i)(url|uri|dest|destination|redirect|next|target|callback|webhook)=(https?(:|%3a)(/|%2f)(/|%2f))?(169\.254\.169\.254|localhost|127\.0\.0\.1|0\.0\.0\.0|metadata\.google\.internal)", "Allow only the hosts the feature needs; never fetch an address a request gives."),
    ("SEN-108", "jndi_injection", "high", "CWE-917, CVE-2021-44228", r"(?i)(\$|%24)(\{|%7b)jndi(:|%3a)", "Update Log4j and check the host for connections to the address named."),
    ("SEN-109", "secret_in_request", "medium", "CWE-598", r"""(?i)(password|passwd|pwd|token|api_?key|secret|access_key)=[^& "]+|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+""", "Rotate the secret: it is now in every log and proxy on the way; send secrets in a header or the body."),
    ("SEN-201", "brute_force", "high", "CWE-307, ATT&CK T1110", "", "Lock the accounts tried and require a second factor."),
    ("SEN-202", "login_after_failures", "high", "ATT&CK T1078", "", "Treat the account as compromised: end its sessions and reset its password."),
    ("SEN-203", "enumeration", "medium", "ATT&CK T1595.003", "", "Rate-limit requests that are answered 404."),
    ("SEN-204", "object_enumeration", "high", "CWE-639 (IDOR)", "", "Check that the endpoint answers only for objects of the user who asks; use ids that cannot be guessed."),
    ("SEN-205", "foreign_object", "high", "CWE-639 (IDOR)", "", "Confirm the user may read that object; if not, the endpoint does not check the owner."),
    ("SEN-301", "hidden_text", "medium", "CWE-117, ATT&CK T1036.002", "", "Open the line with a tool that shows every character before trusting it."),
    ("SEN-302", "oversized_line", "low", "CWE-400", "", "Limit the length of a request at the proxy."),
    ("SEN-303", "malformed_line", "low", "CWE-117", "", "Check what writes this log; a broken line can hide an entry."),
]
BY_ID = {rule[0]: rule for rule in RULES}
PATTERNS = [(rule[0], re.compile(rule[4])) for rule in RULES if rule[4]]

ENTRY = re.compile(r"^(\S+) (\S+) (\S+) (.*)$")
ADDRESS = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")
FAILED_LOGIN = re.compile(r"(?i)login failed|authentication failure|invalid password")
ACCEPTED_LOGIN = re.compile(r"(?i)login accepted|session opened")
NOT_FOUND = re.compile(r" 404( |$)")
COMBINED = re.compile(r'''^(\S+) \S+ (\S+) \[[^\]]+\] "([A-Z]+) (\S+)[^"]*" ([0-9]{3}) \S+( "[^"]*" ("[^"]*"))?''')
NAMED_SECRET = re.compile(r'''(?i)(password|passwd|pwd|token|api_?key|secret|access_key)=[^& "]+''')
BARE_SECRET = re.compile(r"AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
OBJECT_IN_PATH = re.compile(r"^[A-Z]+ ((/[^/ ?]+)*?)/([0-9]+)(/[^ ?]*)?(\?[^ ]*)? [0-9]{3}( |$)")
OBJECT_IN_QUERY = re.compile(r"^[A-Z]+ ([^ ?]+)\?([^ ]*&)?([a-z_]*id)=([0-9]+)(&[^ ]*)? [0-9]{3}( |$)")
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
    return clean[:80] + "..." if len(clean) > 80 else clean


def inside_logs(path):
    return path.startswith("logs/") and ".." not in path and "\\" not in path


def finding(rule, file, line, source, evidence):
    return {"rule": rule, "name": BY_ID[rule][1], "severity": BY_ID[rule][2], "file": file, "line": line,
            "source": source, "evidence": evidence}


def fields_of(line):
    if len(line) > MAX_LINE:
        return None
    web = COMBINED.search(line)
    if web and ADDRESS.search(web.group(1)):
        message = f"{web.group(3)} {web.group(4)} {web.group(5)}"
        if web.group(7) is not None:
            message += f" {web.group(7)}"
        if web.group(2) != "-":
            message += f" user={web.group(2)}"
        return web.group(1), message
    fields = ENTRY.search(line)
    if fields is None or ADDRESS.search(fields.group(2)) is None:
        return None
    return fields.group(2), fields.group(4)


def check_line(file, number, line):
    if len(line) > MAX_LINE:
        return [finding("SEN-302", file, number, "-", f"{len(line)} characters")]
    if line.strip() == "":
        return []
    found = []
    fields = fields_of(line)
    source, message = "-", line
    if fields is None:
        found.append(finding("SEN-303", file, number, source, shown(line)))
    else:
        source, message = fields
    if HIDDEN.search(line) or CONTROL.search(line):
        found.append(finding("SEN-301", file, number, source, shown(message)))
    if fields is None:
        return found
    for rule, pattern in PATTERNS:
        if pattern.search(message):
            found.append(finding(rule, file, number, source, shown(message)))
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


def scan_file(file, settings):
    found, failed, missing, number = [], {}, {}, 0
    objects, foreign = {}, []
    with open(file, encoding="utf-8") as lines:
        for line in lines:
            line = line.rstrip("\r\n")
            number += 1
            found += check_line(file, number, line)
            fields = fields_of(line)
            if fields is None:
                continue
            source, message = fields
            if FAILED_LOGIN.search(message):
                failed[source] = failed.get(source, 0) + 1
                if failed[source] == settings["max_failed_logins"]:
                    found.append(finding("SEN-201", file, number, source, f"{failed[source]} failed logins"))
            elif ACCEPTED_LOGIN.search(message) and failed.get(source, 0) >= settings["max_failed_logins"]:
                found.append(finding("SEN-202", file, number, source,
                                     f"accepted after {failed[source]} failed logins: {shown(message)}"))
            if NOT_FOUND.search(message):
                missing[source] = missing.get(source, 0) + 1
                if missing[source] == settings["max_not_found"]:
                    found.append(finding("SEN-203", file, number, source, f"{missing[source]} requests answered 404"))
            asked = object_of(message)
            if asked:
                ids = objects.setdefault(f"{source} {asked[0]}", [])
                if asked[1] not in ids:
                    ids.append(asked[1])
                    if len(ids) == settings["max_object_ids"]:
                        found.append(finding("SEN-204", file, number, source,
                                             f"{len(ids)} different ids of {shown(asked[0])}"))
            other = foreign_object(message)
            if other and other not in foreign:
                foreign.append(other)
                found.append(finding("SEN-205", file, number, source,
                                     f"user {other[0]} was answered an object of user {other[1]}: {shown(message)}"))
    return number, found


def scan(settings):
    read_files, lines, skipped, findings = 0, 0, [], []
    for file in settings["logs"]:
        if not inside_logs(file):
            skipped.append((shown(file), "outside logs/"))
            continue
        try:
            number, found = scan_file(file, settings)
        except UnicodeDecodeError:
            skipped.append((file, f'Invalid data: the file "{file}" is not UTF-8'))
        except FileNotFoundError:
            skipped.append((file, f'File "{file}" not found'))
        else:
            read_files += 1
            lines += number
            findings += found
    return read_files, lines, skipped, findings


def count_of(findings, severity):
    return sum(1 for f in findings if f["severity"] == severity)


def score_of(findings):
    return 10 * count_of(findings, "high") + 4 * count_of(findings, "medium") + count_of(findings, "low")


def sources(findings, never):
    rows = []
    for source in dict.fromkeys(f["source"] for f in findings if f["source"] != "-"):
        own = [f for f in findings if f["source"] == source]
        proposal = "watch"
        if count_of(own, "high") > 0:
            proposal = "protected" if source in never else "block"
        rows.append({"address": source, "score": score_of(own), "high": count_of(own, "high"),
                     "medium": count_of(own, "medium"), "low": count_of(own, "low"),
                     "rules": list(dict.fromkeys(f["rule"] for f in own)), "proposal": proposal})
    return sorted(rows, key=lambda row: -row["score"])  # stable: the first found comes first


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


def print_report(files, lines, skipped, findings, rows):
    print("NEXO SENTINEL - security report of the logs")
    print()
    print(f"Scanned {files} files and {lines} lines; skipped {len(skipped)} files.")
    print(f'Findings: {len(findings)} (high {count_of(findings, "high")}, medium {count_of(findings, "medium")}, '
          f'low {count_of(findings, "low")})')
    if skipped:
        print("\nSKIPPED")
        for file, reason in skipped:
            print(f"  {file:<24}{reason}")
    if findings:
        print("\nFINDINGS")
        print(f'  {"SEVERITY":<10}{"RULE":<9}{"NAME":<22}{"WHERE":<23}{"SOURCE":<17}EVIDENCE')
        for f in findings:
            where = f'{f["file"]}:{f["line"]}'
            print(f'  {f["severity"]:<10}{f["rule"]:<9}{f["name"]:<22}{where:<23}{f["source"]:<17}{f["evidence"]}')
    if rows:
        print("\nSOURCES")
        print(f'  {"ADDRESS":<17}{"SCORE":<7}{"HIGH":<6}{"MEDIUM":<8}{"LOW":<5}{"PROPOSAL":<11}RULES')
        for row in rows:
            print(f'  {row["address"]:<17}{row["score"]:<7}{row["high"]:<6}{row["medium"]:<8}{row["low"]:<5}'
                  f'{row["proposal"]:<11}{", ".join(row["rules"])}')
    if findings:
        print("\nRECOMMENDED")
        for rule in RULES:
            if any(f["rule"] == rule[0] for f in findings):
                print(f"  {rule[0]:<9}{rule[1]:<22}{rule[5]} ({rule[3]})")
    print()


def main():
    with open("sentinel.json", encoding="utf-8") as f:
        settings = json.load(f)
    files, lines, skipped, findings = scan(settings)
    rows = sources(findings, settings["never_block"])
    print_report(files, lines, skipped, findings, rows)
    events = [("scan", f"{files} files, {lines} lines, {len(findings)} findings")]
    events += [("skipped", f"{file}: {reason}") for file, reason in skipped]
    write_json("report.json", {"files": files, "lines": lines,
                               "skipped": [{"file": file, "reason": reason} for file, reason in skipped],
                               "findings": findings, "sources": rows})
    print("The report is in report.json.")
    events.append(("report", "report.json"))
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
