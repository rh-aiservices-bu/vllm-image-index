#!/bin/bash
# CronJob entrypoint: seed from ConfigMap → run fetch.py → patch ConfigMap.
set -euo pipefail

WORKDIR=/tmp/workdir
AUTH_FILE=/mnt/auth/.dockerconfigjson
CACHE_DIR=/mnt/cache
CM_NAME=vllm-image-data

SA_DIR=/var/run/secrets/kubernetes.io/serviceaccount
TOKEN=$(cat "$SA_DIR/token")
CACERT="$SA_DIR/ca.crt"
NAMESPACE=$(cat "$SA_DIR/namespace")
API="https://kubernetes.default.svc/api/v1/namespaces/$NAMESPACE/configmaps/$CM_NAME"

mkdir -p "$WORKDIR"

# Seed data.json from ConfigMap mount (read-only) so this run can use the cache.
if [ -f "$CACHE_DIR/data.json" ]; then
    cp "$CACHE_DIR/data.json" "$WORKDIR/data.json"
    echo "Seeded data.json from ConfigMap cache ($(wc -c < "$WORKDIR/data.json") bytes)"
fi

# Run fetch.py.
cd "$WORKDIR"
python3 /app/fetch.py \
    --auth-file "$AUTH_FILE" \
    --dockerhub-config /mnt/dockerhub/.dockerconfigjson \
    --output-dir "$WORKDIR"

echo "fetch.py complete — patching ConfigMap $CM_NAME ..."

# Patch data.json key via the Kubernetes API.
python3 - <<'PYEOF'
import json, os, ssl, urllib.request

workdir = os.environ.get("WORKDIR", "/tmp/workdir")
sa_dir  = "/var/run/secrets/kubernetes.io/serviceaccount"
cm_name = os.environ.get("CM_NAME", "vllm-image-data")

with open(f"{sa_dir}/token") as f:
    token = f.read().strip()
with open(f"{workdir}/data.json") as f:
    data_json = f.read()
with open(f"{sa_dir}/namespace") as f:
    namespace = f.read().strip()

patch = {"data": {"data.json": data_json}}
body = json.dumps(patch).encode()

url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/configmaps/{cm_name}"
req = urllib.request.Request(url, data=body, method="PATCH")
req.add_header("Authorization", f"Bearer {token}")
req.add_header("Content-Type", "application/merge-patch+json")

ctx = ssl.create_default_context(cafile=f"{sa_dir}/ca.crt")
with urllib.request.urlopen(req, context=ctx) as resp:
    result = json.loads(resp.read())
    print(f"ConfigMap patched — resourceVersion {result['metadata']['resourceVersion']}")
PYEOF
