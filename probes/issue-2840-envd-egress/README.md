# envd-only Fast Sandbox + Shared Egress Repro Kit

Reproduces the `#2840` Q1–Q4 verification with shared `egress` (`OPENSANDBOX_EGRESS_PROFILE=fast-sandbox`, `dns+nft`, transparent MITM) on a Linux/amd64 KVM `kind` cluster running `fast-sandbox` and `opensandbox/egress`.

## Prerequisites

- A live KVM `kind` cluster with `fast-sandbox` (`fsb-controller`, `fsb-firecracker-runtime`, `fsb-fastlet`, `fsb-sandboxtemplate-builder`) and `opensandbox/egress` images loaded, plus `sandbox-oss-credentials` in `opensandbox-dataplane`.
- Upstream `envd 0.8.0` binary (`e2b-dev/runtime` release `2026.30`, commit `f32ee8a2a50052f32e3632ceb451111a98dd5104`, `sha256: 101e86ad3841eb289c4332c58f8d756b6f3bd0e166a98554cb687b7290bf7347`).

## 1. Build and push the probe image (or use the prebuilt image)

You can use the anonymously pullable prebuilt image directly (`crpi-m4ebzf3qn4yixelo.cn-hangzhou.personal.cr.aliyuncs.com/jianpingpei/envd-only:probe-p01`, `sha256:8b462814d1f3a867404540f765ab29b10b7e79d335319cf25d827ca525e6ac06`), or place the `envd` `0.8.0` binary next to `Dockerfile` and build/push your own:

```bash
docker build -t <IMAGE_REF> .
docker push <IMAGE_REF>
```

## 2. Provision MITM CA, TLS echo endpoint, rules, and pool

```bash
./scripts/setup_mitm_echo.sh ./mitm-ca
kubectl apply -f manifests/egress-network-security-rules.yaml
# Substitute <FASTLET_IMAGE> and <EGRESS_IMAGE> in manifests/firecracker-egress-pool.yaml
kubectl apply -f manifests/firecracker-egress-pool.yaml
```

Verify `egress /healthz` inside the Fastlet Pod reports the same `mitmCaFingerprint` as `./mitm-ca/fingerprint`.

## 3. Build the envd-only template and run Q2 + negative control

```bash
# Substitute <IMAGE_REF> in manifests/sandboxtemplate-envd-only.yaml
kubectl apply -f manifests/sandboxtemplate-envd-only.yaml
kubectl -n opensandbox-dataplane wait --for=condition=BuildSucceeded=True sandboxtemplate/envd-only-egress-probe --timeout=180s

# Substitute <IMAGE_REF> and <ECHO_IP> (from ./mitm-ca/echo_ip) in manifests/sandboxes-q2-cases.yaml
kubectl apply -f manifests/sandboxes-q2-cases.yaml
kubectl -n opensandbox-dataplane get sandbox sbx-envd-allow sbx-envd-deny sbx-envd-sibling sbx-envd-bad-json
```

Expected: `sbx-envd-allow`, `sbx-envd-deny`, and `sbx-envd-sibling` reach `Ready=True`, while `sbx-envd-bad-json` stays `Ready=False` (`ActionBindingNotReady`).

## 4. Run in-guest L3/L4 and L7 MITM probes

Inspect the guest IPs (`172.30.0.x`) from `kubectl -n opensandbox-dataplane exec <fastlet-pod> -c egress -- nft list ruleset`, then run:

```bash
kubectl -n opensandbox-dataplane exec -i <fastlet-pod> -c egress -- \
  python3 - --allow-ip <ALLOW_IP> --deny-ip <DENY_IP> --sibling-ip <SIBLING_IP> --echo-ip <ECHO_IP> --rustfs-ip <RUSTFS_IP> \
  < scripts/probe_l3_l4.py

openssl req -x509 -new -newkey rsa:2048 -nodes -days 1 -keyout ./mitm-ca/dummy.key -out ./mitm-ca/dummy.crt -subj "/CN=dummy-rotated-ca"
kubectl -n opensandbox-dataplane exec -i <fastlet-pod> -c egress -- \
  python3 - --deny-ip <DENY_IP> --deny-uid <DENY_UID> --echo-ip <ECHO_IP> \
  --ca-pem-b64 "$(base64 -w0 ./mitm-ca/tls.crt)" --dummy-pem-b64 "$(base64 -w0 ./mitm-ca/dummy.crt)" \
  < scripts/probe_l7_mitm.py
```

## 5. Run envd `/init`, default-user, and sticky `/tmp` checks (P0-2)

Run inside a clean `envd-only` container with `python3` to verify `/init` `ReplaceUserVars`, `timestamp` ordering, restart memory wipe, `defaultUser` behavior, and `fs.protected_regular=2` sticky `/tmp` overwrite `HTTP 500`:

```bash
docker run --rm -i --entrypoint python3 -v "$PWD/scripts/probe_init_and_tmp.py:/probe_init_and_tmp.py:ro" \
  crpi-m4ebzf3qn4yixelo.cn-hangzhou.personal.cr.aliyuncs.com/jianpingpei/envd-only:probe-p01 /probe_init_and_tmp.py
```

## 6. Run `server -> ingress -> fastlet -> envd` streaming & HTTP/2 checks (P1-2)

With `opensandbox-server` and `opensandbox-ingress-gateway` deployed and an `fsb-*` envd-only sandbox (`<SANDBOX_ID>`) running on `firecracker-egress-pool`:

```bash
python3 scripts/probe_ingress_streaming.py \
  --server-url http://<SERVER_HOST>:<SERVER_PORT> \
  --api-key <OPEN_SANDBOX_API_KEY> \
  --sandbox-id <SANDBOX_ID>
```
