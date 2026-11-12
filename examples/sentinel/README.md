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
| SEN-204 | `object_enumeration` | high | an address asks for `max_object_ids` different numbers on one route (`/api/orders/{id}`, `?doc_id={id}`) in a file |
| SEN-205 | `foreign_object` | high | a request with `user=N` is answered 2xx for `/users/M/...` (also accounts, customers, profiles, members) and M is not N |
| SEN-301 | `hidden_text` | medium | the line has invisible or direction-changing Unicode characters, or control characters such as terminal escapes |
| SEN-302 | `oversized_line` | low | the line is longer than 4096 characters; it is not examined further |
| SEN-303 | `malformed_line` | low | the line is not a log entry, or its address is not an IPv4 address |

SEN-204 and SEN-205 are the signs of IDOR (insecure direct object
reference, CWE-639) that a log can show. They are suspicions: a log does
not say who owns an object, so an administrator who reads the object of a
user, or a client that lists many orders of its own, is reported too.
SEN-205 needs the user in the log, as `user=N` after the status.

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
| `max_object_ids` | different ids of one route asked by one address before SEN-204 |

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

## Restarts, with Prometheus and Loki

`restarts.nexo` is a second program. It asks Prometheus which containers
restarted in the last `window`, with their exit code, state and memory,
reads the last lines of each from Loki and says why each one restarted and
how serious it is. It only reads from both; it changes nothing.

```
cd examples/sentinel
../../.venv/bin/nexo restarts.nexo
../../.venv/bin/nexo test restarts.nexo
```

The report has the restarts with the most serious first, the evidence of
each, the count for each namespace and what to do about each cause. It is
also written to `restarts-report.json`, for other tools, and to
`restarts-report.md`, to paste where an incident is discussed; the
Markdown has the lines around the evidence of each container.

### What it needs

- Prometheus with kube-state-metrics: `kube_pod_container_status_restarts_total`
  is required. `..._last_terminated_reason`, `..._last_terminated_exitcode`
  and `..._waiting_reason` are used when present.
- For the memory column, cAdvisor's `container_memory_working_set_bytes`
  and `kube_pod_container_resource_limits`. The column is the highest use
  in the window as a percentage of the limit.
- Loki with the labels `namespace`, `pod` and `container` on the logs.

When a query other than the first fails, the report says what it is
without and goes on.

### Settings

`restarts.json`:

| Field | Meaning |
|---|---|
| `prometheus`, `loki` | the addresses of the two services |
| `window` | how far back to look, such as `30m`, `1h` or `2d` |
| `namespace` | one namespace, or `""` for all |
| `lines` | how many lines of each container to read from Loki (1 to 5000) |
| `tenant` | the `X-Scope-OrgID` header, or `""` |
| `auth` | `true` to send the variable `SENTINEL_AUTH` as the `Authorization` header |
| `crash_loop_restarts` | restarts in the window from which a container is critical |

With `auth`, set the whole header in the environment, for example
`export SENTINEL_AUTH="Bearer <token>"` or `"Basic <base64>"`. It is read
as a secret: it is never printed, and it is sent to both services.

The `policy` block allows the network of this machine only. For services
elsewhere, add their hosts, for example `allow net "loki.example.com"`.
A release build needs that; development mode warns and goes on.

### Causes

| Cause | Name | Given when |
|---|---|---|
| RST-101 | `out_of_memory` | Kubernetes says `OOMKilled`; a line speaks of memory running out; or exit code 137 with memory at 90% of the limit or more |
| RST-102 | `crash` | a line has a panic, a fatal error, a segmentation fault or a traceback; or exit code 139 or 134 |
| RST-103 | `storage` | a line has no space left, a quota exceeded or a read-only file system |
| RST-104 | `certificate` | a line has an expired or unverified certificate or a failed TLS handshake |
| RST-105 | `port_in_use` | a line has an address already in use |
| RST-106 | `configuration` | a line has a missing variable, file or permission, or a configuration that does not load |
| RST-107 | `dependency` | a line has a refused or reset connection, or a host not found |
| RST-108 | `stopped_by_platform` | a line has SIGTERM, a failed liveness probe or a shutdown; or exit code 137 or 143 |
| RST-109 | `start_failure` | the container waits with `ImagePullBackOff`, `CreateContainerConfigError` or alike |
| RST-900 | `unknown` | nothing above matched |

`OOMKilled` comes first, then the first cause of the table that matches
any line, then the exit code and the state. `PROOF` says where the cause
comes from: `metrics`, `logs` or `none`.

| Severity | Given when |
|---|---|
| critical | the container waits in `CrashLoopBackOff`, or restarted `crash_loop_restarts` times or more |
| high | the cause is RST-101 to RST-104 |
| low | the cause is RST-108 |
| medium | the rest |

Names that come back from Prometheus are checked against the form of a
Kubernetes name before they go into a Loki query, and the window and
namespace of the settings are checked the same way, so neither can change
a query. Lines of a log are shown as the security report shows them, with
hidden and control characters named.

Limits: it has run against stand-ins for the two services that answer
their HTTP APIs (`tests/test_sentinel_restarts.py`), not against a real
Prometheus or Loki; and it reads the container that is running now, so
the lines of a pod that was replaced (a new name) are not looked for.

## CPU and memory, with Prometheus and Loki

`resources.nexo` is a third program. It asks Prometheus for the CPU and
memory of every container in the last `window` and in the window before
it, reports the ones that grew or are near their limit, and reads from
Loki what each of those logged meanwhile. It only reads from both.

```
cd examples/sentinel
../../.venv/bin/nexo resources.nexo
../../.venv/bin/nexo test resources.nexo
```

| Rule | Name | Severity | A container is reported when |
|---|---|---|---|
| RES-101 | `memory_near_limit` | high | its memory is `near_limit_percent` of its limit or more |
| RES-102 | `memory_increase` | medium | its memory grew `increase_percent` or more, and at least 16 MiB |
| RES-201 | `cpu_near_limit` | high | its CPU is `near_limit_percent` of its limit or more |
| RES-202 | `cpu_increase` | medium | its CPU grew `increase_percent` or more, and at least 50 thousandths of a core |
| RES-203 | `cpu_throttled` | medium | it was held back in `throttled_percent` of its periods or more |

CPU is the average of the window in thousandths of a core (`950m`), memory
the average of the window in MiB. A container without a limit, or one that
was not there in the window before, is not reported for what cannot be
compared. For each container with a finding, the report gives how many of
its lines in Loki speak of errors, timeouts or memory, and the newest one.
The report is also written to `resources-report.json`.

It needs cAdvisor's `container_cpu_usage_seconds_total` (required),
`container_memory_working_set_bytes` and `container_cpu_cfs_*_periods_total`,
and kube-state-metrics' `kube_pod_container_resource_limits`. Settings are
in `resources.json`: `prometheus`, `loki`, `window`, `namespace`, `tenant`
and `auth` as for restarts, the three percentages above, and `lines`, the
most lines read for each container.

`monitoring.nexo` is the module it uses for the queries and for text that
is safe to show. Like the restarts report, it has run against stand-ins
for the two services (`tests/test_sentinel_resources.py`), not against a
real Prometheus or Loki.

Not done yet from the roadmap: the measurements against Python (startup,
throughput, memory, size) and running both under the same isolation
profile.
