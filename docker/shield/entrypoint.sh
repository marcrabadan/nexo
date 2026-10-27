#!/bin/sh
# Verify the requested Docker boundary; an image cannot set host runtime flags.
set -eu
reject() { echo "Error: Shield container requires $*. Use docker/shield/run.sh or its Compose configuration." >&2; exit 125; }
[ "$(id -u)" != 0 ] || reject "a non-root user"
awk '$1 ~ /^Cap(Inh|Prm|Eff|Bnd|Amb):$/ && $2 !~ /^0+$/ { bad=1 } $1=="NoNewPrivs:" { nnp=$2 } $1=="Seccomp:" { seccomp=$2 } END { exit (bad || nnp!=1 || seccomp!=2) }' /proc/self/status || reject "zero Linux capabilities, no-new-privileges and seccomp"
awk '$2=="/" && $4 ~ /(^|,)ro(,|$)/ { found=1 } END { exit !found }' /proc/mounts || reject "a read-only root filesystem"
for interface in /sys/class/net/*; do
    [ "${interface##*/}" = lo ] || reject "an offline network namespace (--network=none)"
done
for limit in memory.max pids.max; do
    [ -r "/sys/fs/cgroup/$limit" ] || reject "readable cgroup v2 resource limits"
    value=$(cat "/sys/fs/cgroup/$limit")
    case "$value" in ''|max|*[!0-9]*) reject "finite $limit";; esac
    [ "$value" -gt 0 ] || reject "positive $limit"
done
[ "$(cat /sys/fs/cgroup/memory.swap.max)" = 0 ] || reject "swap disabled for this container"
read -r quota period < /sys/fs/cgroup/cpu.max
case "$quota" in ''|max|*[!0-9]*) reject "a finite CPU quota";; esac
[ "$quota" -gt 0 ] && [ "$period" -gt 0 ] || reject "a positive CPU quota"
command=${1:-run}
if [ "$#" -gt 0 ]; then shift; fi
case "$command" in
    probe)
        if [ "$#" -eq 0 ]; then exec nexo security probe; fi
        [ "$#" -eq 1 ] && [ "$1" = --require-linux ] || { echo "Usage: shield probe [--require-linux]" >&2; exit 2; }
        result=$(nexo security probe)
        printf '%s\n' "$result"
        case "$result" in *'"supported":true'*) exit 0;; *) exit 125;; esac
        ;;
    run|trace|check|audit|worker) exec nexo security "$command" "$@";;
    run-linux) exec nexo security run --sandbox=linux "$@";;
    trace-linux) exec nexo security trace --sandbox=linux "$@";;
    *) echo "Usage: shield run|trace|check|audit|worker|run-linux|trace-linux|probe ..." >&2; exit 2;;
esac
