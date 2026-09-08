"""Deploy the HTTP E2E fixtures into an explicitly selected disposable cluster.

Images must be built and loaded first. This does not modify the user's current
kube context or deploy the central Sandbox Proxy. See docs/components/ingress.md.
"""

import argparse
import base64
from pathlib import Path
import secrets
import re
import subprocess
import json
import time

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--fast-sandbox", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-image", default="opensandbox/fleets-runtime:http-local")
    parser.add_argument("--proxy-image", default="opensandbox/fleets-proxy:http-local")
    parser.add_argument("--server-image", default="opensandbox/server:fleets-http-current")
    parser.add_argument(
        "--workload-image", default="docker.io/opensandbox/fleets-workload:http-local"
    )
    parser.add_argument("--egress-image", default="opensandbox/fleets-egress:http-local")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--kubeconfig", args.kubeconfig]
    ns = "fast-sandbox-system"
    docs = list(
        yaml.safe_load_all(
            subprocess.check_output(
                ["kubectl", "kustomize", str(args.fast_sandbox / "config/all-in-one")],
                text=True,
            )
        )
    )
    docs = [d for d in docs if d and d["metadata"]["name"] != "fast-sandbox-proxy"]
    for doc in docs:
        if doc["kind"] not in ("Deployment", "DaemonSet"):
            continue
        for c in doc["spec"]["template"]["spec"]["containers"]:
            c["image"] = args.runtime_image
            if c["name"] == "manager":
                c["args"].append("--fastlet-proxy-image=" + args.proxy_image)
                c["args"].append("--route-credential-ttl=30s")

    def resource(kind, name, **kwargs):
        return {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {"name": name, "namespace": ns},
            **kwargs,
        }

    signing = base64.b64encode(secrets.token_bytes(32)).decode()
    api_key = secrets.token_hex(16)
    previous = args.output / "deployment.yaml"
    if previous.exists():
        for doc in yaml.safe_load_all(previous.read_text()):
            if doc and doc["metadata"]["name"] == "http-server-config":
                old_config = doc["stringData"]["config.toml"]
                signing = re.search(r'^key = "([^"]+)"', old_config, re.M).group(1)
                api_key = (args.output / "api-key").read_text().strip()
    config = f'''[server]
host = "0.0.0.0"
port = 8080
api_key = "{api_key}"
[runtime]
type = "fleets"
execd_image = "{args.workload_image}"
[fleets]
namespace = "{ns}"
fastpath_endpoint = "fast-sandbox-fastpath.{ns}.svc:9090"
default_pool_ref = "http-basic"
[store]
type = "sqlite"
path = "/tmp/http-e2e.db"
[ingress]
mode = "gateway"
[ingress.gateway]
address = "127.0.0.1:28890"
[ingress.gateway.route]
mode = "header"
[ingress.secure_access]
active_key = "k"
[[ingress.secure_access.keys]]
key_id = "k"
key = "{signing}"
'''
    # Local test credentials are kept out of source control and console output.
    (args.output / "api-key").write_text(api_key)
    (args.output / "api-key").chmod(0o600)
    docs.append(resource("Secret", "http-server-config", stringData={"config.toml": config}))
    docs.append(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "http-server", "namespace": ns},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "http-server"}},
                "template": {
                    "metadata": {"labels": {"app": "http-server"}},
                    "spec": {
                        "serviceAccountName": "fast-sandbox-controller",
                        "containers": [
                            {
                                "name": "server",
                                "image": args.server_image,
                                "imagePullPolicy": "IfNotPresent",
                                "ports": [{"containerPort": 8080}],
                                "readinessProbe": {"tcpSocket": {"port": 8080}, "periodSeconds": 1},
                                "volumeMounts": [
                                    {"name": "config", "mountPath": "/etc/opensandbox"}
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "config",
                                "secret": {"secretName": "http-server-config"},
                            }
                        ],
                    },
                },
            },
        }
    )
    docs.append(
        resource(
            "Pod",
            "http-ingress",
            spec={
                "serviceAccountName": "fast-sandbox-controller",
                "containers": [
                    {
                        "name": "ingress",
                        "image": args.runtime_image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/workspace/ingress"],
                        "readinessProbe": {"tcpSocket": {"port": 28888}, "periodSeconds": 1},
                        "args": [
                            "--provider-type=fleets",
                            "--port=28888",
                            "--fastpath-endpoint=fast-sandbox-fastpath:9090",
                            "--fastpath-access-mode=direct-fastlet-proxy",
                            "--secure-access-keys=k=" + signing,
                        ],
                    }
                ],
            },
        )
    )
    # containerd copies the Fastlet resolver into each slot; use the gateway DNS
    # path that Firecracker templates bake in, while egress itself keeps Pod DNS.
    docs.append(
        resource(
            "ConfigMap",
            "http-egress-dns",
            data={
                "resolv.conf": "nameserver 172.30.0.1\noptions ndots:1\n",
            },
        )
    )
    for name, egress in (("http-basic", False), ("http-egress", True)):
        spec = {
            "runtime": "container",
            "sandboxResources": {"cpu": "250m", "memory": "256Mi", "pids": 128},
            "capacity": {
                "poolMin": 1 if egress else 2,
                "poolMax": 1 if egress else 2,
                "bufferMin": 0,
                "bufferMax": 0,
            },
            "maxSandboxesPerPod": 5,
            "warmImages": [args.workload_image],
            "fastletTemplate": {
                "spec": {
                    "containers": [
                        {
                            "name": "fastlet",
                            "image": args.runtime_image,
                            "imagePullPolicy": "IfNotPresent",
                        }
                    ]
                }
            },
        }
        if egress:
            spec["fastletTemplate"]["spec"]["volumes"] = [
                {
                    "name": "sandbox-dns",
                    "configMap": {"name": "http-egress-dns"},
                }
            ]
            spec["fastletTemplate"]["spec"]["containers"][0]["volumeMounts"] = [
                {
                    "name": "sandbox-dns",
                    "mountPath": "/etc/resolv.conf",
                    "subPath": "resolv.conf",
                    "readOnly": True,
                }
            ]
            spec["fastletTemplate"]["spec"]["containers"].append(
                {
                    "name": "egress",
                    "image": args.egress_image,
                    "imagePullPolicy": "IfNotPresent",
                    "securityContext": {"capabilities": {"add": ["NET_ADMIN", "NET_RAW"]}},
                    "env": [
                        {"name": "OPENSANDBOX_EGRESS_PROFILE", "value": "fleet"},
                        {"name": "OPENSANDBOX_EGRESS_MODE", "value": "dns+nft"},
                    ],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "64Mi"},
                        "limits": {"cpu": "500m", "memory": "256Mi"},
                    },
                }
            )
            spec["actionHandlers"] = [
                {
                    "name": "egress",
                    "targetHTTPPort": 18080,
                    "hooks": ["sandbox.runtime-ready", "sandbox.data-plane-ready"],
                }
            ]
        docs.append(
            {
                "apiVersion": "sandbox.fast.io/v1alpha2",
                "kind": "SandboxPool",
                "metadata": {"name": name, "namespace": ns},
                "spec": spec,
            }
        )
    rendered = yaml.safe_dump_all(docs, sort_keys=False)
    (args.output / "deployment.yaml").write_text(rendered)
    (args.output / "deployment.yaml").chmod(0o600)
    crds = [d for d in docs if d["kind"] == "CustomResourceDefinition"]
    subprocess.run(
        kube + ["apply", "-f", "-"],
        input=yaml.safe_dump_all(crds),
        text=True,
        check=True,
    )
    subprocess.run(
        kube
        + ["wait", "--for=condition=Established", "--timeout=60s"]
        + ["crd/" + d["metadata"]["name"] for d in crds],
        check=True,
    )
    subprocess.run(kube + ["apply", "-f", "-"], input=rendered, text=True, check=True)
    subprocess.run(
        kube
        + ["-n", ns, "rollout", "status", "deployment/fast-sandbox-controller", "--timeout=180s"],
        check=True,
    )
    subprocess.run(
        kube + ["-n", ns, "rollout", "status", "deployment/http-server", "--timeout=180s"],
        check=True,
    )
    subprocess.run(
        kube + ["-n", ns, "wait", "--for=condition=Ready", "pod/http-ingress", "--timeout=180s"],
        check=True,
    )
    for name, replicas in (("http-basic", 2), ("http-egress", 1)):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            pool = json.loads(
                subprocess.check_output(
                    kube + ["-n", ns, "get", "sandboxpool", name, "-o", "json"],
                    text=True,
                )
            )
            status = pool.get("status", {})
            if status.get("preparedFastlets", 0) >= replicas and any(
                image.get("cachedFastlets", 0) >= replicas
                and image.get("observedGeneration") == pool["metadata"]["generation"]
                for image in status.get("warmImages", [])
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"Pool {name} did not become warm: {status}")


if __name__ == "__main__":
    main()
