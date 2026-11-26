---
title: P1-3 Pause/Resume Continuity, Clock Drift, and Clone Isolation Report
description: Verification of envd and egress state continuity across same-Fastlet and cross-Fastlet pause/resume, clock sync via same-token re-/init, clone token isolation, and envd crash behavior.
---

# P1-3: Pause/Resume Continuity, Clock Drift, and Clone Isolation

**Date**: 2026-11-26 | **Cluster**: `kind-fsb` (single Linux/amd64 KVM node `fsb-control-plane`) | **Pool**: `firecracker-egress-pool` (`actionHandlers: [egress]`, `infraComponents: [egress]`) | **Image**: `<registry>/envd-only:probe-p01`

## 1. Executive Summary

- **Same-Fastlet pause/resume (`fsb-p13-main` on `firecracker-egress-pool-fastlet-pb6tz`)**: Passed. `envd` `accessToken`, `envVars`, `/root/sentinel.txt`, detached background counter process (`2 -> 3 -> 5`, `PS_COUNTER=1`), `egress` DNS allow/deny policy (`example.com` resolved, `httpbin.org` `NXDOMAIN`), and `Paused`-state route release (`GET /health -> 503`) all behaved as expected.
- **Guest clock drift & same-token re-`/init` sync**: Passed. After a `67.75s` pause + `8s` sleep, guest UTC trailed host UTC by `77s` (`1795674582` vs `1795674659`). Re-calling `POST /init` with the same `accessToken`, current UTC `timestamp`, and full `envVars` map returned `204`, synced guest clock to `1795674659 == host_utc_now`, and preserved `envVars` (`alive-across-pause|1`).
- **Cross-Fastlet pause/resume (`fsb-p13-cross`: `pb6tz` -> `64kf8`)**: Guest + `egress` state continuity **passed** (`64kf8` resumed in `1.59s` with `attempt=2`, `routeGeneration=2`, `egress` policy reloaded, `SENTINEL=p13-cross-sentinel`, counter `404 -> 406`, `PS_COUNTER=1`, `ENV_POST=cross-fastlet|2`, `example.com` allowed, `httpbin.org` `NXDOMAIN`, wrong token `401`). However, **two real infrastructure defects** were uncovered in the surrounding control/data plane (see Section 4).
- **Clone isolation & `envd` crash (`fsb-p13-clone-a`, `fsb-p13-clone-b`)**: Passed. Cross-tokens returned `401` while own tokens returned `200` (`CLONE_ID=A` / `B`). Killing `envd` (`kill -9 $(pidof envd)`) dropped the stream and left `/health`, `/metrics`, and `/process.Process/Start` returning `503` (both unauthenticated and with `token-clone-a`), confirming `envd` does not silently restart into an unauthenticated open state.

## 2. Continuity Matrix

| Dimension | Same-Fastlet (`pb6tz -> pb6tz`, `fsb-p13-main`) | Cross-Fastlet (`pb6tz -> 64kf8`, `fsb-p13-cross`) | Evidence |
| :--- | :--- | :--- | :--- |
| Pause duration (`POST /pause` -> `Paused`) | `67.75s` (`Suspended`) | `63.67s` (`Suspended`) | [step1.log](evidence/step1.log), [step2_prep_pause.log](evidence/step2_prep_pause.log) |
| Route released while `Paused` | `GET /health` -> `503` | N/A (pod deleted while `Paused`) | [step1.log](evidence/step1.log) |
| Resume duration (`POST /resume` -> `Running`) | `0.56s` (`pb6tz`) | `1.59s` (`64kf8`, `attempt=2`, `routeGeneration=2`) | [step1.log](evidence/step1.log) |
| `envd` `accessToken` enforced | Wrong token `401`; original token `200` | Wrong token `401`; original token `200` | [step1.log](evidence/step1.log), [step2_after_ingress_restart.log](evidence/step2_after_ingress_restart.log) |
| `envVars` preserved | `ENV_POST=alive-across-pause\|1` | `ENV_POST=cross-fastlet\|2` | [step1.log](evidence/step1.log), [step2_after_ingress_restart.log](evidence/step2_after_ingress_restart.log) |
| Rootfs file preserved | `/root/sentinel.txt` = `p13-sentinel-v1` | `/root/sentinel.txt` = `p13-cross-sentinel` | [step1.log](evidence/step1.log), [step2_after_ingress_restart.log](evidence/step2_after_ingress_restart.log) |
| Detached process continuity | Counter `2 -> 3 -> 5`, `PS_COUNTER=1` | Counter `2 -> 404 -> 406`, `PS_COUNTER=1` | [step1.log](evidence/step1.log), [step2_after_ingress_restart.log](evidence/step2_after_ingress_restart.log) |
| `egress` allow/deny (`172.30.0.1:53`) | `example.com` A/AAAA; `httpbin.org` `NXDOMAIN` | `example.com` A/AAAA; `httpbin.org` `NXDOMAIN` (`64kf8` reloaded policy) | [step1.log](evidence/step1.log), [step2_after_ingress_restart.log](evidence/step2_after_ingress_restart.log) |
| Guest clock drift & re-`/init` sync | Drifted `-77s` (`1795674582` vs `1795674659`); synced to `1795674659` via re-`/init` (`204`) | Not re-tested (already verified in Step 1) | [step1.log](evidence/step1.log) |

## 3. Guest Clock Drift & Gate 1b Re-`/init` Recommendation

- **Observed drift**: Firecracker snapshot/restore freezes guest monotonic/wall progression across `Paused` (`67.75s` publish + `8s` sleep), leaving `GUEST_UTC_POST=1795674582` vs `host_utc_now=1795674659` (`-77s`).
- **Sync behavior**: Calling `POST /init` with the same `accessToken` (`p13-token-main`), current UTC `timestamp` (`2026-11-26T06:30:59Z`), and full `envVars` (`{"P13_MARKER": "alive-across-pause", "P13_SEQ": "1"}`) returned `204`, immediately synced guest UTC (`GUEST_UTC_SYNCED=1795674659 == host_utc_now=1795674659`), and kept `ENV_SYNCED=alive-across-pause|1`.
- **Gate 1b contract recommendation**: Because `envd 0.8.0` `POST /init` calls `ReplaceUserVars` (full replace, not merge) whenever `timestamp` is newer than `lastSetTime`, any lifecycle/SDK `resume` hook that re-calls `/init` for clock synchronization **must** send: (1) the original `accessToken`, (2) the current UTC `timestamp`, and (3) the **complete** `envVars` map (plus `caBundle` if MITM CA was injected), or omit `envVars` only if `envd` is verified not to clear it on nil.

## 4. Infrastructure Defects Discovered During Step 2

### Defect A: `opensandbox-ingress-gateway` Stale FastPath Pod-IP Cache on Cross-Fastlet Resume

- **Symptom**: After `fsb-p13-cross` paused on `pb6tz` (`10.244.0.11`), `pb6tz` was deleted, and `fsb-p13-cross` resumed on `64kf8` (`10.244.0.16`, `routeGeneration=2`, `Ready=True`), `GET /v1/sandboxes/fsb-p13-cross/endpoints/49983` returned the same `OpenSandbox-Ingress-To` token (`f1.b3BlbnNhbmRib3gtZGF0YXBsYW5l.ZnNiLXAxMy1jcm9zcw.49983.a.s9FzNsj4ZxjUr-sNB-BLXg`). For **>3 minutes** after resume, `opensandbox-ingress-gateway` kept routing requests to the deleted pod IP `http://10.244.0.11:5780/...`, failing with `TimeoutError` / `context canceled`.
- **Verification**: Rolling-restarting `deploy/opensandbox-ingress-gateway` cleared its in-memory FastPath route cache; the new `ingress` pod immediately resolved `fsb-p13-cross` to `http://10.244.0.16:5780/...` and all continuity checks succeeded.
- **Root-cause area**: `opensandbox-ingress-gateway` caches FastPath target `PodIP:5780` per `OpenSandbox-Ingress-To` key without invalidating on `routeGeneration` change, `Sandbox` placement updates, or upstream TCP dial failure.

### Defect B: Multi-Fastlet Startup Image-Cache GC on Shared Node `hostPath`

- **Symptom**: When `firecracker-egress-pool` scaled from `poolMin: 1` to `2` on the single node `fsb-control-plane`, the newly started Fastlet pod `64kf8` ran startup GC (`driver.go:257`) on the shared `/var/lib/fast-sandbox/firecracker/images/` hostPath and removed digest `10524c0826b26d0a12c928549a66dd13b1e5ccd35f85c5d36220dfe81414fbe9` (`probe-p01`) as "unreferenced" (`64kf8` had 0 local sandboxes yet). Eight seconds later, co-located `pb6tz` attempted a 2nd `pause` on `fsb-p13-main` and looped (`pauseAttempt: 35`) with `read source image manifest: open /var/lib/fast-sandbox/firecracker/images/10524c0826b26d0a12c928549a66dd13b1e5ccd35f85c5d36220dfe81414fbe9/manifest.json: no such file or directory`.
- **Evidence**: [step2_64kf8_gc.log](evidence/step2_64kf8_gc.log).
- **Root-cause area**: Multiple Fastlet pods sharing one node-level `hostPath` (`runtime` state/image cache) must not run uncoordinated per-pod image GC at startup, or must reference-count across all Fastlets on the node.

## 5. Clone Isolation and `envd` Crash Behavior (Step 3)

- **Image source note**: Passing `s3://sandbox-images/publish/1d6225f6ef84713c/manifest.json` directly in `Sandbox.spec.image` timed out waiting for `Ready` (`25s`), so `fsb-p13-clone-a` and `fsb-p13-clone-b` were created from OCI `<registry>/envd-only:probe-p01`.
- **Token isolation**: `clone-a` (`token-clone-a`, `CLONE_ID=A`) rejected `token-clone-b` with `401`, and `clone-b` (`token-clone-b`, `CLONE_ID=B`) rejected `token-clone-a` with `401`, while each accepted its own token (`200`).
- **Crash behavior (`kill -9 $(pidof envd)` in `fsb-p13-clone-a`)**: Stream terminated (`IncompleteRead(36 bytes read)`). After 3s, `GET /health`, `GET /metrics` (unauth & `token-clone-a`), and `POST /process.Process/Start` (unauth & `token-clone-a`) all returned `503`. `envd` did **not** restart into an unauthenticated open state. `Sandbox` CR `status` remained `Ready=True` because no in-guest `envd` readiness probe was configured on the `Sandbox` CR.

## 6. Draft `#2840` Q5 Owner Summary (Draft Only — Not Posted)

> **Q5 (`pause` / `resume` continuity across `envd 0.8.0` + `egress`):**
> Verified on `kind-fsb` (`firecracker-egress-pool` with `actionHandlers: [egress]`). Across both same-Fastlet (`67.75s` pause, `0.56s` resume) and cross-Fastlet (`63.67s` pause, `1.59s` resume onto a fresh Fastlet pod after deleting the original pod), `envd` `accessToken` (`401` on wrong token), `envVars`, rootfs files, detached background processes, and `egress` DNS/nftables policies all survived intact. Guest UTC drifted behind host UTC by the paused duration (`-77s`) and was resynchronized via same-token `POST /init` (`204`, passing current UTC `timestamp` and the full `envVars` map because `ReplaceUserVars` replaces rather than merges). Two sibling clones initialized with distinct tokens rejected cross-tokens (`401`), and `kill -9 envd` resulted in `503` across `/health`, `/metrics`, and `/process.Process/Start` with no unauthenticated restart. Two infrastructure issues were observed during cross-Fastlet testing: (1) `opensandbox-ingress-gateway` kept a stale FastPath Pod-IP cache pointing to the deleted Fastlet pod until restarted, and (2) a second Fastlet pod starting on the same node ran image-cache GC on the shared `hostPath` (`driver.go:257`) and removed an image manifest still needed by a co-located Fastlet pod's subsequent `pause`.

## 7. `P2-2` Restore Guidance

- **Route propagation retry**: Newly created or resumed `fsb-*` sandboxes can briefly return `503` on the first `/init` or `/health` call right after `Ready=True`; SDK/lifecycle callers should retry `503` with short backoff.
- **Clock sync on `resume`**: `P2-2` should trigger same-token re-`/init` (with current UTC `timestamp` and full `envVars`) after `resume` completes, or document the guest clock drift expectation.
- **Ingress FastPath invalidation**: Fix `opensandbox-ingress-gateway` FastPath cache invalidation on `routeGeneration` / pod replacement before relying on zero-downtime cross-Fastlet `resume` in production.
