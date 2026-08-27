FROM registry.access.redhat.com/ubi9/ubi-minimal

RUN microdnf install -y python3 skopeo tar gzip && microdnf clean all

WORKDIR /app
COPY app/fetch.py .
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER 1001
ENTRYPOINT ["/entrypoint.sh"]
