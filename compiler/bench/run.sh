#!/bin/sh
# Time each benchmark with the phase 0 interpreter and as a native program
# built by `nexo build --release`. Run from the repository root:
#   compiler/bench/run.sh
set -e
here=$(dirname "$0")
nexo="$here/../target/release/nexo"
py="${PHASE0:-.venv/bin/nexo}"
tmp=$(mktemp -d)
for f in "$here"/*.nexo; do
    name=$(basename "$f" .nexo)
    "$nexo" build --release "$f" -o "$tmp/$name"
    t0=$(date +%s.%N); "$tmp/$name" > /dev/null; t1=$(date +%s.%N)
    t2=$(date +%s.%N); "$py" "$f" > /dev/null; t3=$(date +%s.%N)
    echo "$name: native $(echo "$t1 - $t0" | bc)s, phase 0 $(echo "$t3 - $t2" | bc)s"
done
rm -rf "$tmp"
