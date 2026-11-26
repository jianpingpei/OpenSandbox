#!/usr/bin/env python3
"""Verify envd 0.8.0 /init contract, envVars precedence, default user/workdir, and sticky /tmp overwrite behavior."""

import argparse
import base64
import json
import os
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def start_envd(envd_bin: str, port: int):
    os.makedirs("/run/e2b", exist_ok=True)
    proc = subprocess.Popen(
        [envd_bin, "-isnotfc", "-no-cgroups", "-verbose", "-port", str(port)],
        stdout=open("/tmp/envd-probe.log", "wb"),
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as resp:
                if resp.status in (200, 204):
                    return proc
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("envd failed to become healthy")


def req(base: str, path: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None):
    r = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", errors="replace")


def basic_hdr(username: str | None) -> dict:
    if username is None:
        return {}
    encoded = base64.b64encode(f"{username}:".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def exec_cmd(base: str, token: str | None, cmd: str, username: str | None = None, envs: dict | None = None):
    proc = {"cmd": "/bin/sh", "args": ["-c", cmd]}
    if envs:
        proc["envs"] = envs
    payload = json.dumps({"process": proc}).encode()
    frame = b"\x00" + struct.pack(">I", len(payload)) + payload
    hdrs = {"Content-Type": "application/connect+json", "Connect-Protocol-Version": "1"}
    if token:
        hdrs["X-Access-Token"] = token
    hdrs.update(basic_hdr(username))
    r = urllib.request.Request(f"{base}/process.Process/Start", data=frame, method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, "", exc.read().decode("utf-8", errors="replace")
    pos = 0
    stdout, stderr = [], []
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
    return 200, "".join(stdout).strip(), "".join(stderr).strip()


def upload_file(base: str, token: str | None, path: str, content: bytes, username: str | None = None):
    boundary = "----envdprobe"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    query = f"/files?path={urllib.parse.quote(path)}"
    if username is not None:
        query += f"&username={urllib.parse.quote(username)}"
    hdrs = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        hdrs["X-Access-Token"] = token
    return req(base, query, method="POST", data=body, headers=hdrs)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--envd-bin", default="/usr/local/bin/envd")
    p.add_argument("--port", type=int, default=49983)
    args = p.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    print("=== PART 1: /init Matrix & envVars Precedence ===")
    proc = start_envd(args.envd_bin, args.port)
    st, hdrs, body = req(base, "/init", "POST", json.dumps({"accessToken": "tok-1", "envVars": {"FOO": "from-init", "BAR": "1"}}).encode(), {"Content-Type": "application/json"})
    print("1. First /init:", st, {k: v for k, v in hdrs.items() if k.lower().startswith("x-envd") or k.lower() == "cache-control"}, repr(body))

    st2, _, _ = req(base, "/init", "POST", json.dumps({"accessToken": "tok-1", "envVars": {"FOO": "from-init-v2", "BAZ": "2"}}).encode(), {"Content-Type": "application/json"})
    _, _, envs_after = req(base, "/envs", "GET", headers={"X-Access-Token": "tok-1"})
    print("2. Same-token re-/init (no timestamp):", st2, "GET /envs:", envs_after.strip())

    st_ts1, _, _ = req(base, "/init", "POST", json.dumps({"accessToken": "tok-1", "timestamp": "2030-01-01T00:00:00Z", "envVars": {"FOO": "ts-2030"}}).encode(), {"Content-Type": "application/json"})
    st_ts2, _, _ = req(base, "/init", "POST", json.dumps({"accessToken": "tok-1", "timestamp": "2020-01-01T00:00:00Z", "envVars": {"FOO": "ts-2020-stale"}}).encode(), {"Content-Type": "application/json"})
    _, _, envs_ts = req(base, "/envs", "GET", headers={"X-Access-Token": "tok-1"})
    print("3. Timestamp guard (2030 then stale 2020):", st_ts1, st_ts2, "GET /envs:", envs_ts.strip())

    st_diff, _, b_diff = req(base, "/init", "POST", json.dumps({"accessToken": "tok-2"}).encode(), {"Content-Type": "application/json"})
    st_none, _, b_none = req(base, "/init", "POST", json.dumps({}).encode(), {"Content-Type": "application/json"})
    st_m1, _, _ = req(base, "/metrics", "GET", headers={"X-Access-Token": "tok-1"})
    print("4. Different-token /init:", st_diff, b_diff.strip(), "| No-token /init:", st_none, b_none.strip(), "| tok-1 /metrics:", st_m1)

    rc, out_p, err_p = exec_cmd(base, "tok-1", "env | grep -E '^(FOO|BAR|BAZ)=' | sort", envs={"FOO": "from-start"})
    print("5. envVars precedence (Start envs overrides /init):", rc, repr(out_p), repr(err_p))

    proc.terminate()
    proc.wait(timeout=5)
    proc = start_envd(args.envd_bin, args.port)
    st_m_after_restart, _, _ = req(base, "/metrics", "GET")
    _, _, envs_after_restart = req(base, "/envs", "GET")
    print("6. After envd restart (no token): GET /metrics=", st_m_after_restart, "GET /envs=", envs_after_restart.strip())

    print("\n=== PART 2: Default User & Workdir (WITHOUT 'user' account) ===")
    req(base, "/init", "POST", json.dumps({"accessToken": "tok-1"}).encode(), {"Content-Type": "application/json"})
    for uname in [None, "root", "user"]:
        rc, out, err = exec_cmd(base, "tok-1", "id; pwd", username=uname)
        st_f, _, bf = upload_file(base, "tok-1", "rel_test.txt", b"hello\n", username=uname)
        print(f"No-'user'-acct Start(username={uname}): http={rc} out={out!r} err={err!r} | POST /files(rel_test.txt)={st_f} {bf.strip()}")

    proc.terminate()
    proc.wait(timeout=5)
    subprocess.run(["useradd", "-m", "-s", "/bin/bash", "user"], check=True)
    proc = start_envd(args.envd_bin, args.port)

    print("\n=== PART 3: Default User & Workdir (WITH 'user' account) ===")
    req(base, "/init", "POST", json.dumps({"accessToken": "tok-1"}).encode(), {"Content-Type": "application/json"})
    for uname in [None, "root", "user"]:
        rc, out, _ = exec_cmd(base, "tok-1", "id; pwd", username=uname)
        st_f, _, bf = upload_file(base, "tok-1", f"rel_{uname or 'none'}.txt", b"hello\n", username=uname)
        print(f"With-'user'-acct, no defaultUser (username={uname}): Start={rc} {out!r} | POST /files={st_f} {bf.strip()}")

    st_du, hdr_du, _ = req(base, "/init", "POST", json.dumps({"accessToken": "tok-1", "defaultUser": "user", "defaultWorkdir": "/home/user"}).encode(), {"Content-Type": "application/json"})
    print("Re-/init with defaultUser='user', defaultWorkdir='/home/user':", st_du, hdr_du.get("X-Envd-Defaults"))
    for uname in [None, "root", "user"]:
        rc, out, _ = exec_cmd(base, "tok-1", "id; pwd", username=uname)
        st_f, _, bf = upload_file(base, "tok-1", f"du_{uname or 'none'}.txt", b"hello\n", username=uname)
        print(f"With-'user'-acct, defaultUser='user' (username={uname}): Start={rc} {out!r} | POST /files={st_f} {bf.strip()}")

    print("\n=== PART 4: Sticky /tmp Overwrite 500 Analysis ===")
    print("fs.protected_regular =", open("/proc/sys/fs/protected_regular").read().strip())
    print("/tmp stat:", subprocess.check_output(["ls", "-ld", "/tmp"], text=True).strip())
    for target, uname in [("/tmp/user_rt.txt", "user"), ("/tmp/root_rt.txt", "root"), ("/home/user/home_rt.txt", "user")]:
        s1, _, _ = upload_file(base, "tok-1", target, b"first-write-long\n", username=uname)
        st_ls = subprocess.check_output(["ls", "-l", target], text=True).strip()
        s2, _, b2 = upload_file(base, "tok-1", target, b"second\n", username=uname)
        print(f"target={target} username={uname}: write1={s1} ({st_ls}) -> overwrite2={s2} {b2.strip()}")

    proc.terminate()
    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
