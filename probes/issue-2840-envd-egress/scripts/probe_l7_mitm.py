#!/usr/bin/env python3
"""Run 4-phase L7 MITM CA trust & /init caBundle remediation matrix from inside the Fastlet Pod netns."""

import argparse
import base64
import json
import struct
import urllib.error
import urllib.request


CLIENT_CMD = r'''
echo "--- env (ssl/cert/ca) ---"
env | grep -Ei 'ssl|cert|ca_' || echo "(none)"
echo "--- curl ---"
curl -fsS -m 8 https://echo.test/echo 2>&1 || echo "CURL_FAILED"
echo "--- python urllib ---"
python3 -c "import urllib.request; print(urllib.request.urlopen('https://echo.test/echo', timeout=8).read().decode())" 2>&1 || echo "URLLIB_FAILED"
echo "--- python requests ---"
python3 -c "import requests; print(requests.get('https://echo.test/echo', timeout=8).text)" 2>&1 || echo "REQUESTS_FAILED"
echo "--- node https ---"
node -e "const https=require('https'); https.get('https://echo.test/echo', r=>{let d=''; r.on('data',c=>d+=c); r.on('end',()=>console.log(d));}).on('error', e=>{console.error(e.code, e.message); process.exit(1);});" 2>&1 || echo "NODE_FAILED"
'''


def http_req(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", errors="replace")


def envd_init(ip, body_dict):
    body = json.dumps(body_dict).encode()
    st, _, resp = http_req(
        f"http://{ip}:49983/init",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    return st, resp


def envd_exec(ip, token, cmd):
    payload = json.dumps({"process": {"cmd": "/bin/bash", "args": ["-lc", cmd]}}).encode()
    frame = b"\x00" + struct.pack(">I", len(payload)) + payload
    req = urllib.request.Request(
        f"http://{ip}:49983/process.Process/Start",
        data=frame,
        method="POST",
        headers={
            "Content-Type": "application/connect+json",
            "Connect-Protocol-Version": "1",
            "X-Access-Token": token,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    pos = 0
    stdout, stderr, exit_code = [], [], None
    while pos + 5 <= len(raw):
        length = struct.unpack(">I", raw[pos + 1 : pos + 5])[0]
        chunk = raw[pos + 5 : pos + 5 + length]
        pos += 5 + length
        msg = json.loads(chunk.decode("utf-8", errors="replace"))
        ev = msg.get("event", {})
        if "data" in ev:
            d = ev["data"]
            if "stdout" in d:
                stdout.append(base64.b64decode(d["stdout"]).decode("utf-8", errors="replace"))
            if "stderr" in d:
                stderr.append(base64.b64decode(d["stderr"]).decode("utf-8", errors="replace"))
        if "end" in ev:
            exit_code = ev["end"].get("exitCode", 0)
    return exit_code, "".join(stdout).strip(), "".join(stderr).strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--deny-ip", required=True)
    p.add_argument("--deny-uid", required=True)
    p.add_argument("--echo-ip", required=True)
    p.add_argument("--ca-pem-b64", required=True)
    p.add_argument("--dummy-pem-b64", required=True)
    p.add_argument("--token", default="p01-test-token")
    args = p.parse_args()

    ca_pem = base64.b64decode(args.ca_pem_b64).decode()
    dummy_pem = base64.b64decode(args.dummy_pem_b64).decode()

    vault_payload = {
        "credentials": [{"name": "tok", "source": {"type": "inline", "value": "p0-1-mitm-secret"}}],
        "bindings": [{"name": "echo", "match": {"hosts": ["echo.test"]}, "auth": {"type": "apiKey", "name": "X-Api-Key", "credential": "tok"}}],
    }
    st_v, _, resp_v = http_req(
        f"http://127.0.0.1:18080/vault/{args.deny_uid}",
        method="POST",
        data=json.dumps(vault_payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    print(f"[Vault Bind] status={st_v} body={resp_v}")

    rc, out, _ = envd_exec(args.deny_ip, args.token, f"grep -q 'echo.test' /etc/hosts || echo '{args.echo_ip} echo.test' >> /etc/hosts; tail -n 2 /etc/hosts")
    print(f"[/etc/hosts setup] rc={rc} out={out}")

    print("\n=== Phase 4A: Pre-CA Baseline (no caBundle, no envVars) ===")
    _, out, _ = envd_exec(args.deny_ip, args.token, "ls -la /opt/opensandbox /usr/local/share/ca-certificates 2>&1 || true\n" + CLIENT_CMD)
    print(out)

    print("\n=== Phase 4B: POST /init with caBundle only ===")
    st, resp = envd_init(args.deny_ip, {"accessToken": args.token, "caBundle": ca_pem})
    print(f"POST /init (caBundle) -> {st} {resp}")
    _, out, _ = envd_exec(args.deny_ip, args.token, "ls -la /usr/local/share/ca-certificates/e2b-ca.crt 2>&1 || true\n" + CLIENT_CMD)
    print(out)

    print("\n=== Phase 4C: POST /init with caBundle + envVars ===")
    st, resp = envd_init(
        args.deny_ip,
        {
            "accessToken": args.token,
            "caBundle": ca_pem,
            "envVars": {
                "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
                "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
                "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca-certificates.crt",
            },
        },
    )
    print(f"POST /init (caBundle + envVars) -> {st} {resp}")
    _, out, _ = envd_exec(args.deny_ip, args.token, CLIENT_CMD)
    print(out)

    print("\n=== Phase 4D: CA Rotation via re-/init (dummy CA -> restore real CA) ===")
    st1, _ = envd_init(args.deny_ip, {"accessToken": args.token, "caBundle": dummy_pem})
    _, out1, _ = envd_exec(args.deny_ip, args.token, "sleep 0.5; curl -fsS -m 5 https://echo.test/echo 2>&1 || echo 'CURL_FAILED_AS_EXPECTED'")
    print(f"After dummy caBundle (/init={st1}): {out1}")
    st2, _ = envd_init(args.deny_ip, {"accessToken": args.token, "caBundle": ca_pem})
    _, out2, _ = envd_exec(args.deny_ip, args.token, "sleep 0.5; curl -fsS -m 5 https://echo.test/echo 2>&1 || echo 'CURL_RESTORE_FAILED'")
    print(f"After restoring real caBundle (/init={st2}): {out2}")


if __name__ == "__main__":
    main()
