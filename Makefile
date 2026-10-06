IMAGE     ?= quay.io/rh-aiservices-bu/vllm-image-index
TAG       ?= v0.1.0 # Current deployed version
NAMESPACE ?= vllm-image-index

.PHONY: build push deploy

build:
	podman build -t $(IMAGE):$(TAG) .

push:
	podman push $(IMAGE):$(TAG)

deploy:
	oc create configmap vllm-image-data \
	  --from-literal=data.json='{}' \
	  -n $(NAMESPACE) 2>/dev/null || true
	oc apply -k .
