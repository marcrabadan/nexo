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

It asks two questions. Anything but `yes` (an empty answer too) is a no.

1. `Ask the local model for a summary?` sends the rule, severity, address
   and place of each finding to Ollama on this machine, and prints the
   answer. The text of a log is never sent, and the answer is never read
   by the program: it cannot block an address or answer the next question.
2. `Block them on the firewall?` sends `{"address": ..., "by": "sentinel"}`
   to `firewall + "/block"` for each address with a high finding that is
   not in `never_block`.

## What it detects

A log line is `time address service message`. The rules are fixed regular
expressions; no model takes part.

| Rule | Severity | A line is reported when |
|---|---|---|
| `brute_force` | high | an address reaches 5 failed logins in a file |
| `sql_injection` | high | the message has `union select`, `or 1=1` or a quote followed by `--` or `;` (also URL-encoded) |
| `path_traversal` | medium | the message has `../` (also URL-encoded) |
| `sensitive_file` | medium | the message asks for `/etc/passwd`, `/.env`, `/.git/`, `/.ssh/` or `wp-config.php` |
| `hidden_text` | medium | the line has invisible or direction-changing Unicode characters, or control characters such as terminal escapes |
| `oversized_line` | low | the line is longer than 4096 characters; it is not examined further |
| `malformed_line` | low | the line is not a log entry, or its address is not an IPv4 address |

Evidence is cut at 80 characters and hidden or control characters are
printed as `<hidden>` and `<control>`, so a log cannot rewrite the terminal
of whoever reads the report.

## What it may touch

The `policy` block allows reading `sentinel.json`, `logs/` and
`audit.json`, writing `audit.json`, and the network of this machine only;
shell commands are denied. The program also refuses any log whose name is
not under `logs/`. In development mode the policy warns; a release build
(`nexo build --release`) stops, so a link inside `logs/` that points
elsewhere cannot be read. The policy is checked by the standard library;
it is not isolation by the operating system.

Each run adds its records to `audit.json`: the scan, every file skipped
and why, whether advice was given, the answer to the approval question and
every address blocked or not.

## Files

- `logs/`: the fixed corpus. `benign.log` (no findings, with look-alikes
  such as `/search?q=union+station+hotel`), `attack.log`, `unicode.log`,
  `oversized.log` and `binary.log` (not UTF-8, skipped).
- `findings.json`: the findings the corpus must give, each `[file, line, rule]`.
- `sentinel.expected`: the output when both questions are answered `no`.
- `compare/sentinel.py`: the same program in Python, with the same output
  and audit records.

`tests/test_sentinel.py` checks, in the interpreter and the compiler:
the findings equal `findings.json` without any call to a model; a refusal
sends nothing to the firewall; an answer of the model that says "yes" blocks
nothing; a failing firewall is recorded; a release build cannot read
outside `logs/`; and the Python comparison prints the same.

Not done yet from the roadmap: the measurements against Python (startup,
throughput, memory, size) and running both under the same isolation
profile.
