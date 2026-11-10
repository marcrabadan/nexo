"""The Python comparison of sentinel.nexo: the same rules, the same
prompts and the same output on the same logs. Run it from
examples/sentinel: python3 compare/sentinel.py"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

MAX_LINE = 4096
MAX_FAILED_LOGINS = 5

ENTRY = re.compile(r"^(\S+) (\S+) (\S+) (.*)$")
ADDRESS = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")
FAILED_LOGIN = re.compile(r"(?i)login failed|authentication failure|invalid password")
SQL_INJECTION = re.compile(r"(?i)union(\s|\+|%20)+select|(\s|\+|%20)or(\s|\+|%20)+1=1|(%27|')(\s|\+|%20)*(--|;|%3b)")
PATH_TRAVERSAL = re.compile(r"(?i)\.\./|\.\.\\|%2e%2e(/|%2f)|\.\.%2f")
SENSITIVE_FILE = re.compile(r"(?i)/etc/(passwd|shadow)|/\.env|/\.git/|/\.ssh/|/wp-config\.php")
HIDDEN = re.compile("[​-‏‪-‮⁦-⁩﻿]")
CONTROL = re.compile("[\u0000-\u0008\u000b-\u001f\u007f]")


def shown(text):
    clean = CONTROL.sub("<control>", HIDDEN.sub("<hidden>", text))
    return clean[:80] + "..." if len(clean) > 80 else clean


def inside_logs(path):
    return path.startswith("logs/") and ".." not in path and "\\" not in path


def finding(rule, severity, file, line, source, evidence):
    return {"rule": rule, "severity": severity, "file": file, "line": line, "source": source, "evidence": evidence}


def check_line(file, number, line):
    if len(line) > MAX_LINE:
        return [finding("oversized_line", "low", file, number, "-", f"{len(line)} characters")]
    if line.strip() == "":
        return []
    found = []
    fields = ENTRY.search(line)
    source, message = "-", line
    if fields is None or ADDRESS.search(fields.group(2)) is None:
        found.append(finding("malformed_line", "low", file, number, source, shown(line)))
    else:
        source, message = fields.group(2), fields.group(4)
    if HIDDEN.search(line) or CONTROL.search(line):
        found.append(finding("hidden_text", "medium", file, number, source, shown(message)))
    if source == "-":
        return found
    for rule, severity, pattern in (("sql_injection", "high", SQL_INJECTION),
                                    ("path_traversal", "medium", PATH_TRAVERSAL),
                                    ("sensitive_file", "medium", SENSITIVE_FILE)):
        if pattern.search(message):
            found.append(finding(rule, severity, file, number, source, shown(message)))
    return found


def failed_login_from(line):
    if len(line) > MAX_LINE:
        return None
    fields = ENTRY.search(line)
    if fields is None or ADDRESS.search(fields.group(2)) is None or FAILED_LOGIN.search(fields.group(4)) is None:
        return None
    return fields.group(2)


def scan_file(file):
    found, failed, number = [], {}, 0
    with open(file, encoding="utf-8") as lines:
        for line in lines:
            line = line.rstrip("\r\n")
            number += 1
            found += check_line(file, number, line)
            source = failed_login_from(line)
            if source is not None:
                failed[source] = failed.get(source, 0) + 1
                if failed[source] == MAX_FAILED_LOGINS:
                    found.append(finding("brute_force", "high", file, number, source, f"{MAX_FAILED_LOGINS} failed logins"))
    return number, found


def scan(files):
    read_files, lines, skipped, findings = 0, 0, [], []
    for file in files:
        if not inside_logs(file):
            skipped.append(f"{shown(file)}: outside logs/")
            continue
        try:
            number, found = scan_file(file)
        except UnicodeDecodeError:
            skipped.append(f'{file}: Invalid data: the file "{file}" is not UTF-8')
        except FileNotFoundError:
            skipped.append(f'{file}: File "{file}" not found')
        else:
            read_files += 1
            lines += number
            findings += found
    return read_files, lines, skipped, findings


def to_block(findings, never):
    out = []
    for f in findings:
        if f["severity"] == "high" and f["source"] not in out and f["source"] not in never:
            out.append(f["source"])
    return out


def digest(findings):
    return "".join(f'{f["severity"]} {f["rule"]} from {f["source"]} at {f["file"]}:{f["line"]}\n' for f in findings)


def advice(findings):
    """The answer of the local model (Ollama's API), as ask gives it."""
    body = {"model": "llama3.2", "stream": False, "messages": [
        {"role": "system", "content": "Summarise these security findings for the engineer on call, in two sentences"},
        {"role": "user", "content": digest(findings)}]}
    request = urllib.request.Request(os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as answer:
        return json.load(answer)["message"]["content"]


def block(firewall, source):
    body = json.dumps({"address": source, "by": "sentinel"}).encode()
    request = urllib.request.Request(f"{firewall}/block", body, {"Content-Type": "application/json"})
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
    with open("audit.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(records, indent=2, ensure_ascii=False))


def yes(question):
    print(f"{question} (yes/no) ", end="", flush=True)
    return sys.stdin.readline().strip().lower() == "yes"


def main():
    with open("sentinel.json", encoding="utf-8") as f:
        settings = json.load(f)
    files, lines, skipped, findings = scan(settings["logs"])
    events = [("scan", f"{files} files, {lines} lines, {len(findings)} findings")]
    print("Nexo Sentinel")
    print(f"Scanned {files} files, {lines} lines.")
    for reason in skipped:
        print(f"Skipped {reason}")
        events.append(("skipped", reason))
    print(f"Findings: {len(findings)}")
    for f in findings:
        print(f'  [{f["severity"]}] {f["rule"]} {f["file"]}:{f["line"]} {f["source"]}: {f["evidence"]}')
    count = lambda severity: sum(1 for f in findings if f["severity"] == severity)
    print(f'High: {count("high")}, medium: {count("medium")}, low: {count("low")}')
    addresses = to_block(findings, settings["never_block"])
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
        print(f"Addresses to block: {json.dumps(addresses)}")
        if yes("Block them on the firewall?"):
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
