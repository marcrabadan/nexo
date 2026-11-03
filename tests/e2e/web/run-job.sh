#!/usr/bin/env bash
set -euo pipefail
: "${BASE_URL:?Set BASE_URL to the deployed M6 demo HTTPS origin}"
namespace="${NAMESPACE:-default}"
browser="${BROWSER:-chromium}"
image="${E2E_IMAGE:-nexo-web-e2e:local}"
results="${RESULTS_DIR:-./web-e2e-results}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p -- "$results"
manifest="$(mktemp)"
trap 'rm -f -- "$manifest"' EXIT
export BASE_URL browser image
kubectl -n "$namespace" create --dry-run=client -f "$script_dir/job.yaml" -o json > "$manifest"
python3 - "$manifest" <<'PY'
import json,os,sys
path=sys.argv[1]
with open(path) as f:job=json.load(f)
container=job['spec']['template']['spec']['containers'][0]
container['image']=os.environ['image']
for item in container['env']:
    if item['name']=='NEXO_E2E_BASE_URL':item['value']=os.environ['BASE_URL']
    if item['name']=='NEXO_E2E_BROWSER':item['value']=os.environ['browser']
with open(path,'w') as f:json.dump(job,f)
PY
job="$(kubectl -n "$namespace" create -f "$manifest" -o jsonpath='{.metadata.name}')"
printf 'Created Job %s in namespace %s\n' "$job" "$namespace"
results="$results/$job"
mkdir -p -- "$results"
# Preserve manifests/diagnostics even if image pull or scheduling fails.
kubectl -n "$namespace" get job "$job" -o yaml > "$results/job.yaml"
pod=""
for ((attempt=0;attempt<60;attempt++)); do
    pod="$(kubectl -n "$namespace" get pod -l "job-name=$job" -o jsonpath='{.items[*].metadata.name}')"
    if [[ -n "$pod" ]]; then break;fi
    sleep 1
done
if [[ -z "$pod" ]]; then
    kubectl -n "$namespace" describe job "$job" > "$results/job-description.txt"
    printf 'Job controller did not create a Pod; see %s\n' "$results" >&2
    exit 1
fi
if ! kubectl -n "$namespace" wait --for=condition=Ready "pod/$pod" --timeout=180s; then
    kubectl -n "$namespace" describe pod "$pod" > "$results/pod-description.txt"
    kubectl -n "$namespace" logs "$pod" > "$results/runner.log" 2>&1 || true
    exit 1
fi
ready=0
for ((attempt=0;attempt<100;attempt++)); do
    if kubectl -n "$namespace" exec "$pod" -- test -f /results/report.json 2>/dev/null; then ready=1;break;fi
    phase="$(kubectl -n "$namespace" get pod "$pod" -o jsonpath='{.status.phase}')"
    if [[ "$phase" == Failed || "$phase" == Succeeded ]]; then break;fi
    sleep 2
done
kubectl -n "$namespace" logs "$pod" > "$results/runner.log"
if [[ "$ready" != 1 ]]; then printf 'No completed E2E report; see %s/runner.log\n' "$results" >&2;exit 1;fi
kubectl -n "$namespace" cp "$pod:/results/." "$results/"
expected="$(python3 - "$results/report.json" <<'PY'
import json,sys
with open(sys.argv[1]) as f:report=json.load(f)
print(0 if report.get('status')=='passed' and len(report.get('tests',[]))==5 else 1)
PY
)"
kubectl -n "$namespace" exec "$pod" -- touch /results/.collected
kubectl -n "$namespace" wait "--for=jsonpath={.status.containerStatuses[0].state.terminated.exitCode}=$expected" "pod/$pod" --timeout=30s
kubectl -n "$namespace" get job "$job" -o yaml > "$results/job.yaml"
printf 'Artifacts: %s; Job retained for inspection (TTL one hour).\n' "$results"
exit "$expected"
