# pyright: reportAttributeAccessIssue=false
# protobuf-generated modules expose dynamic attributes.

# Copyright 2026 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HTTP-to-gRPC integration tests for the fleets runtime."""

from concurrent import futures
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import grpc
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from opensandbox_server.api import lifecycle
from opensandbox_server.api.schema import RenewSandboxExpirationRequest
from opensandbox_server.config import (
    AppConfig,
    FleetsRuntimeConfig,
    IngressConfig,
    RuntimeConfig,
    ServerConfig,
)
from opensandbox_server.services.fleets.fastpath_client import FastPathClient
from opensandbox_server.services.fleets.fleet_service import FleetSandboxService
from opensandbox_server.services.fleets.generated import fastpath_pb2 as pb2
from opensandbox_server.services.fleets.generated import fastpath_pb2_grpc as pb2_grpc
from opensandbox_server.services.snapshot_runtime_factory import create_snapshot_runtime
from opensandbox_server.tenants.context import get_current_tenant, set_current_tenant
from opensandbox_server.tenants.models import TenantEntry


class _FakeFastPathService(pb2_grpc.FastPathServiceServicer):
    def __init__(self):
        self.sandboxes: dict[tuple[str, str], tuple[str, int]] = {}
        self.last_create: pb2.CreateSandboxRequest | None = None
        self.last_get: pb2.GetSandboxRequest | None = None
        self.last_update: pb2.UpdateSandboxRequest | None = None
        self.last_delete: pb2.DeleteRequest | None = None
        self.abort_update_with: grpc.StatusCode | None = None
        self.abort_create_with: grpc.StatusCode | None = None
        self.reject_create_with: grpc.StatusCode | None = None
        self.create_pending = False
        self.create_time_remaining: float | None = None
        self.diagnostic_runtime_state = pb2.RUNTIME_STATE_READY
        self.pool_has_resources = True
        self.get_error_by_namespace: dict[str, grpc.StatusCode] = {}

    @staticmethod
    def _info(name: str, uid: str, namespace: str = "ns-1") -> pb2.SandboxInfo:
        return pb2.SandboxInfo(
            identity=pb2.SandboxIdentity(uid=uid, name=name, namespace=namespace),
            applied_generation=1,
            runtime=pb2.RuntimeInfo(state=pb2.RUNTIME_STATE_READY),
            data_plane=pb2.DataPlaneInfo(state=pb2.DATA_PLANE_STATE_READY),
            ready=True,
        )

    def CreateSandbox(self, request, context):
        self.last_create = request
        if self.reject_create_with is not None:
            context.abort(self.reject_create_with, "scripted rejection before persistence")
        self.create_time_remaining = context.time_remaining()
        uid = f"uid-{request.request_id}"
        self.sandboxes[(request.namespace, request.request_id)] = (uid, 1)
        if self.abort_create_with is not None:
            context.abort(self.abort_create_with, "scripted post-persistence failure")
        info = self._info(request.request_id, uid, request.namespace)
        if self.create_pending:
            info.runtime.state = pb2.RUNTIME_STATE_CREATING
            info.data_plane.state = pb2.DATA_PLANE_STATE_PENDING
            info.ready = False
        return pb2.CreateSandboxResponse(
            sandbox=info,
            generation=1,
            completion=request.completion,
        )

    def GetSandbox(self, request, context):
        self.last_get = request
        namespace = request.sandbox.namespaced_name.namespace
        name = request.sandbox.namespaced_name.name
        if error := self.get_error_by_namespace.get(namespace):
            context.abort(error, "scripted get failure")
        current = self.sandboxes.get((namespace, name))
        if current is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "not found")
        uid, generation = current
        if request.sandbox.expected_uid and request.sandbox.expected_uid != uid:
            context.abort(grpc.StatusCode.ABORTED, "uid fence rejected")
        if request.expected_generation and request.expected_generation != generation:
            context.abort(grpc.StatusCode.ABORTED, "generation fence rejected")
        return pb2.GetSandboxResponse(
            sandbox=self._info(name, uid, namespace), generation=generation
        )

    def UpdateSandbox(self, request, context):
        self.last_update = request
        if self.abort_update_with is not None:
            context.abort(self.abort_update_with, "scripted update failure")
        namespace = request.sandbox.namespaced_name.namespace
        name = request.sandbox.namespaced_name.name
        current = self.sandboxes.get((namespace, name))
        if current is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "not found")
        uid, generation = current
        if request.sandbox.expected_uid != uid or (
            request.expected_generation and request.expected_generation != generation
        ):
            context.abort(grpc.StatusCode.ABORTED, "fence rejected")
        self.sandboxes[(namespace, name)] = (uid, generation + 1)
        return pb2.UpdateSandboxResponse(
            sandbox=pb2.SandboxIdentity(uid=uid, name=name, namespace=namespace),
            committed_generation=generation + 1,
        )

    def DeleteSandbox(self, request, context):
        self.last_delete = request
        namespace = request.sandbox.namespaced_name.namespace
        name = request.sandbox.namespaced_name.name
        current = self.sandboxes.get((namespace, name))
        if current is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "not found")
        uid, _ = current
        if request.sandbox.expected_uid != uid:
            context.abort(grpc.StatusCode.ABORTED, "uid fence rejected")
        self.sandboxes.pop((namespace, name))
        return pb2.DeleteResponse()

    def GetSandboxDiagnostics(self, request, context):
        sandbox = self.sandboxes.get((request.namespace, request.sandbox_name))
        if sandbox is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "not found")
        uid, _ = sandbox
        info = self._info(request.sandbox_name, uid, request.namespace)
        info.runtime.state = self.diagnostic_runtime_state
        return pb2.SandboxDiagnosticsResponse(
            sandbox=info,
            assignment_state="assigned",
            events=[
                pb2.SandboxDiagnosticEvent(
                    timestamp_unix_nano=1,
                    level="Info",
                    source="fastlet",
                    phase="Ready",
                    message="sandbox ready",
                )
            ],
        )

    def GetPool(self, request, context):
        if not self.pool_has_resources:
            return pb2.PoolInfo(namespace=request.namespace, name=request.pool_name)
        return pb2.PoolInfo(
            namespace=request.namespace,
            name=request.pool_name,
            sandbox_cpu="500m",
            sandbox_memory="512Mi",
        )


@pytest.fixture
def http_fleets(monkeypatch):
    fake = _FakeFastPathService()
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb2_grpc.add_FastPathServiceServicer_to_server(fake, grpc_server)
    port = grpc_server.add_insecure_port("127.0.0.1:0")
    grpc_server.start()

    config = AppConfig(
        server=ServerConfig(
            host="0.0.0.0",
            port=8080,
            api_key="x",
            max_sandbox_timeout_seconds=7200,
        ),
        runtime=RuntimeConfig(type="fleets", execd_image="ghcr.io/opensandbox/execd:latest"),
        fleets=FleetsRuntimeConfig(namespace="ns-1"),
    )
    fastpath = FastPathClient(endpoint=f"127.0.0.1:{port}")
    service = FleetSandboxService(config, fastpath_client=fastpath)
    monkeypatch.setattr(lifecycle, "sandbox_service", service)

    app = FastAPI()
    app.include_router(lifecycle.router, prefix="/v1")
    try:
        with TestClient(app) as client:
            yield client, fake, service
    finally:
        fastpath.close()
        grpc_server.stop(None)


def test_http_create_calls_fastpath_with_ready_completion(http_fleets):
    client, fake, service = http_fleets
    response = client.post(
        "/v1/sandboxes",
        json={
            "image": {"uri": "python:3.11"},
            "entrypoint": ["python", "-m", "http.server"],
            "timeout": 3600,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
            "metadata": {"team": "agents"},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["id"].startswith("flt-")
    assert body["status"]["state"] == "Running"
    assert body["metadata"] == {"team": "agents"}
    assert fake.last_create.request_id == body["id"]
    assert fake.last_create.completion == pb2.CREATE_COMPLETION_READY
    assert fake.create_time_remaining > service._fleets.wait_ready_timeout_millis / 1000


def test_http_create_recovers_an_ambiguous_post_persistence_failure(http_fleets):
    client, fake, _ = http_fleets
    fake.abort_create_with = grpc.StatusCode.INTERNAL

    response = client.post(
        "/v1/sandboxes",
        json={
            "image": {"uri": "python:3.11"},
            "entrypoint": ["python"],
            "timeout": 3600,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
        },
    )

    assert response.status_code == 202
    sandbox_id = response.json()["id"]
    assert sandbox_id.startswith("flt-")
    assert fake.last_get is not None
    assert ("ns-1", sandbox_id) in fake.sandboxes


def test_http_create_rejects_timeout_pool_mismatch_and_unreadable_extension(http_fleets):
    client, fake, _ = http_fleets
    base = {
        "image": {"uri": "python:3.11"},
        "entrypoint": ["python"],
        "timeout": 3600,
        "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
    }

    too_long = client.post("/v1/sandboxes", json={**base, "timeout": 10**9})
    pool_mismatch = client.post(
        "/v1/sandboxes",
        json={**base, "resourceLimits": {"cpu": "1", "memory": "512Mi"}},
    )
    renew_extension = client.post(
        "/v1/sandboxes",
        json={**base, "extensions": {"access.renew.extend.seconds": "300"}},
    )
    fake.pool_has_resources = False
    empty_pool_profile = client.post("/v1/sandboxes", json=base)

    assert too_long.status_code == 400
    assert pool_mismatch.status_code == 400
    assert "resourceLimits" in pool_mismatch.json()["detail"]["message"]
    assert renew_extension.status_code == 400
    assert "access.renew.extend.seconds" in renew_extension.json()["detail"]["message"]
    assert empty_pool_profile.status_code == 400
    assert (
        "not defined by the selected SandboxPool" in empty_pool_profile.json()["detail"]["message"]
    )


def test_http_read_and_metadata_patch_report_new_fastpath_contract_gap(http_fleets):
    client, _, _ = http_fleets
    created = client.post(
        "/v1/sandboxes",
        json={
            "image": {"uri": "python:3.11"},
            "entrypoint": ["python", "-m", "http.server"],
            "timeout": 3600,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
            "metadata": {"team": "agents", "remove-me": "yes"},
        },
    ).json()
    sandbox_id = created["id"]

    responses = [
        client.get(f"/v1/sandboxes/{sandbox_id}"),
        client.patch(
            f"/v1/sandboxes/{sandbox_id}/metadata",
            json={"team": "platform", "remove-me": None},
        ),
        client.get("/v1/sandboxes", params={"pageSize": 10}),
    ]

    assert [response.status_code for response in responses] == [400, 400, 400]
    assert all(
        response.json()["detail"]["code"] == "FLEETS::API_NOT_SUPPORTED" for response in responses
    )


def test_http_renew_and_delete_use_uid_fences(http_fleets):
    client, fake, _ = http_fleets
    created = client.post(
        "/v1/sandboxes",
        json={
            "image": {"uri": "python:3.11"},
            "entrypoint": ["python"],
            "timeout": 3600,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
        },
    ).json()
    sandbox_id = created["id"]
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)

    renewed = client.post(
        f"/v1/sandboxes/{sandbox_id}/renew-expiration",
        json={"expiresAt": expires_at.isoformat()},
    )
    past = client.post(
        f"/v1/sandboxes/{sandbox_id}/renew-expiration",
        json={"expiresAt": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
    )
    deleted = client.delete(f"/v1/sandboxes/{sandbox_id}")

    assert renewed.status_code == 200
    assert fake.last_update.sandbox.expected_uid == f"uid-{sandbox_id}"
    assert fake.last_update.expected_generation == 1
    assert past.status_code == 400
    assert deleted.status_code == 204
    assert fake.last_delete.sandbox.expected_uid == f"uid-{sandbox_id}"


def test_http_renew_maps_fence_conflict_and_missing_sandbox(http_fleets):
    client, fake, _ = http_fleets
    created = client.post(
        "/v1/sandboxes",
        json={
            "image": {"uri": "python:3.11"},
            "entrypoint": ["python"],
            "timeout": 3600,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
        },
    ).json()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
    fake.abort_update_with = grpc.StatusCode.ABORTED

    conflict = client.post(
        f"/v1/sandboxes/{created['id']}/renew-expiration",
        json={"expiresAt": expires_at.isoformat()},
    )
    missing_renew = client.post(
        "/v1/sandboxes/flt-missing/renew-expiration",
        json={"expiresAt": expires_at.isoformat()},
    )
    missing_delete = client.delete("/v1/sandboxes/flt-missing")

    assert conflict.status_code == 409
    assert missing_renew.status_code == 404
    assert missing_delete.status_code == 404


def test_diagnostics_use_new_structured_state(http_fleets):
    client, fake, service = http_fleets
    sandbox_id = client.post(
        "/v1/sandboxes",
        json={
            "image": {"uri": "python:3.11"},
            "entrypoint": ["python"],
            "timeout": 3600,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
        },
    ).json()["id"]
    content = service.get_sandbox_events(sandbox_id)

    assert '"runtime_state": "RUNTIME_STATE_READY"' in content
    assert '"data_plane_state": "DATA_PLANE_STATE_READY"' in content
    assert "sandbox ready" in content

    fake.diagnostic_runtime_state = 99
    assert '"runtime_state": "99"' in service.get_sandbox_events(sandbox_id)


def test_event_diagnostics_enforce_stable_scope_contract(http_fleets):
    _, _, service = http_fleets

    with pytest.raises(HTTPException) as exc_info:
        service.get_sandbox_event_diagnostics("flt-1", "lifecycle")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "code": "DIAGNOSTICS_SCOPE_UNSUPPORTED",
        "message": (
            "Unsupported events diagnostics scope 'lifecycle'. Supported scopes: runtime, all."
        ),
    }


@pytest.mark.parametrize("operation", ["logs", "pause", "resume"])
def test_unsupported_fleets_operations_are_explicit(http_fleets, operation):
    _, _, service = http_fleets

    with pytest.raises(HTTPException) as exc_info:
        if operation == "logs":
            service.get_sandbox_logs("flt-1")
        elif operation == "pause":
            service.pause_sandbox("flt-1")
        elif operation == "resume":
            service.resume_sandbox("flt-1")

    assert exc_info.value.status_code == 501


@pytest.mark.parametrize("pending", [False, True])
def test_http_create_handles_capacity_rejection_and_accepted_pending(http_fleets, pending):
    client, fake, _ = http_fleets
    fake.create_pending = pending
    if not pending:
        fake.reject_create_with = grpc.StatusCode.RESOURCE_EXHAUSTED
    response = client.post(
        "/v1/sandboxes",
        json={
            "image": {"uri": "python:3.11"},
            "entrypoint": ["python"],
            "timeout": 60,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
        },
    )
    if pending:
        assert response.status_code == 202
        assert response.json()["status"]["state"] == "Pending"
    else:
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "1"


def _gateway_config(mode="header"):
    return IngressConfig.model_validate(
        {
            "mode": "gateway",
            "gateway": {
                "address": "*.example.com" if mode == "wildcard" else "ingress.example.com",
                "route": {"mode": mode},
            },
            "secure_access": {
                "active_key": "k",
                "keys": [{"key_id": "k", "key": "c2hhcmVkLXNlY3JldA=="}],
            },
        }
    )


@pytest.mark.parametrize("mode", ["header", "uri"])
def test_http_endpoint_matches_go_scope_without_fastpath_lookup(http_fleets, monkeypatch, mode):
    client, _, service = http_fleets
    service._app_config.ingress = _gateway_config(mode)
    fastpath = Mock(spec=FastPathClient)
    monkeypatch.setattr(service, "_fastpath", fastpath)
    previous = get_current_tenant()
    set_current_tenant(TenantEntry(name="tenant-a", namespace="tenant-a"))
    try:
        response = client.get(
            "/v1/sandboxes/sandbox-123/endpoints/44772",
            headers={"X-Namespace": "attacker"},
        )
    finally:
        set_current_tenant(previous)
    assert response.status_code == 200
    scope = "f1.dGVuYW50LWE.c2FuZGJveC0xMjM.44772.k.uo11HjECmnSuCCRF3v-1AQ"
    body = response.json()
    if mode == "header":
        assert body == {
            "endpoint": "ingress.example.com",
            "headers": {"OpenSandbox-Ingress-To": scope},
        }
    else:
        assert body["endpoint"] == f"ingress.example.com/{scope}"
        assert not body.get("headers")
    assert fastpath.mock_calls == []


@pytest.mark.parametrize("port", [8080, 18080])
def test_endpoint_binds_port_and_default_namespace(http_fleets, port):
    client, _, service = http_fleets
    service._app_config.ingress = _gateway_config()
    response = client.get(f"/v1/sandboxes/flt-123/endpoints/{port}")
    assert response.status_code == 200
    scope = response.json()["headers"]["OpenSandbox-Ingress-To"]
    assert scope.startswith(f"f1.bnMtMQ.Zmx0LTEyMw.{port}.k.")


@pytest.mark.parametrize(
    "invalid", ["no_gateway", "no_keys", "wildcard", "expires", "port", "identity"]
)
def test_endpoint_rejects_unsupported_or_invalid_routes(http_fleets, invalid):
    _, _, service = http_fleets
    config = service._app_config
    config.ingress = _gateway_config("wildcard" if invalid == "wildcard" else "header")
    if invalid == "no_gateway":
        config.ingress = None
    elif invalid == "no_keys":
        config.ingress.secure_access = None
    with pytest.raises(HTTPException) as exc_info:
        service.get_endpoint(
            "bad\nidentity" if invalid == "identity" else "flt-123",
            0 if invalid == "port" else 44772,
            expires=2_000_000_000 if invalid == "expires" else None,
        )
    assert exc_info.value.status_code == 400


def test_fleets_snapshot_runtime_is_noop(http_fleets):
    _, _, service = http_fleets

    runtime = create_snapshot_runtime(config=service._app_config)

    assert runtime.supports_create_snapshot() is False


def test_background_renew_resolves_tenant_namespace(http_fleets):
    client, fake, service = http_fleets
    sandbox_id = client.post(
        "/v1/sandboxes",
        json={
            "image": {"uri": "python:3.11"},
            "entrypoint": ["python"],
            "timeout": 3600,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
        },
    ).json()["id"]
    sandbox = fake.sandboxes.pop(("ns-1", sandbox_id))
    fake.sandboxes[("tenant-a", sandbox_id)] = sandbox
    service.set_tenant_provider(
        SimpleNamespace(list_tenants=lambda: [SimpleNamespace(namespace="tenant-a")])
    )

    service.renew_expiration(
        sandbox_id,
        request=RenewSandboxExpirationRequest(
            expiresAt=datetime.now(timezone.utc) + timedelta(hours=2)
        ),
    )

    assert fake.last_get.sandbox.namespaced_name.namespace == "tenant-a"
    assert fake.last_update.sandbox.namespaced_name.namespace == "tenant-a"


def test_background_lookup_does_not_treat_fastpath_failure_as_namespace_miss(http_fleets):
    _, fake, service = http_fleets
    fake.get_error_by_namespace["ns-1"] = grpc.StatusCode.UNAVAILABLE
    service.set_tenant_provider(
        SimpleNamespace(list_tenants=lambda: [SimpleNamespace(namespace="tenant-a")])
    )

    with pytest.raises(HTTPException) as exc_info:
        service.renew_expiration(
            "flt-1",
            request=RenewSandboxExpirationRequest(
                expiresAt=datetime.now(timezone.utc) + timedelta(hours=2)
            ),
        )

    assert exc_info.value.status_code == 503


def test_http_create_returns_503_when_fastpath_is_unavailable(monkeypatch):
    config = AppConfig(
        server=ServerConfig(host="0.0.0.0", port=8080, api_key="x"),
        runtime=RuntimeConfig(type="fleets", execd_image="ghcr.io/opensandbox/execd:latest"),
        fleets=FleetsRuntimeConfig(
            namespace="ns-1",
            fastpath_endpoint="127.0.0.1:1",
            fastpath_timeout_seconds=1,
        ),
    )
    fastpath = FastPathClient(endpoint="127.0.0.1:1", timeout_seconds=1)
    service = FleetSandboxService(config, fastpath_client=fastpath)
    monkeypatch.setattr(lifecycle, "sandbox_service", service)
    app = FastAPI()
    app.include_router(lifecycle.router, prefix="/v1")

    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/sandboxes",
                json={
                    "image": {"uri": "python:3.11"},
                    "entrypoint": ["python"],
                    "timeout": 3600,
                    "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
                },
            )
            assert response.status_code == 503
    finally:
        fastpath.close()
