#!/usr/bin/env python3
"""Verify OpenSandbox server -> ingress-gateway -> fastlet-proxy -> envd streaming, HTTP/2, stdin, and /files."""

import argparse
import base64
import http.client
import json
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def api_req(base_url: str, path: str, method: str = "GET", body_obj=None, headers: dict | None = None, raw_bytes: bytes | None = None):
    hdrs = dict(headers or {})
    data = raw_bytes
    if body_obj is not None:
        data = json.dumps(body_obj).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server-url", required=True)
    p.add_argument("--api-key", required=True)
    p.add_argument("--sandbox-id", required=True)
    p.add_argument("--token", default="p12-test-token")
    p.add_argument("--skip-delete", action="store_true")
    args = p.parse_args()

    st_ep, _, b_ep = api_req(
        args.server_url,
        f"/v1/sandboxes/{args.sandbox_id}/endpoints/49983",
        "GET",
        headers={"OPEN-SANDBOX-API-KEY": args.api_key},
    )
    print("GET /endpoints/49983 ->", st_ep, b_ep.decode("utf-8", errors="replace"))
    ep_obj = json.loads(b_ep.decode())
    ingress_host = ep_obj["endpoint"]
    ingress_hdr = ep_obj["headers"]
    route_key = list(ingress_hdr.keys())[0]
    route_val = ingress_hdr[route_key]
    ingress_url = f"http://{ingress_host}"

    st_h, _, _ = api_req(ingress_url, "/health", "GET", headers={route_key: route_val})
    st_i, hdr_i, _ = api_req(ingress_url, "/init", "POST", {"accessToken": args.token}, {route_key: route_val})
    st_m0, _, _ = api_req(ingress_url, "/metrics", "GET", headers={route_key: route_val})
    st_m1, _, _ = api_req(ingress_url, "/metrics", "GET", headers={route_key: route_val, "X-Access-Token": args.token})
    print(f"via ingress: GET /health={st_h} | POST /init={st_i} X-Envd-Version={hdr_i.get('X-Envd-Version') or hdr_i.get('x-envd-version')} | unauth /metrics={st_m0} | auth /metrics={st_m1}")

    host, port = ingress_host.split(":")

    def stream_start(cmd: str, extra_headers: dict | None = None, disconnect_after_start: bool = False):
        conn = http.client.HTTPConnection(host, int(port), timeout=20)
        payload = json.dumps({"process": {"cmd": "/bin/sh", "args": ["-c", cmd]}}).encode()
        frame = b"\x00" + struct.pack(">I", len(payload)) + payload
        hdrs = {
            route_key: route_val,
            "Content-Type": "application/connect+json",
            "Connect-Protocol-Version": "1",
            "X-Access-Token": args.token,
        }
        if extra_headers:
            hdrs.update(extra_headers)
        t0 = time.monotonic()
        conn.request("POST", "/process.Process/Start", body=frame, headers=hdrs)
        resp = conn.getresponse()
        resp_hdrs = dict(resp.getheaders())
        frames = []
        pid_found = None
        while True:
            hdr = resp.read(5)
            if not hdr or len(hdr) < 5:
                break
            flags = hdr[0]
            length = struct.unpack(">I", hdr[1:5])[0]
            body = b""
            while len(body) < length:
                chunk = resp.read(length - len(body))
                if not chunk:
                    break
                body += chunk
            dt = time.monotonic() - t0
            msg = json.loads(body.decode("utf-8", errors="replace"))
            ev = msg.get("event", {})
            if "start" in ev:
                pid_found = ev["start"].get("pid")
            frames.append((round(dt, 3), flags, msg))
            if disconnect_after_start and pid_found is not None:
                conn.close()
                return resp.status, resp_hdrs, frames, pid_found
        conn.close()
        return resp.status, resp_hdrs, frames, pid_found

    print("\n=== Connect server-streaming frame-by-frame via ingress (HTTP/1.1) ===")
    st_s, hdr_s, frames_s, _ = stream_start("for i in 1 2 3 4 5; do echo tick-$i; sleep 1; done")
    print("Start HTTP status:", st_s, "headers:", {k: v for k, v in hdr_s.items() if k.lower() in ("content-type", "transfer-encoding", "x-powered-by")})
    for dt, fl, msg in frames_s:
        ev = msg.get("event", {})
        if "data" in ev and "stdout" in ev["data"]:
            txt = base64.b64decode(ev["data"]["stdout"]).decode().strip()
            print(f"  t={dt:.3f}s flags={fl} stdout={txt!r}")
        else:
            print(f"  t={dt:.3f}s flags={fl} msg={msg}")

    print("\n=== HTTP/2 (h2c prior-knowledge and Upgrade) against ingress ===")
    for label, cmd_args in [
        ("curl --http2-prior-knowledge GET /health", ["curl", "-sv", "--http2-prior-knowledge", "-m", "5", "-H", f"{route_key}: {route_val}", f"{ingress_url}/health"]),
        ("curl --http2 (Upgrade: h2c) GET /health", ["curl", "-sv", "--http2", "-m", "5", "-H", f"{route_key}: {route_val}", f"{ingress_url}/health"]),
    ]:
        proc = subprocess.run(cmd_args, capture_output=True, text=True)
        print(f"--- {label} (rc={proc.returncode}) ---")
        for line in proc.stderr.splitlines():
            if any(k in line for k in ("* ", "< ", "> ")):
                print(" ", line)

    print("\n=== Non-zero exit, stderr, Connect-Timeout-Ms, mid-stream disconnect ===")
    _, _, frames_err, _ = stream_start("echo out1; echo err1 >&2; exit 42")
    print("non-zero exit frames:", [(dt, msg) for dt, _, msg in frames_err])

    _, _, frames_to, _ = stream_start("sleep 10", extra_headers={"Connect-Timeout-Ms": "1500"})
    print("Connect-Timeout-Ms=1500 frames:", [(dt, msg) for dt, _, msg in frames_to])

    _, _, _, dc_pid = stream_start("sleep 30", disconnect_after_start=True)
    print(f"mid-stream disconnect: got start pid={dc_pid}, closed TCP socket")
    time.sleep(1.5)
    _, _, frames_ps, _ = stream_start(f"ps -o pid,stat,args -p {dc_pid} || echo GONE")
    for _, _, msg in frames_ps:
        ev = msg.get("event", {})
        if "data" in ev and "stdout" in ev["data"]:
            print("  after disconnect ps:", base64.b64decode(ev["data"]["stdout"]).decode().strip())

    print("\n=== SendInput + CloseStdin ===")
    conn = http.client.HTTPConnection(host, int(port), timeout=15)
    payload = json.dumps({"process": {"cmd": "cat", "args": []}}).encode()
    frame = b"\x00" + struct.pack(">I", len(payload)) + payload
    conn.request("POST", "/process.Process/Start", body=frame, headers={
        route_key: route_val,
        "Content-Type": "application/connect+json",
        "Connect-Protocol-Version": "1",
        "X-Access-Token": args.token,
    })
    resp = conn.getresponse()
    hdr = resp.read(5)
    ln = struct.unpack(">I", hdr[1:5])[0]
    start_msg = json.loads(resp.read(ln).decode())
    cat_pid = start_msg["event"]["start"]["pid"]
    st_si, _, b_si = api_req(ingress_url, "/process.Process/SendInput", "POST", {"process": {"pid": cat_pid}, "input": {"stdin": base64.b64encode(b"hello-via-ingress\n").decode()}}, {route_key: route_val, "X-Access-Token": args.token})
    st_cs, _, b_cs = api_req(ingress_url, "/process.Process/CloseStdin", "POST", {"process": {"pid": cat_pid}}, {route_key: route_val, "X-Access-Token": args.token})
    print("SendInput ->", st_si, b_si.decode(), "| CloseStdin ->", st_cs, b_cs.decode())
    cat_frames = []
    while True:
        h = resp.read(5)
        if not h or len(h) < 5:
            break
        ln = struct.unpack(">I", h[1:5])[0]
        cat_frames.append(json.loads(resp.read(ln).decode()))
    conn.close()
    print("cat frames after stdin:", cat_frames)

    print("\n=== /files byte-for-byte via ingress ===")
    def upload(path: str, content: bytes):
        bd = "----p12bd"
        body = (f"--{bd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{bd}--\r\n".encode()
        return api_req(ingress_url, f"/files?path={urllib.parse.quote(path)}", "POST", headers={route_key: route_val, "X-Access-Token": args.token, "Content-Type": f"multipart/form-data; boundary={bd}"}, raw_bytes=body)

    for label, pth, data in [
        ("utf8", "/root/utf8.txt", "你好 OpenSandbox\n".encode("utf-8")),
        ("bin256", "/root/bin256.bin", bytes(range(256))),
    ]:
        su, _, _ = upload(pth, data)
        sd, _, got = api_req(ingress_url, f"/files?path={urllib.parse.quote(pth)}", "GET", headers={route_key: route_val, "X-Access-Token": args.token})
        print(f"file {label}: upload={su} download={sd} exact_match={got == data} len={len(got)}")

    upload("/root/overwrite.txt", b"A" * 200)
    upload("/root/overwrite.txt", b"short")
    sd_ow, _, got_ow = api_req(ingress_url, "/files?path=/root/overwrite.txt", "GET", headers={route_key: route_val, "X-Access-Token": args.token})
    print(f"file long->short overwrite: download={sd_ow} exact_match={got_ow == b'short'} got={got_ow!r}")

    sd_404, _, b_404 = api_req(ingress_url, "/files?path=/root/no-such-file", "GET", headers={route_key: route_val, "X-Access-Token": args.token})
    print(f"file missing 404: status={sd_404} body={b_404.decode('utf-8', errors='replace').strip()}")

    if not args.skip_delete:
        st_del, _, b_del = api_req(args.server_url, f"/v1/sandboxes/{args.sandbox_id}", "DELETE", headers={"OPEN-SANDBOX-API-KEY": args.api_key})
        print(f"DELETE /v1/sandboxes/{args.sandbox_id} -> {st_del} {b_del.decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    main()
