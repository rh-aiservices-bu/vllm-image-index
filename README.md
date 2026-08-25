# vLLM Image Index — OpenShift Deployment

Kustomize-based deployment for the vLLM Image Index: a small web application that lists vLLM container images from Red Hat registries and DockerHub, updated daily by a CronJob.

![image](../img/ui.png)

## How it works

1. A CronJob (`vllm-fetch`) runs daily at 06:00 UTC. It calls `fetch.py`, which queries `registry.redhat.io` and DockerHub for vLLM image tags, resolves vLLM versions and OS details, and writes the result to `data.json`.
2. The CronJob patches the `vllm-image-data` ConfigMap via the Kubernetes API with the new `data.json`.
3. An nginx pod serves `index.html` and `data.json` from the ConfigMap, fronted by an OpenShift OAuth proxy for authentication.

## Directory structure

```
openshift-deployment/
├── Containerfile            # Builds the fetch image (ubi9-minimal + python3 + skopeo)
├── kustomization.yaml       # Root kustomization — sets namespace, generates vllm-image-data ConfigMap
├── app/
│   ├── fetch.py             # Registry scraper (run by the CronJob)
│   └── index.html           # Single-page frontend
├── base/
│   ├── kustomization.yaml
│   ├── cronjob.yaml         # Daily fetch CronJob
│   ├── deployment.yaml      # nginx + OAuth proxy
│   ├── service.yaml
│   ├── route.yaml           # TLS reencrypt route
│   ├── rbac.yaml            # Role/RoleBinding for the fetch ServiceAccount
│   ├── serviceaccount.yaml
│   └── nginx.conf
└── scripts/
    └── entrypoint.sh        # CronJob entrypoint: seed cache → fetch → patch ConfigMap
```

## Prerequisites

- An OpenShift cluster with the `oc` CLI configured
- The target namespace created (default: `vllm-image-index`)
- Two Secrets present in the namespace before deploying (see below)

## Required Secrets

### `rh-registry-pull-secret`

A `.dockerconfigjson` credential for `registry.redhat.io`. The CronJob mounts this at `/mnt/auth/.dockerconfigjson`.

```bash
oc create secret generic rh-registry-pull-secret \
  --from-file=.dockerconfigjson=$HOME/.config/containers/auth.json \
  --type=kubernetes.io/dockerconfigjson \
  -n vllm-image-index
```

### `vllm-image-index-oauth-cookie`

A random cookie secret for the OAuth proxy.

```bash
oc create secret generic vllm-image-index-oauth-cookie \
  --from-literal=cookie-secret=$(openssl rand -base64 32) \
  -n vllm-image-index
```

The `vllm-image-index-proxy-tls` Secret is generated automatically by OpenShift's service serving certificate controller.

## Deploying

```bash
oc apply -k openshift-deployment/
```

On first deploy the `vllm-image-data` ConfigMap contains an empty `data.json` placeholder. The index will show no images until the CronJob runs. To populate it immediately, trigger a manual run:

```bash
oc create job --from=cronjob/vllm-fetch vllm-fetch-manual -n vllm-image-index
```

## Building the fetch image

```bash
podman build -f openshift-deployment/Containerfile -t quay.io/<your-org>/vllm-image-index-fetch:latest openshift-deployment/
podman push quay.io/<your-org>/vllm-image-index-fetch:latest
```

Update the image reference in `base/cronjob.yaml` to match.
