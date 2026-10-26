#!/usr/bin/env bash
# The Docker socket belongs to this host-side launcher, never to the container.
set -euo pipefail
shield_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
image=${NEXO_SHIELD_IMAGE:-nexo-shield:local}
uid=${SHIELD_UID:-$(id -u)}
gid=${SHIELD_GID:-$(id -g)}
if [[ $uid == 0 ]]; then uid=10001; gid=10001; fi
[[ $uid =~ ^[1-9][0-9]*$ && $gid =~ ^[0-9]+$ ]] || { echo "Invalid non-root UID/GID" >&2; exit 2; }
options=(--rm --pull=never --init --network=none --user "$uid:$gid"
    --read-only --cap-drop=ALL --security-opt=no-new-privileges:true
    --security-opt="seccomp=$shield_dir/seccomp.json" --cgroupns=private
    --memory=512m --memory-swap=512m --cpus=1 --pids-limit=64
    --ulimit=core=0 --ulimit=nofile=256:256
    --tmpfs=/tmp:rw,exec,nosuid,nodev,size=134217728,mode=1777)
project=
output=
while [[ $# -gt 0 ]]; do
    case $1 in
        --project|--output)
            flag=$1
            [[ $# -ge 2 && -d $2 ]] || { echo "$flag requires an existing directory" >&2; exit 2; }
            path=$(cd -- "$2" && pwd -P)
            [[ $path != *','* && $path != *$'\n'* ]] || { echo "Unsupported mount path" >&2; exit 2; }
            if [[ $flag == --project ]]; then
                [[ -z $project ]] || { echo "--project may be supplied once" >&2; exit 2; }
                project=$path
            else
                [[ -z $output ]] || { echo "--output may be supplied once" >&2; exit 2; }
                output=$path
            fi
            shift 2;;
        --) shift; break;;
        *) break;;
    esac
done
if [[ -n $project && -n $output && ( "$project/" == "$output/"* || "$output/" == "$project/"* ) ]]; then
    echo "Project and output mounts must be disjoint directories" >&2; exit 2
fi
if [[ -n $project ]]; then options+=(--mount "type=bind,src=$project,dst=/app,readonly"); fi
if [[ -n $output ]]; then
    options+=(--mount "type=bind,src=$output,dst=/out")
else
    options+=(--tmpfs "/out:rw,noexec,nosuid,nodev,size=16777216,uid=$uid,gid=$gid,mode=0700")
fi
exec docker run "${options[@]}" "$image" "$@"
