#!/usr/bin/env bash
set -euo pipefail
shield_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_dir=$(cd -- "$shield_dir/../.." && pwd -P)
target=${1:-runtime}
case $target in runtime) image=${NEXO_SHIELD_IMAGE:-nexo-shield:local};;
    validation) image=${NEXO_SHIELD_VALIDATION_IMAGE:-nexo-shield:validation};;
    *) echo "Usage: $0 [runtime|validation]" >&2; exit 2;; esac
options=(--build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY
    --build-arg http_proxy --build-arg https_proxy --build-arg no_proxy)
# Public trust anchors only, exposed for build steps through an ephemeral
# BuildKit secret. Proxy credentials and host environment files are not copied.
if [[ -n ${SSL_CERT_FILE:-} ]]; then
    [[ -r $SSL_CERT_FILE ]] || { echo "SSL_CERT_FILE must be readable" >&2; exit 2; }
    options+=(--secret "id=build_ca,src=$SSL_CERT_FILE")
fi
exec docker build "${options[@]}" -f "$shield_dir/Dockerfile" --target "$target" -t "$image" "$repo_dir"
