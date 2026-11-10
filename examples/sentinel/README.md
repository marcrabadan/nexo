# Nexo Sentinel

A defensive log analyzer written in Nexo: phase 5 of
[the AI-native roadmap](../../docs/ai-native-roadmap.md). It reads the logs
named in `sentinel.json`, prints what its rules find and changes something
outside (an address blocked on the firewall) only after a person answers
`yes`.

```
cd examples/sentinel
../../.venv/bin/nexo sentinel.nexo          # the interpreter
../../compiler/target/debug/nexo sentinel.nexo   # compiled
../../.venv/bin/nexo test sentinel.nexo     # its tests, with a fake model
python3 compare/sentinel.py                 # the Python comparison
```

It prints a report in four parts: the files it skipped and why, the
findings, the addresses with their score and what Sentinel proposes for
each (`block`, `watch`, or `protected` for an address in `never_block`),
and what to do about each rule that fired, with its CWE or MITRE ATT&CK
reference. The same report is written to `report.json` for other tools.
Then it asks two questions. Anything but `yes` (an empty answer too) is a no.

1. `Ask the local model for a summary?` sends the rule, severity, address
   and place of each finding to Ollama on this machine, and prints the
   answer. The text of a log is never sent, and the answer is never read
   by the program: it cannot block an address or answer the next question.
2. `Block them?` sends `{"address": ..., "by": "sentinel"}` to
   `firewall + "/block"` for each address proposed for blocking: the ones
   with a high finding that are not in `never_block`.

## What it detects

A log line is `time address service message`. The rules are fixed: regular
expressions and counts, in the `RULES` table of the program. No model
takes part.

| Rule | Name | Severity | A line is reported when |
|---|---|---|---|
| SEN-101 | `sql_injection` | high | the message has `union select`, `or 1=1` or a quote followed by `--` or `;` (also URL-encoded) |
| SEN-102 | `command_injection` | high | a `;`, a pipe, `&&`, `$(` or a backquote is followed by a command such as `cat`, `wget`, `curl` or `sh` |
| SEN-103 | `cross_site_scripting` | medium | the message has `<script`, `javascript:` or an `onerror=`/`onload=` attribute |
| SEN-104 | `path_traversal` | medium | the message has `../` (also URL-encoded) |
| SEN-105 | `sensitive_file` | medium | the message asks for `/etc/passwd`, `/.env`, `/.git/`, `/.ssh/` or `wp-config.php` |
| SEN-106 | `scanner` | medium | the message names a scanning tool (sqlmap, nikto, nmap and others) |
| SEN-201 | `brute_force` | high | an address reaches `max_failed_logins` failed logins in a file |
| SEN-202 | `login_after_failures` | high | a login is accepted from an address that reached that limit |
| SEN-203 | `enumeration` | medium | an address reaches `max_not_found` requests answered 404 in a file |
| SEN-301 | `hidden_text` | medium | the line has invisible or direction-changing Unicode characters, or control characters such as terminal escapes |
| SEN-302 | `oversized_line` | low | the line is longer than 4096 characters; it is not examined further |
| SEN-303 | `malformed_line` | low | the line is not a log entry, or its address is not an IPv4 address |

The score of an address is 10 for each high finding, 4 for each medium one
and 1 for each low one; the highest score comes first.

Evidence is cut at 80 characters and hidden or control characters are
printed as `<hidden>` and `<control>`, so a log cannot rewrite the terminal
of whoever reads the report.

## Settings

`sentinel.json` is checked against the `Settings` type before anything is
read; a missing or wrong field stops the program.

| Field | Meaning |
|---|---|
| `logs` | the files to scan, all under `logs/` |
| `never_block` | addresses that are reported and never proposed for blocking |
| `firewall` | the address of the firewall's API |
| `max_failed_logins` | failed logins of one address before SEN-201 |
| `max_not_found` | 404 answers to one address before SEN-203 |

## What it may touch

The `policy` block allows reading `sentinel.json`, `logs/` and
`audit.json`, writing `audit.json` and `report.json`, and the network of this machine only;
shell commands are denied. The program also refuses any log whose name is
not under `logs/`. In development mode the policy warns; a release build
(`nexo build --release`) stops, so a link inside `logs/` that points
elsewhere cannot be read. The policy is checked by the standard library;
it is not isolation by the operating system.

Each run adds its records to `audit.json`: the scan, every file skipped
and why, the report written, whether advice was given, the answer to the approval question and
every address blocked or not.

## Files

- `logs/`: the fixed corpus. `benign.log` (no findings, with look-alikes
  such as `/search?q=union+station+hotel` and `/docs/javascript-guide.html`), `attack.log`, `unicode.log`,
  `oversized.log` and `binary.log` (not UTF-8, skipped).
- `findings.json`: the findings the corpus must give, each `[file, line, rule]`.
- `sentinel.expected`: the output when both questions are answered `no`.
- `compare/sentinel.py`: the same program in Python, with the same output
  and audit records.

`tests/test_sentinel.py` checks, in the interpreter and the compiler:
the findings equal `findings.json` without any call to a model; a refusal
sends nothing to the firewall; an answer of the model that says "yes" blocks
nothing; a failing firewall is recorded; wrong settings stop the program;
a release build cannot read outside `logs/`; and the Python comparison
prints the same and writes the same `report.json`.

Not done yet from the roadmap: the measurements against Python (startup,
throughput, memory, size) and running both under the same isolation
profile.
