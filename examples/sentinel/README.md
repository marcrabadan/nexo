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

How it is built, what each part may touch and why is in
[ARCHITECTURE.md](ARCHITECTURE.md).

It prints a report in five parts: the files it skipped and why; the
findings, each with whether it is sure or a suspicion; the addresses, the
most urgent first, with their priority (P1 to P4), score, the times of
their first and last finding, their ATT&CK tactics and what Sentinel
proposes for each (`block`, `review`, `watch`, or `protected` for an
address in `never_block`); the findings that `sentinel.json` suppresses;
and what to do about each rule that fired, with its CWE or MITRE ATT&CK
reference. The same report is written to `report.json` for other tools.
Then it asks two questions. Anything but `yes` (an empty answer too) is a no.

1. `Ask the local model for a summary?` sends the rule, severity, address
   and place of each finding to Ollama on this machine, and prints the
   answer. The text of a log is never sent, and the answer is never read
   by the program: it cannot block an address or answer the next question.
2. `Block them?` sends `{"address": ..., "by": "sentinel"}` to
   `firewall + "/block"` for each address proposed for blocking: the ones
   with a high finding that is sure and that are not in `never_block`.

## What it detects

A log line is `time address service message`, or a line of nginx or Apache
in the combined format (`address - user [time] "GET /path HTTP/1.1" status
size "referer" "agent"`), which is read as the same message. The rules are fixed: regular
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
| SEN-107 | `server_side_request` | high | a `url=`, `redirect=`, `callback=` or alike points at this machine or at a cloud's metadata address (SSRF) |
| SEN-108 | `jndi_injection` | high | the message has `${jndi:` (Log4Shell), also URL-encoded |
| SEN-109 | `secret_in_request` | medium | the request carries `password=`, `token=`, `api_key=` or alike, an AWS access key or a signed token (JWT) |
| SEN-201 | `brute_force` | high | an address reaches `max_failed_logins` failed logins within `count_window` |
| SEN-202 | `login_after_failures` | high | a login is accepted from an address that reached that limit within `count_window` before it |
| SEN-203 | `enumeration` | medium | an address reaches `max_not_found` requests answered 404 within `count_window` |
| SEN-204 | `object_enumeration` | high | an address asks for `max_object_ids` different numbers or UUIDs on one route (`/api/orders/{id}`, `?doc_id={id}`) within `count_window` |
| SEN-205 | `foreign_object` | high | a request with `user=N` is answered 2xx for `/users/M/...` (also accounts, customers, profiles, members) and M is not N |
| SEN-206 | `login_from_many` | high | logins of one user fail from `max_login_sources` different addresses within `count_window`; no single address is to blame, so none is proposed for blocking |
| SEN-301 | `hidden_text` | medium | the line has invisible or direction-changing Unicode characters, or control characters such as terminal escapes |
| SEN-302 | `oversized_line` | low | the line is longer than 4096 characters; it is not examined further |
| SEN-303 | `malformed_line` | low | the line is not a log entry, or its address is neither IPv4 nor IPv6 |

SEN-204 and SEN-205 are the signs of IDOR (insecure direct object
reference, CWE-639) that a log can show. They are suspicions, so their
address is proposed for `review` and never for a block: a log does
not say who owns an object, so an administrator who reads the object of a
user, or a client that lists many orders of its own, is reported too.
SEN-205 needs the user in the log, as `user=N` after the status.

The score of an address is 10 for each high finding, 4 for each medium one
and 1 for each low one. Its priority is P1 when it is to block and its
findings are in more than one tactic or a login was accepted after
failures, P2 for the other addresses to block or protected, P3 for the
ones to review and P4 for the ones to watch; the most urgent comes first
and, among equals, the highest score.

The value of a secret is replaced by `<redacted>` wherever a line is
shown, on screen and in `report.json`. Evidence is cut at 100 characters
and hidden or control characters are printed as `<hidden>` and `<control>`, so a log cannot rewrite the terminal
of whoever reads the report.

## Settings

`sentinel.json` is checked against the `Settings` type before anything is
read; a missing or wrong field stops the program.

| Field | Meaning |
|---|---|
| `logs` | the files to scan, all under `logs/` |
| `never_block` | addresses and ranges (`192.0.2.10`, `10.0.0.0/8`, `2001:db8::/32`) that are reported and never proposed for blocking |
| `trusted_proxies` | the load balancers and proxies in front of the servers, as addresses and ranges; see below |
| `firewall` | the address of the firewall's API |
| `max_failed_logins` | failed logins of one address before SEN-201 |
| `max_not_found` | 404 answers to one address before SEN-203 |
| `max_object_ids` | different ids of one route asked by one address before SEN-204 |
| `max_login_sources` | addresses from which the logins of one user fail before SEN-206 |
| `count_window` | the window the five counted rules count within, such as `10m` or `1h` |
| `incremental` | `true` to report only the lines added since the run before |
| `alert_webhook` | an address to send the urgent addresses to, or `""` for none |
| `alertmanager` | the address of Prometheus Alertmanager, or `""` for none |
| `sources` | logs to read from other systems besides the files (see Log sources) |
| `source_window`, `source_lines` | how far back each source is read, such as `15m`, and the most lines of each (1 to 1000) |
| `suppress` | findings to silence: each `{"rule", "source", "reason"}`, where `"*"` is any rule or any address (not both) |

A suppressed finding is not scored and proposes nothing; it is listed
under `SUPPRESSED`, written to `report.json` and recorded in `audit.json`
with its reason.

### Running it again and again

With `incremental`, each run writes to `state.json` how many lines of each
file it read and the first line of each. The next run reads a file from
where it was left when it begins the same, and whole when it does not or
is shorter (it was rotated). It also keeps the events of the last
`count_window` (failed and accepted logins, 404s, objects asked for) and
when each counted rule was last reported, so the counts go on from one
run to the next: 3 failed logins in one run and 2 in the next are 5.

### Load balancers, IPv6 and ranges

Behind a load balancer or a proxy (an ALB, Cloudflare, the ingress of
Kubernetes), the address in the log is the proxy's, not the client's.
Blocking it would cut everyone off. List the proxies in
`trusted_proxies`, and have nginx write X-Forwarded-For as the last field,
as its `main` format does:

```
log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                '$status $body_bytes_sent "$http_referer" '
                '"$http_user_agent" "$http_x_forwarded_for"';
```

When the address that connected is a trusted proxy, the source of the
line is read from X-Forwarded-For, from the right, skipping the trusted
proxies. When it is not, the header is ignored: anyone can send one,
and believing it would let an attacker blame, or have Sentinel block,
another address. A trusted proxy is never proposed for blocking; if a
line from one has no client in it, the finding is the proxy's and is
`protected`.

Addresses are IPv4 or IPv6. IPv6 is written one way (RFC 5952:
`2001:DB8:0:0:0:0:0:7` is `2001:db8::7`), so the same address is always
counted as one, and an IPv4 address inside IPv6 (`::ffff:203.0.113.9`)
is the IPv4 address. `never_block`, `trusted_proxies` and the `source` of
a `suppress` entry take ranges; a range that is not valid stops the run.
`addresses.nexo` is the module with all this.

### Counting in a window of time

SEN-201 to SEN-204 and SEN-206 count events. Every log of a run, and the
events `state.json` kept, are put in the order they happened and each
event is counted with those of the same key in the `count_window` before
it, whatever file it is in. A rule is reported when its count reaches
the limit, and then not again for that key until a window has passed:
a burst gives one finding, and an attack that goes on gives one for each
window. To find a slower attack, widen the window: one failed login
every 4 minutes never puts 5 in `10m`, but does in `1h`.

The time is read from each line: ISO 8601 with its zone (`Z`, `+01:00`),
the combined format (`02/Nov/2026:10:30:00 +0000`) and syslog
(`Nov  2 12:01:04`). A syslog time has no year and no zone: it is taken
as UTC, of this year, or of the year before for a month still to come.
A line whose time cannot be read is counted at the time of the line
before it in the same log. The events of log sources (Loki and the
rest) are kept like those of files.

### Alerts

With `alert_webhook`, a run that has addresses of priority P1 or P2 sends
`{"text": ...}` to that address, the form Slack and alike accept: one line
for each address with its priority, proposal, tactics and rules. No line
of a log is in it, so nothing an attacker wrote leaves the machine. An
alert is not an approval: nothing is blocked by it. Add the host of the
webhook to the `policy` block. A failed alert is said and recorded, and
the run goes on.

### Integrations

| With | What happens | Setting |
|---|---|---|
| Loki, Elasticsearch or OpenSearch, Splunk, Datadog, as sources | `sentinel.nexo` reads the lines of each query in the last `source_window`, and every rule applies to them; a finding is shown at `kind:name:line` | `sources` in `sentinel.json` |
| Alertmanager | one alert for each address of priority P1 (`critical`) or P2 (`warning`); for each restart that is critical or high; for each container near a limit. Alertmanager routes them to PagerDuty, Opsgenie, Slack, mail or whatever it is set up for | `alertmanager` in the three settings files |
| A webhook | one message with the urgent addresses, in the form Slack takes | `alert_webhook` in `sentinel.json` |
| Grafana | an annotation for each restart that is critical or high, with the tags `nexo-sentinel`, `restart`, the namespace and the severity, to see it on the dashboards | `grafana` in `restarts.json`, and its token in `SENTINEL_GRAFANA_AUTH` (`"Bearer <token>"`) |
| Prometheus and Loki | the two monitoring programs below | `restarts.json`, `resources.json` |

Alerts are named `SentinelAttack`, `SentinelRestart` and `SentinelResource`
and carry the label `source="nexo-sentinel"`, to route them. None carries
a line of a log. Each integration is off until its setting names an
address, and the host of each has to be added to the `policy` block of
the program. A failure to tell is said (and, in `sentinel.nexo`,
recorded) and the report stands.

`notify.nexo` is the module with Alertmanager and Grafana.

### Log sources

A line is read in any of three forms, whichever it comes from: the simple
form of the examples, the combined format of nginx and Apache, and the
lines of `sshd` in a syslog file such as `/var/log/auth.log` (`Failed
password for ... from address`, `Accepted ... for ... from address`),
with a classic or an ISO time. Other lines of a syslog file (cron, sudo,
systemd) are not reported as malformed.

Besides the files of `logs`, each entry of `sources` is a query to a
system that keeps logs:

| `kind` | `query` is | Also | With `"auth": true`, the `Authorization` header is read from |
|---|---|---|---|
| `loki` | LogQL, such as `{job="nginx"}` | | `SENTINEL_LOKI_AUTH` |
| `elastic` | a query string, such as `service:web` | `index`, the index or pattern; `field`, the field with the line (`message`). Elasticsearch and OpenSearch | `SENTINEL_ELASTIC_AUTH` (`"ApiKey ..."` or `"Basic ..."`) |
| `splunk` | what follows `search`, such as `index=web` | | `SENTINEL_SPLUNK_AUTH` (`"Bearer ..."`) |
| `datadog` | a log search, such as `service:web` | always sends `SENTINEL_DATADOG_API_KEY` and `SENTINEL_DATADOG_APP_KEY` | |
| `graylog` | a search, such as `source:web1` | uses the relative search of Graylog's REST API | `SENTINEL_GRAYLOG_AUTH` (`"Basic ..."`, of a token) |
| `azure` | KQL over a Log Analytics workspace of Azure Monitor, such as `ContainerLogV2 \| order by TimeGenerated asc` | `workspace`, its id; `field`, the column with the line (`LogMessage`). `url` is `https://api.loganalytics.io` | always `SENTINEL_AZURE_AUTH` (`"Bearer <token>"`) |
| `gcp` | a filter of Google Cloud Logging, such as `resource.type="k8s_container"` | `project`, the project's id. `url` is `https://logging.googleapis.com` | always `SENTINEL_GCP_AUTH` (`"Bearer <token>"`) |

```json
"sources": [
  {"name": "edge", "kind": "loki", "url": "http://loki:3100", "query": "{job=\"nginx\"}"},
  {"name": "web", "kind": "elastic", "url": "https://es.example.com:9200", "index": "logs-web-*", "query": "service:web", "auth": true},
  {"name": "legacy", "kind": "splunk", "url": "https://splunk.example.com:8089", "query": "index=web", "auth": true},
  {"name": "cloud", "kind": "datadog", "url": "https://api.datadoghq.com", "query": "service:web"}
]
```

Each needs its host in the `policy` block. The keys are read as secrets
and never shown. A source that fails is listed under `SKIPPED` and the
rest is scanned. Each run reads the whole `source_window` of a source
(the newest `source_lines` of it). With `incremental` it leaves out the
lines the run before saw: `state.json` keeps, for each source, the
time of its newest line and how many lines it had at that second, never
the text of a line. Lines of a source whose times are the same and come
in another order are taken as the same lines.
`logsources.nexo` is the module with the seven.

For Azure the rows come in the order of the query, so order it by time,
the oldest first; the first `source_lines` rows are read. For Google a
text entry is its text, an entry with a `message` in its JSON is that
message, and a request of a load balancer is read as an access line of
its address, so the rules for requests apply to it; other entries (audit
records) are left out. Both take a token that lasts about an hour:

```
export SENTINEL_AZURE_AUTH="Bearer $(az account get-access-token --resource https://api.loganalytics.io --query accessToken -o tsv)"
export SENTINEL_GCP_AUTH="Bearer $(gcloud auth print-access-token)"
```

A run on a schedule has to renew the token before it starts; Sentinel
does not ask Azure or Google for one itself.

### Amazon CloudWatch Logs

CloudWatch is read by a program of its own, `cloudwatch.nexo`, which
copies the new events of each log group of `cloudwatch.json` into
`logs/cloudwatch-NAME.log`. `sentinel.nexo` scans that file like any
other (add it to `logs`), so `incremental` works on it.

```
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...   # and AWS_SESSION_TOKEN, if temporary
../../.venv/bin/nexo cloudwatch.nexo && ../../.venv/bin/nexo sentinel.nexo
```

| Field of `cloudwatch.json` | Meaning |
|---|---|
| `region` | the region of the log groups, such as `eu-west-1` |
| `endpoint` | `""` for AWS itself (`https://logs.REGION.amazonaws.com`), or another address. The file in this folder names LocalStack on this machine, so that running the example sends nothing to AWS until you clear it |
| `window` | how far back the first run reads, such as `15m`; later runs read from the last event on (`cloudwatch-state.json`) |
| `max_pages` | the most pages of 1000 events read from a group in a run; the rest waits for the next |
| `groups` | each `{"name", "group", "filter"}`: the name of the file, the log group and an optional filter pattern |

The keys need the permission `logs:FilterLogEvents` on the groups and no
other. It is a separate program because of what it needs: requests to
AWS are signed (Signature Version 4), and Nexo has no HMAC of its own, so
the program uses Python's `hmac` and `hashlib` through `use python` and
reads the keys with Python's `os`. Its `policy` allows those modules, and
the keys are therefore not under `allow env`. Keeping it apart leaves
`sentinel.nexo` without Python and without the keys of AWS. Add
`allow net "logs.REGION.amazonaws.com"` to its policy for a release build.

## What it may touch

The `policy` block allows reading `sentinel.json`, `logs/` and
`audit.json` and `state.json`, the five variables with the keys of the
log sources, writing `audit.json`, `report.json` and
`state.json`, and the network of this machine only;
shell commands are denied. The program also refuses any log whose name is
not under `logs/`. In development mode the policy warns; a release build
(`nexo build --release`) stops, so a link inside `logs/` that points
elsewhere cannot be read. The policy is checked by the standard library;
it is not isolation by the operating system.

Each run adds its records to `audit.json`: the scan, every file skipped
and why, every finding suppressed and its reason, the report written, whether an
alert was sent, whether advice was given, the answer to the approval question and
every address blocked or not.

## Files

- `logs/`: the fixed corpus. `benign.log` (no findings, with look-alikes
  such as `/search?q=union+station+hotel` and `/docs/javascript-guide.html`), `attack.log`, `unicode.log`,
  `nginx.log` (the combined format), `proxy.log` (behind a load balancer, and IPv6), `auth.log` (syslog, with sshd), `oversized.log` and `binary.log` (not UTF-8, skipped).
- `findings.json`: the findings the corpus must give, each `[file, line, rule]`.
- `sentinel.expected`: the output when both questions are answered `no`.
- `cloudwatch.nexo`, `cloudwatch.json`: the collector of CloudWatch Logs.
- `addresses.nexo`: IPv4 and IPv6, ranges and the client behind proxies.
- `safe.nexo`: the module that decides what may be shown, used by the
  three programs.
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

`monitoring.nexo` is the module it and `restarts.nexo` use for the queries. Like the restarts report, it has run against stand-ins
for the two services (`tests/test_sentinel_resources.py`), not against a
real Prometheus or Loki.

Not done yet from the roadmap: the measurements against Python (startup,
throughput, memory, size) and running both under the same isolation
profile.
