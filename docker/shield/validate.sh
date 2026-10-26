#!/bin/sh
set -eu
probe=$(/usr/local/bin/shield probe)
printf '%s\n' "$probe"
case ${1:-} in
    --require-linux)
        shift
        case "$probe" in *'"supported":true'*) :;; *) echo "Linux kernel isolation validation blocked: Landlock/seccomp prerequisites unavailable." >&2; exit 125;; esac;;
    '') :;;
    *) echo "Usage: shield-validate [--require-linux]" >&2; exit 2;;
esac
[ "$#" -eq 0 ] || exit 2
cd /workspace
for binary in /opt/nexo-tests/*; do "$binary" --test-threads=2; done
exec python -m pytest -q -o cache_dir=/tmp/pytest-cache tests/test_security.py tests/test_security_isolation.py tests/test_policy.py tests/test_http.py tests/unit/test_errors.py
