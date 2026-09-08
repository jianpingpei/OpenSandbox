"""Real HTTP acceptance tests; no SDK, fake FastPath, or fake Kubernetes API.

Run against deploy.py's disposable environment. Every created sandbox is tracked
and deleted in finally; infrastructure is left available for failure diagnosis.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import time
import uuid

import httpx


class RouteNotReady(RuntimeError):
    """A replaying Action Handler temporarily prevents endpoint resolution."""


def wait(description, probe, timeout=90):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except (httpx.TransportError, RouteNotReady) as exc:
            last = type(exc).__name__
        time.sleep(0.5)
    raise AssertionError(f"Timed out: {description}; last={last}")


class Suite:
    def __init__(self, args):
        self.args = args
        self.client = httpx.Client(timeout=40, trust_env=False)
        self.headers = {"OPEN-SANDBOX-API-KEY": args.api_key_file.read_text().strip()}
        self.created = []
        self.runtime_uids = set()
        self.nodes = set()
        self.run_id = uuid.uuid4().hex[:10]
        self.results = []

    def api(self, method, path, **kwargs):
        return self.client.request(
            method, self.args.server + "/v1" + path, headers=self.headers, **kwargs
        )

    def kube(self, *args):
        return subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                self.args.kubeconfig,
                "--request-timeout=20s",
                "-n",
                "fast-sandbox-system",
                *args,
            ],
            text=True,
        )

    def cr(self, sandbox_id):
        return json.loads(self.kube("get", "sandbox", sandbox_id, "-o", "json"))

    def record(self, name):
        self.results.append(name)
        print("PASS " + name, flush=True)

    def create(self, pool="http-basic", policy=None):
        payload = {
            "image": {"uri": self.args.image},
            "timeout": 600,
            "entrypoint": ["/execd"],
            "env": {"HTTP_E2E_RUN": self.run_id},
            "metadata": {"e2e-run": self.run_id},
            "extensions": {"poolRef": pool},
        }
        if policy is not None:
            payload["networkPolicy"] = policy
        response = self.api("POST", "/sandboxes", json=payload)
        assert response.status_code == 202, response.text
        sandbox_id = response.json()["id"]
        self.created.append(sandbox_id)
        assert sandbox_id.startswith("flt-")
        endpoint = self.api("GET", f"/sandboxes/{sandbox_id}/endpoints/44772")
        assert endpoint.status_code == 200, endpoint.text
        route = endpoint.json()
        wait(
            "execd through Ingress",
            lambda: self.data(route, "GET", "/ping").status_code == 200,
        )
        cr = self.cr(sandbox_id)
        self.runtime_uids.add(cr["metadata"]["uid"])
        pod = json.loads(
            self.kube("get", "pod", cr["status"]["placement"]["fastletName"], "-o", "json")
        )
        self.nodes.add(pod["spec"]["nodeName"])
        return sandbox_id, route

    def data(self, route, method, path, **kwargs):
        headers = {**route.get("headers", {}), **kwargs.pop("headers", {})}
        return self.client.request(
            method,
            "http://" + route["endpoint"].rstrip("/") + path,
            headers=headers,
            **kwargs,
        )

    def command(self, route, command, error=False):
        response = self.data(route, "POST", "/command", json={"command": command})
        if response.status_code == 503:
            raise RouteNotReady(response.text)
        assert response.status_code == 200, response.text
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        types = [event.get("type") for event in events]
        assert ("error" if error else "execution_complete") in types, response.text
        if error:
            assert "execution_complete" not in types, response.text
        else:
            assert "error" not in types, response.text
        return "".join(event.get("text", "") for event in events if event.get("type") == "stdout")

    def basic(self):
        first, route = self.create()
        self.record("HTTP create -> endpoint -> Ingress -> Fastlet Proxy -> execd /ping")
        malformed = self.data(
            route,
            "POST",
            "/command",
            content="{not-json",
            headers={"Content-Type": "application/json"},
        )
        assert malformed.status_code == 400, malformed.text
        for command, marker in (
            ("echo probe-ok", "probe-ok"),
            ("printf 'a b c\\n' | wc -w", "3"),
            ("sleep 1 && echo done-after-sleep", "done-after-sleep"),
            ("printf '%s' \"$HTTP_E2E_RUN\"", self.run_id),
        ):
            assert marker in self.command(route, command)
        self.command(route, "/bin/false", error=True)
        self.command(route, "definitely-not-a-real-binary-xyz", error=True)
        self.record("execd battery: malformed JSON, echo, pipe, sleep, env, false, missing binary")

        body = ("file-" + self.run_id).encode()
        uploaded = self.data(
            route,
            "POST",
            "/files/upload",
            files=[
                (
                    "metadata",
                    (
                        "metadata.json",
                        json.dumps({"path": "/tmp/http-e2e.txt", "mode": 420}),
                        "application/json",
                    ),
                ),
                ("file", ("http-e2e.txt", body, "application/octet-stream")),
            ],
        )
        assert uploaded.status_code == 200, uploaded.text
        assert (
            self.data(route, "GET", "/files/download", params={"path": "/tmp/http-e2e.txt"}).content
            == body
        )
        self.record("multipart upload and binary download through Ingress")

        # Independent writable layers and routing on both warm Fastlet Pods.
        second = self.create()
        with ThreadPoolExecutor(max_workers=5) as executor:
            others = [second, *executor.map(lambda _: self.create(), range(5))]
        assert (
            len(
                {
                    self.cr(sid)["status"]["placement"]["fastletName"]
                    for sid, _ in [(first, route), *others]
                }
            )
            >= 2
        )
        for sid, other in others:
            assert "isolated" in self.command(other, "test ! -e /tmp/http-e2e.txt && echo isolated")
            assert sid != first
        self.record("seven live sandboxes across two Fastlets; isolated files and routes")

        response = self.api("PATCH", f"/sandboxes/{first}/metadata", json={"checked": "yes"})
        assert response.status_code == 200, response.text
        wait(
            "metadata CR/cache convergence",
            lambda: self.api("GET", f"/sandboxes/{first}").json().get("metadata", {}).get("checked")
            == "yes",
        )
        expiration = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        response = self.api(
            "POST",
            f"/sandboxes/{first}/renew-expiration",
            json={"expiresAt": expiration},
        )
        assert response.status_code == 200, response.text
        wait(
            "renew CR observed",
            lambda: datetime.fromisoformat(
                self.cr(first)["spec"]["expireTime"].replace("Z", "+00:00")
            )
            >= datetime.fromisoformat(expiration).replace(microsecond=0),
        )
        pages = [
            self.api(
                "GET",
                "/sandboxes",
                params={
                    "metadata": "e2e-run=" + self.run_id,
                    "pageSize": 2,
                    "page": page,
                },
            ).json()
            for page in (1, 2, 3, 4)
        ]
        assert all(p["pagination"]["totalItems"] == 7 for p in pages), pages
        assert len({item["id"] for page in pages for item in page["items"]}) == 7
        self.record("metadata, renewal, globally consistent list pagination")

        forged = dict(route["headers"])
        scope = forged["OpenSandbox-Ingress-To"].split(".")
        scope[3] = "8081"  # Change signed port, not ambiguous base64 tail bits.
        forged["OpenSandbox-Ingress-To"] = ".".join(scope)
        rejected = self.data(route, "GET", "/ping", headers=forged)
        assert rejected.status_code == 401, rejected.text
        self.record("signed route tampering rejected")
        # Keep a cached route alive across its refresh boundary.
        before = time.monotonic()
        while time.monotonic() - before < 65:
            assert self.data(route, "GET", "/ping").status_code == 200
            time.sleep(2)
        self.record("continued access across credential/cache refresh window")

    def egress(self):
        deny = {"defaultAction": "deny", "egress": []}
        allow = {
            "defaultAction": "deny",
            "egress": [
                {"action": "allow", "target": "example.com"},
                {"action": "allow", "target": "1.1.1.1"},
            ],
        }
        a, ra = self.create("http-egress", deny)
        b, rb = self.create("http-egress", allow)
        pod = self.cr(a)["status"]["placement"]["fastletName"]
        assert pod == self.cr(b)["status"]["placement"]["fastletName"]
        status = json.loads(
            self.kube(
                "exec",
                pod,
                "-c",
                "egress",
                "--",
                "curl",
                "-fsS",
                "http://127.0.0.1:18080/_fastlet/v1/actions/status",
            )
        )
        assert status["ready"] and status["instanceId"]
        self.record("real Actions handler ready; deny/allow sandboxes share one Fastlet")

        def reachable(route, url="http://example.com"):
            return "EGRESS_ALLOWED" in self.command(
                route,
                f"curl -fsS --max-time 4 {url} >/dev/null && echo EGRESS_ALLOWED || echo EGRESS_BLOCKED",
            )

        wait("allow control has live DNS/network", lambda: reachable(rb), timeout=120)
        wait(
            "allow control has IP-direct access",
            lambda: reachable(rb, "http://1.1.1.1"),
        )
        assert not reachable(ra) and not reachable(ra, "http://1.1.1.1")
        self.record("domain/IP allowlist and deny isolation with working allow controls")
        for sid, policy, target, expected in (
            (a, allow, ra, True),
            (b, deny, rb, False),
        ):
            response = self.api("PUT", f"/sandboxes/{sid}/networkpolicy", json=policy)
            assert response.status_code == 200, response.text
            wait(
                "committed policy visible",
                lambda: self.api("GET", f"/sandboxes/{sid}/networkpolicy").json().get("policy")
                == policy,
            )
            wait("policy enforced", lambda: reachable(target) == expected, timeout=90)
        assert reachable(ra) and not reachable(rb)
        assert reachable(ra, "http://1.1.1.1") and not reachable(rb, "http://1.1.1.1")
        self.record("HTTP policy replacement -> FastPath Update -> per-sandbox enforcement")

        try:
            self.kube("exec", pod, "-c", "egress", "--", "sh", "-c", "kill -TERM 1")
        except subprocess.CalledProcessError:
            # Killing PID 1 may close the exec stream before its exit status.
            # Success is established below by a new instanceId and policy replay.
            pass

        def restarted():
            result = subprocess.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    self.args.kubeconfig,
                    "-n",
                    "fast-sandbox-system",
                    "exec",
                    pod,
                    "-c",
                    "egress",
                    "--",
                    "curl",
                    "-fsS",
                    "http://127.0.0.1:18080/_fastlet/v1/actions/status",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                return False
            current = json.loads(result.stdout)
            return current["ready"] and current["instanceId"] != status["instanceId"]

        wait("egress handler restart", restarted)
        wait("allow policy replayed", lambda: reachable(ra), timeout=120)
        assert not reachable(rb)
        self.record("handler restart replays both policies without crossing sandbox identity")

        def chain(sid):
            return "subj_s_" + self.cr(sid)["metadata"]["uid"].replace("-", "_")

        chain_a, chain_b = chain(a), chain(b)

        def nft():
            return self.kube(
                "exec",
                pod,
                "-c",
                "egress",
                "--",
                "nft",
                "list",
                "table",
                "inet",
                "opensandbox-fleet",
            )

        assert chain_a in nft() and chain_b in nft()
        assert self.api("DELETE", f"/sandboxes/{a}").status_code == 204
        wait("A policy cleanup", lambda: chain_a not in nft())
        assert chain_b in nft()
        assert "B-alive" in self.command(rb, "echo B-alive")
        assert not reachable(rb)
        self.record("delete A removes only A's nft rules; B stays alive and isolated")
        assert self.api("DELETE", f"/sandboxes/{b}").status_code == 204
        wait("B policy cleanup", lambda: chain_b not in nft())
        self.record("delete B removes its remaining nft rules")

    def cleanup(self):
        failures = []
        for sid in self.created:
            try:
                response = self.api("DELETE", f"/sandboxes/{sid}")
                assert response.status_code in (204, 404), response.text
                wait(
                    "deleted Sandbox CR " + sid,
                    lambda: self.api("GET", f"/sandboxes/{sid}").status_code == 404,
                )
            except Exception as exc:
                failures.append(f"{sid}: {exc}")
        self.client.close()
        assert not failures, failures
        self.record("HTTP deletion converged to NotFound for every created sandbox")
        for node in self.nodes:

            def tasks_clean():
                tasks = subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        node,
                        "ctr",
                        "-n",
                        "k8s.io",
                        "tasks",
                        "list",
                        "-q",
                    ],
                    text=True,
                )
                return all(uid not in tasks for uid in self.runtime_uids)

            wait("containerd tasks removed", tasks_clean)
        self.record("created sandbox containerd tasks are gone")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--server", default="http://127.0.0.1:28080")
    parser.add_argument("--image", default="docker.io/opensandbox/fleets-workload:http-local")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", choices=("basic", "egress", "all"), default="all")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for target, ports in (
            ("deployment/http-server", "28080:8080"),
            ("pod/http-ingress", "28890:28888"),
        ):
            log = stack.enter_context((args.output / (target.replace("/", "-") + ".log")).open("w"))
            proc = subprocess.Popen(
                [
                    "kubectl",
                    "--kubeconfig",
                    args.kubeconfig,
                    "-n",
                    "fast-sandbox-system",
                    "port-forward",
                    "--address=127.0.0.1",
                    target,
                    ports,
                ],
                stdout=log,
                stderr=log,
            )

            def stop(process=proc):
                process.terminate()
                process.wait(timeout=10)

            stack.callback(stop)
        suite = Suite(args)
        try:
            wait(
                "server HTTP available",
                lambda: suite.api("GET", "/sandboxes").status_code == 200,
            )
            if args.suite in ("basic", "all"):
                suite.basic()
            if args.suite in ("egress", "all"):
                suite.egress()
        finally:
            try:
                suite.cleanup()
            finally:
                (args.output / "results.json").write_text(json.dumps(suite.results, indent=2))
    print("RESULT=PASS (real container runtime; no Firecracker/P2P claim)", flush=True)


if __name__ == "__main__":
    main()
