#!/usr/bin/env python3
"""Run in-guest envd /init, Connect Process/Start, and L3/L4 egress probes from inside the Fastlet Pod netns."""

import argparse
import base64
import json
import struct
import urllib.error
import urllib.request


def http_req(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def envd_init(ip, token):
    body = json.dumps({"accessToken": token}).encode()
    st, hdrs, _ = http_req(
        f"http://{ip}:49983/init",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    return st, {k: v for k, v in hdrs.items() if k.lower().startswith("x-envd")}


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
    return exit_code, "".join(stdout), "".join(stderr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--allow-ip", required=True)
    p.add_argument("--deny-ip", required=True)
    p.add_argument("--sibling-ip", required=True)
    p.add_argument("--echo-ip", required=True)
    p.add_argument("--rustfs-ip", required=True)
    p.add_argument("--token", default="p01-test-token")
    args = p.parse_args()

    for name, ip in [("CaseA-allow", args.allow_ip), ("CaseB-deny", args.deny_ip), ("CaseC-sibling", args.sibling_ip)]:
        st_h, _, _ = http_req(f"http://{ip}:49983/health")
        st_i, hdrs_i = envd_init(ip, args.token)
        st_m, _, _ = http_req(f"http://{ip}:49983/metrics")
        print(f"[{name} ({ip})] GET /health={st_h}, POST /init={st_i} headers={hdrs_i}, unauth GET /metrics={st_m}")

    tests = [
        ("CaseB_plumbing", args.deny_ip, "uname -a; id; cat /etc/resolv.conf; ip -4 addr; ip route; ss -lntp || true"),
        ("CaseB_dns_allow_example", args.deny_ip, "nslookup example.com"),
        ("CaseB_dns_deny_httpbin", args.deny_ip, "nslookup httpbin.org || true"),
        ("CaseB_https_allow_echo", args.deny_ip, f"curl -svk --max-time 8 --resolve echo.test:443:{args.echo_ip} https://echo.test:443/echo"),
        ("CaseB_https_deny_httpbin", args.deny_ip, "curl -svI --max-time 5 https://httpbin.org/get || true"),
        ("CaseB_ip_bypass_rustfs", args.deny_ip, f"curl -sv --max-time 4 http://{args.rustfs_ip}:9000 || true"),
        ("CaseB_ip_bypass_1111", args.deny_ip, "curl -sv --max-time 4 http://1.1.1.1 || true"),
        ("CaseB_sibling_isolation", args.deny_ip, f"curl -sv --max-time 4 http://{args.sibling_ip}:49983/health || true"),
        ("CaseA_sibling_isolation", args.allow_ip, f"curl -sv --max-time 4 http://{args.sibling_ip}:49983/health || true"),
        ("CaseA_dns_httpbin", args.allow_ip, "nslookup httpbin.org"),
        ("CaseA_https_httpbin", args.allow_ip, "curl -sI --max-time 8 https://httpbin.org/get | head -n 5 || true"),
    ]

    for label, ip, cmd in tests:
        print(f"\n=== {label} ===")
        rc, out, err = envd_exec(ip, args.token, cmd)
        print(f"exitCode={rc}")
        if out:
            print("STDOUT:\n" + out.strip())
        if err:
            print("STDERR:\n" + err.strip())


if __name__ == "__main__":
    main()
