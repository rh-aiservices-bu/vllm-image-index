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
    --output-dir "$WORKDIR" \
    --skip-models

echo "fetch.py complete — upserting ConfigMap $CM_NAME ..."

# Create the ConfigMap if it doesn't exist, then patch data.json into it.
python3 - <<'PYEOF'
import json, os, ssl, urllib.request, urllib.error

workdir = os.environ.get("WORKDIR", "/tmp/workdir")
sa_dir  = "/var/run/secrets/kubernetes.io/serviceaccount"
cm_name = os.environ.get("CM_NAME", "vllm-image-data")

with open(f"{sa_dir}/token") as f:
    token = f.read().strip()
with open(f"{workdir}/data.json") as f:
    data_json = f.read()
with open(f"{sa_dir}/namespace") as f:
    namespace = f.read().strip()

ctx = ssl.create_default_context(cafile=f"{sa_dir}/ca.crt")
base_url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/configmaps"
headers = {"Authorization": f"Bearer {token}"}

def do_request(url, body, method, content_type="application/json"):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read())

# Check if ConfigMap exists.
check = urllib.request.Request(f"{base_url}/{cm_name}")
check.add_header("Authorization", f"Bearer {token}")
try:
    with urllib.request.urlopen(check, context=ctx) as resp:
        exists = resp.status == 200
except urllib.error.HTTPError as e:
    exists = e.code != 404

if exists:
    body = json.dumps({"data": {"data.json": data_json}}).encode()
    result = do_request(f"{base_url}/{cm_name}", body, "PATCH", "application/merge-patch+json")
    print(f"ConfigMap patched — resourceVersion {result['metadata']['resourceVersion']}")
else:
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": cm_name, "namespace": namespace},
          "data": {"data.json": data_json}}
    body = json.dumps(cm).encode()
    result = do_request(base_url, body, "POST")
    print(f"ConfigMap created — resourceVersion {result['metadata']['resourceVersion']}")
PYEOF
