---
title: Fleets Server and Ingress HTTP E2E
description: Exercise real FastPath, Fastlets, Server, and Ingress with a local containerd runtime.
---

# Fleets HTTP E2E

This suite reproduces the upper-layer checks from fast-sandbox's integration
environment through OpenSandbox Server and Ingress. It uses a real Kubernetes
API, FastPath, Fastlets, containerd sandboxes, execd and egress. It does not use an
SDK, a fake FastPath, or the central Sandbox Proxy.

The container runtime replaces Firecracker only for local testing. It does not
validate snapshot building/restoration, template catalog APIs, P2P artifact
distribution, or Firecracker performance.

## Prerequisites

- Docker, Kind, kubectl, Go matching the source modules, and the Server Python
  environment (`cd server && uv sync --all-groups`).
- An unused disposable Kind cluster name and local ports `28080`/`28890`.
- Outbound access to `example.com` and `1.1.1.1`; the suite proves the allow
  control works before asserting denial. Unavailable external access is a test
  failure, not a successful deny test.
- Fast-sandbox checkout with FastPath v2 and Sandbox Actions. Tested revision:
  `11b21bf6a6ce730d48ea6e3e3d0050db2607ae49`.
- OpenSandbox egress source with Actions support. Tested revision:
  `5c0b901a460ab1365e93f09f32215b341c223070`. Pass that separate checkout to
  `--egress-source` if the Server branch predates the egress integration. This
  dependency is built from source; the suite does not substitute a mock handler.

## Run

Run from the OpenSandbox checkout containing the Server/Ingress changes:

```bash
FLEETS_E2E_DIR=$(mktemp -d)
kind create cluster --name osb-fleets-http --image kindest/node:v1.27.3 \
  --kubeconfig "$FLEETS_E2E_DIR/kubeconfig"

server/.venv/bin/python tests/fleets/build.py \
  --fast-sandbox /path/to/fast-sandbox \
  --egress-source /path/to/opensandbox-with-actions \
  --output "$FLEETS_E2E_DIR" --cluster osb-fleets-http

server/.venv/bin/python tests/fleets/deploy.py \
  --fast-sandbox /path/to/fast-sandbox \
  --kubeconfig "$FLEETS_E2E_DIR/kubeconfig" --output "$FLEETS_E2E_DIR"

server/.venv/bin/python tests/fleets/http_e2e.py \
  --kubeconfig "$FLEETS_E2E_DIR/kubeconfig" \
  --api-key-file "$FLEETS_E2E_DIR/api-key" \
  --output "$FLEETS_E2E_DIR/results"
```

The deployment contains two basic Fastlets and one egress Fastlet. Execd is baked
into the workload image and accessed on raw port `44772`. Neither pool declares
runtime Infra Components. The egress pool runs a sidecar and declares only its
Action Handler. Because containerd copies the Fastlet resolver into each slot,
this test pool mounts a gateway resolver (`172.30.0.1`) in the Fastlet container,
matching the DNS path baked into the Firecracker template. Egress itself retains
Kubernetes DNS for upstream resolution.

Server runs in Fleets mode so HTTP Create targets that backend; this suite does
not change or test Kubernetes-mode Create backend selection. Gateway access is
`DIRECT_FASTLET_PROXY`. Route credentials have a 30-second test TTL so the
65-second access test crosses credential/cache refresh boundaries. Random test credentials are stored only in the output
directory. Do not publish `api-key` or `deployment.yaml`.

## Coverage

| Source-script behavior | HTTP acceptance path |
| --- | --- |
| Create two, then five concurrent sandboxes; multiple Fastlets | Server Create, stable endpoint, Ingress, raw port, execd ping; seven live sandboxes |
| Execd API battery | Malformed JSON, echo, pipe, sleep, nonzero exit and missing binary through Ingress |
| Per-sandbox network policy | HTTP Create binding; domain/IP allowlist versus deny on the same Fastlet |
| Live policy update | HTTP PUT → FastPath Update → actual network enforcement; other sandbox unchanged |
| Handler recovery | Restart egress; wait for a new handler instance and policy replay |
| Deletion cleanup | HTTP DELETE, CR disappearance, per-subject nft removal, containerd task removal |
| Additional Server/Ingress regressions | Environment injection, isolated files, upload/download, metadata, renewal, pagination, signed-route tampering and continued access |

Use `--suite basic` or `--suite egress` for focused iterations. After rebuilding
images, restart the test Server deployment and recreate the test Ingress Pod
before rerunning. If changing pool fixtures, wait for replacement Fastlets to
become warm. A run prints `RESULT=PASS` only after every selected assertion and
cleanup check succeeds; the output directory also contains the stage report
and port-forward logs.

Created sandboxes are deleted in `finally`, including on failure. The cluster
and images remain for diagnosis. After inspecting failures, remove only the
disposable cluster you created:

```bash
kind delete cluster --name osb-fleets-http
```
