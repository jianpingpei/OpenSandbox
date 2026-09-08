"""Build real binaries/images for the disposable Kind HTTP acceptance suite."""

import argparse
import os
from pathlib import Path
import subprocess


def run(*command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast-sandbox", type=Path, required=True)
    parser.add_argument(
        "--egress-source",
        type=Path,
        required=True,
        help="OpenSandbox checkout with Fleet Actions support; contains components/",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cluster", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    (output / "bin").mkdir(parents=True, exist_ok=True)
    arch = subprocess.check_output(
        ["docker", "info", "--format", "{{.Architecture}}"], text=True
    ).strip()
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(arch, arch)
    env = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": arch}
    for name in (
        "controller",
        "fastlet",
        "sandbox-init",
        "sandbox-tunnel",
        "fastlet-proxy",
        "janitor",
    ):
        run(
            "go",
            "build",
            "-o",
            str(output / "bin" / name),
            "./cmd/" + name,
            cwd=args.fast_sandbox,
            env=env,
        )
    for name, source in (
        ("ingress", repo),
        ("execd", repo),
        ("egress", args.egress_source),
    ):
        run(
            "go",
            "build",
            "-o",
            str(output / "bin" / name),
            ".",
            cwd=source / "components" / name,
            env=env,
        )
    images = []
    for target in ("runtime", "proxy", "workload", "egress"):
        image = f"opensandbox/fleets-{target}:http-local"
        run(
            "docker",
            "build",
            "--target",
            target,
            "-f",
            str(repo / "tests/fleets/Dockerfile"),
            "-t",
            image,
            str(output),
        )
        images.append(image)
    server_image = "opensandbox/server:fleets-http-current"
    run("docker", "build", "-t", server_image, str(repo / "server"))
    run("kind", "load", "docker-image", "--name", args.cluster, *images, server_image)


if __name__ == "__main__":
    main()
