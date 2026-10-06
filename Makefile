IMAGE     ?= quay.io/rh-aiservices-bu/vllm-image-index
TAG       ?= v0.1.0 # Current deployed version
NAMESPACE ?= vllm-image-index

.PHONY: build push deploy

build:
	podman build -t $(IMAGE):$(TAG) .

push:
	podman push $(IMAGE):$(TAG)

deploy:
	NAMESPACE=$(NAMESPACE) bash scripts/deploy.sh
