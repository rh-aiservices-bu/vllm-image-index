#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-vllm-image-index}"

# 1. Namespace
if ! oc get namespace "$NAMESPACE" &>/dev/null; then
  echo "Creating namespace $NAMESPACE..."
  oc create namespace "$NAMESPACE"
else
  echo "Namespace $NAMESPACE already exists."
fi

# 2. rh-registry-pull-secret
if ! oc get secret rh-registry-pull-secret -n "$NAMESPACE" &>/dev/null; then
  echo ""
  read -rp "Path to registry.redhat.io .dockerconfigjson file: " RH_JSON
  oc create secret generic rh-registry-pull-secret \
    --from-file=.dockerconfigjson="$RH_JSON" \
    --type=kubernetes.io/dockerconfigjson \
    -n "$NAMESPACE"
  echo "Created rh-registry-pull-secret."
else
  echo "Secret rh-registry-pull-secret already exists, skipping."
fi

# 3. dockerhub-pull-secret
if ! oc get secret dockerhub-pull-secret -n "$NAMESPACE" &>/dev/null; then
  echo ""
  read -rp "Path to DockerHub .dockerconfigjson file (leave blank for unauthenticated placeholder): " DH_JSON
  if [[ -n "$DH_JSON" ]]; then
    oc create secret generic dockerhub-pull-secret \
      --from-file=.dockerconfigjson="$DH_JSON" \
      --type=kubernetes.io/dockerconfigjson \
      -n "$NAMESPACE"
  else
    oc create secret generic dockerhub-pull-secret \
      --from-literal=.dockerconfigjson='{"auths":{}}' \
      -n "$NAMESPACE"
  fi
  echo "Created dockerhub-pull-secret."
else
  echo "Secret dockerhub-pull-secret already exists, skipping."
fi

# 4. vllm-image-index-oauth-cookie
if ! oc get secret vllm-image-index-oauth-cookie -n "$NAMESPACE" &>/dev/null; then
  oc create secret generic vllm-image-index-oauth-cookie \
    --from-literal=cookie-secret="$(openssl rand -base64 32)" \
    -n "$NAMESPACE"
  echo "Created vllm-image-index-oauth-cookie."
else
  echo "Secret vllm-image-index-oauth-cookie already exists, skipping."
fi

# 5. vllm-image-data ConfigMap (seed if absent)
if ! oc get configmap vllm-image-data -n "$NAMESPACE" &>/dev/null; then
  oc create configmap vllm-image-data \
    --from-literal=data.json='{}' \
    -n "$NAMESPACE"
  echo "Created vllm-image-data ConfigMap."
else
  echo "ConfigMap vllm-image-data already exists, skipping."
fi

# 6. Apply
echo ""
echo "Applying kustomization..."
oc apply -k .

# 7. Seed data if configmap is empty
DATA=$(oc get configmap vllm-image-data -n "$NAMESPACE" -o jsonpath='{.data.data\.json}' 2>/dev/null || echo "")
if [[ -z "$DATA" || "$DATA" == "{}" || "$DATA" == "[]" ]]; then
  echo ""
  echo "data.json is empty — triggering vllm-fetch CronJob to populate it..."
  oc create job --from=cronjob/vllm-fetch vllm-fetch-manual -n "$NAMESPACE"
fi
