# B0 environment unblock — Docker image, native environment and mock service

**Scope:** environment only. No Runtime change, no formal model evaluation, no image substitution,
no Docker data cleared, no OpenSandbox object touched.

Pinned image:
`acciolyk/accio_bench@sha256:1e9cf5c72a56794175b7d06ece036b92e296e6b7e9e9a7fa244026f6acea3859`

**Result: unblocked.** The image is registered locally, its RepoDigest matches the pin exactly, it is
`linux/amd64`, it runs a container, and the first Slack task's native mock service starts and serves
the full 19-message thread. All five acceptance checks pass.

---

## 1. Where Docker actually lives

| Probe | Result |
|---|---|
| `Get-Command docker` (Windows) | not found |
| `C:\Program Files\Docker\Docker\Docker Desktop.exe` | absent |
| Windows services matching `docker` | none |
| `docker context ls` (WSL Ubuntu) | `default * unix:///var/run/docker.sock` |
| Engine | Docker Engine 29.6.0, `overlayfs` via `io.containerd.snapshotter.v1` |
| `DockerRootDir` | `/var/lib/docker` (WSL ext4 disk, 842 GB free) |
| Daemon proxy config | `/etc/systemd/system/docker.service.d/http-proxy.conf` → `HTTP(S)_PROXY=http://172.26.96.1:7890`, `NO_PROXY=localhost,127.0.0.1,::1` |

So: **no Windows-side Docker at all**; one Docker Engine inside WSL2 Ubuntu, using the containerd
image store. AtlasRuntime and the harness stay on Windows native — nothing was migrated; the adapter
reaches the daemon through `wsl.exe -d Ubuntu -- docker …`, which is the existing design.

## 2. Root cause of the stalled pull

1. In WSL, the registry hostnames resolve to **IPv6-only** addresses:
   `registry-1.docker.io → 2a03:2880:f10c:283:face:b00c:0:25de`,
   `auth.docker.io → 2a03:2880:…`, `production.cloudflare.docker.com → 2001::c73b:94de`.
2. Direct IPv6 HTTPS from WSL is unreachable: `curl https://registry-1.docker.io/v2/` →
   `curl: (28) Connection timed out after 20033 ms`; the auth endpoint behaves the same.
3. The daemon therefore only reaches the registry **through its configured proxy** (172.26.96.1:7890).
   That path works — `docker pull alpine:3.20` succeeded — but it is slow and drops long connections,
   which is exactly what the earlier log showed: every layer reached `Download complete`, no layer
   reached `Pull complete`, the pull process stayed alive and idle, and `docker manifest inspect`
   timed out on the registry ping.
4. Retrying let the daemon finish the layers through the proxy; the image then registered normally.

Not the cause: disk space (842 GB free), a corrupt image store (nothing was pruned), or a missing
platform manifest (`os=linux arch=amd64`).

## 3. Verification (acceptance checklist)

| Check | Evidence |
|---|---|
| `docker pull` exits successfully | image registered; no pull process remains |
| `docker image inspect` finds the pinned image | `RepoDigests = ["acciolyk/accio_bench@sha256:1e9cf5c7…"]` |
| digest and platform verified | `os=linux arch=amd64`, size 1 035 712 926 bytes, created 2026-08-01 |
| Linux container starts from it | entrypoint `/entrypoint.sh` starts the workstation stack (uvicorn :7878, Chromium :9222/9223, noVNC :6080) |
| first Slack task's native mock service starts | adapter smoke: `services_started=true`, health probe OK, container 414 ms / services 752 ms / health 261 ms / cleanup 431 ms |

Native data path, exercised through the official mock API on port 9110:

```text
POST /slack/messages        {"channel":"#rfq-stainless-bottle","max_results":30} -> 19 messages
POST /slack/messages/get    x19 (one per message_id)                            -> all senders
audit -> workspace/outputs/mock_audit/slack_audit.json (22 call entries, 19 get calls)
```

That audit is precisely what the task's `setup_gate` audits (≥1 list call, ≥19 get calls, no
`/slack/send`, no `/slack/drafts/save`).

Native grader, run against that live audit plus a schema-valid deliverable:

| Field | Value |
|---|---|
| status | `GRADER_OK` |
| score | 1.0 |
| checks | 7 / 7 (`setup_gate` … `budget_ceiling` all ok) |

This run validates the environment → deliverable → grader chain. It is **not** an agent result and
must never be quoted as a benchmark score: the deliverable's values were written by the operator for
the check, not produced by AtlasRuntime.

## 4. Adapter defect found and fixed

While validating, the graded directory was wrong: the task writes deliverables to `/task/outputs`
(inside the workspace mount), but the adapter pointed `outputs_dir` at the `/data/amt-out` mirror, so
`NativeGrader` saw an empty directory and reported 0/7 even though the run had produced the audit.

Fixed in `environment.py`: `outputs_dir = <run>/workspace/outputs`; the `/data/amt-out` mount is kept
for upstream parity. `tests/unit/test_b0_commerce_adapter.py` now asserts the graded directory.

## 5. What was deliberately not done

* No other image or tag was used; the digest pin is unchanged.
* No `docker system prune`, no volume removal, no image store reset.
* No OpenSandbox container, network or volume was modified. The only containers created and removed
  were temporary verifications from the benchmark image (`docker rm -f` on the exact names); the
  daemon currently reports zero running containers from this work.
* No model evaluation was started.

## 6. If another machine must obtain the same image

1. Preferred: a host where the daemon can reach `registry-1.docker.io` (IPv4 or a stable proxy):
   `docker pull --platform linux/amd64 acciolyk/accio_bench@sha256:1e9cf5c7…`, then verify
   `docker image inspect <img> --format '{{index .RepoDigests 0}}'` equals the pin.
2. Registry-blocked host: on a connected machine run
   `skopeo copy docker://acciolyk/accio_bench@sha256:1e9cf5c7… docker-archive:accio_bench.tar`
   (or `docker save`), transfer the tarball, then `docker load -i accio_bench.tar`; `RepoDigests`
   keeps the pinned digest.
3. On a host whose WSL registry DNS is IPv6-only, the daemon proxy in §1 is the working path; set it
   before pulling rather than retrying blind, and expect several minutes for the 3.85 GB image.

## 7. Verdicts

| Dimension | Verdict |
|---|---|
| Docker deployment identified | **PASS** (WSL2 Engine, no Windows Docker) |
| Pull root cause identified | **PASS** (IPv6-only registry DNS + proxy-only egress, slow proxy path) |
| Pinned image present with matching digest | **PASS** |
| Image runs a Linux container | **PASS** |
| First task's native mock service starts | **PASS** (19 messages, audit with 19 get calls) |
| Native grader reachable end-to-end | **PASS** (7/7 on an operator-written deliverable) |
| Adapter artifact path | **PASS** after fix |
| Runtime / OpenSandbox untouched | **PASS** (no runtime change, no prune, no OpenSandbox objects touched) |

Environment work is complete; the formal A0/A1 matrix is still not executed and remains the next
step, not a result of this document.
