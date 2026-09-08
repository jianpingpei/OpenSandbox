FROM alpine:3.21 AS runtime
RUN apk add --no-cache iproute2 iptables e2fsprogs util-linux curl ca-certificates
WORKDIR /workspace
COPY bin/controller bin/fastlet bin/fastlet-proxy bin/janitor bin/ingress /workspace/
COPY bin/sandbox-init bin/sandbox-tunnel /opt/fast-sandbox/bin/
ENV RUNTIME_SOCKET=/run/containerd/containerd.sock
ENTRYPOINT ["/workspace/fastlet"]

FROM runtime AS proxy
ENTRYPOINT ["/workspace/fastlet-proxy"]

FROM python:3.11-alpine AS workload
RUN apk add --no-cache bash curl
COPY bin/execd /execd
ENTRYPOINT ["/execd"]

FROM alpine:3.21 AS egress
RUN apk add --no-cache iproute2 iptables nftables util-linux curl ca-certificates
COPY bin/egress /bin/egress
ENTRYPOINT ["/bin/egress"]
