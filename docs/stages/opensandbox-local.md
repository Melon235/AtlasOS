# Atlas Sandbox — Local OpenSandbox Setup

Status: verified on the current Windows + WSL2 + Docker host. Atlas Sandbox uses a self-hosted
OpenSandbox execution backend; OpenSandbox is infrastructure, not the Atlas runtime.

## Verified topology

```text
Windows 11 host
  └── Atlas / experiments (Windows Python 3.12 venv)
        │  http://localhost:8080
        ▼
WSL2 Ubuntu
  └── OpenSandbox server 1.2.0 (127.0.0.1:8080)
        │  Docker Engine API
        ▼
      Docker Engine 29.6.0
        ├── opensandbox/execd:v1.1.0
        ├── opensandbox/egress:v1.1.7   (network policy sidecar)
        └── atlas-uat-python:3.12       (sandbox image)
```

Windows `localhost:8080` reached the WSL-bound OpenSandbox server successfully. The server was
started in a persistent WSL terminal; it is not run from Atlas.

## Install the server

```bash
# WSL2 Ubuntu; uv is already available in this environment
uv tool install opensandbox-server==1.2.0

# Generate the Docker example configuration
opensandbox-server init-config ~/.sandbox.toml --example docker

# Keep the server loopback-only and enable packet-level egress enforcement
#   [server] host = "127.0.0.1"
#   [docker] publish_host = "127.0.0.1"
#   [egress] mode = "dns+nft"
```

The generated config enables the Docker runtime and the egress sidecar. The local development run
used no server API key and therefore bound to `127.0.0.1` only:

```bash
OPENSANDBOX_INSECURE_SERVER=YES opensandbox-server
```

If the server is configured with `server.api_key`, set the same locally generated key in
`OPEN_SANDBOX_API_KEY` for Atlas. That is local service authentication, not an external cloud
credential; never commit it.

Verify the server:

```bash
curl http://127.0.0.1:8080/health
# {"status":"healthy"}
```

## Docker prerequisite

The Docker daemon must be able to pull `opensandbox/execd`, `opensandbox/egress`, the sandbox image,
and the base image used by the UAT image. On this host the Docker daemon needed a working outbound
proxy; a stale proxy made all image pulls fail. The verified configuration routed the Docker daemon
through the Windows host proxy at `172.26.96.1:7890`. If Docker cannot pull, check:

```bash
docker info | grep -i proxy
docker run --rm python:3.12 python --version
```

No Atlas code talks to the Docker API directly.

## Build the UAT image

The fixture needs Git (M5 bundle provisioning) and pytest. Build the local image once:

```bash
cd /mnt/d/Work/AtlasOS
docker build -t atlas-uat-python:3.12 experiments/fixtures/uat_image
docker run --rm atlas-uat-python:3.12 sh -lc \
  'git --version && python3 -m pytest --version'
```

`experiments/fixtures/uat_image/Dockerfile` pins Python 3.12 and pytest 9.1.1.

## Atlas configuration

[`experiments/configs/v2-uat.yaml`](../../experiments/configs/v2-uat.yaml):

```yaml
sandbox:
  provider: opensandbox
  endpoint: localhost:8080
  protocol: http
  image: atlas-uat-python:3.12
  workspace_root: /workspace/project
  network: none
  timeout_minutes: 20
  ready_timeout_seconds: 240
  cpu: "1"
  memory: "1Gi"
  use_server_proxy: true
  retain_on_failure: false
```

Provider, endpoint, image, workspace, network, CPU/memory, and TTL are trusted Host configuration.
The model cannot select or change them.

## Run the evidence

```powershell
cd D:\Work\AtlasOS
$env:PYTHONPATH = "src;."
.\.venv\Scripts\python.exe experiments\eval_real_sandbox.py
.\.venv\Scripts\python.exe experiments\uat_v2.py --runs-root experiments\runs\uat
```

The evaluator checks server health, create, file transfer, command execution, network-none,
Host/sandbox sentinels, export, and destroy. The UAT harness drives the real CLI through M1–M7 and
runs independent external acceptance on the exported workspace.

## Cleanup and limitations

- Normal completion and failure cleanup call `provider.destroy()`; the OpenSandbox sandbox TTL is
  finite as a backstop.
- `LOST` remains terminal; Atlas never creates a replacement sandbox.
- OS-process sandbox reattachment is **not implemented** in this stage. The adapter deliberately
  does not call `Sandbox.connect()` on a cache miss.
- Network-none enforcement depends on the local egress sidecar. Atlas records requested, accepted,
  and empirically verified outcomes separately; the verified run used `mode = "dns+nft"` and an
  actual `opensandbox/egress` sidecar.
- The default local server has no authentication and is loopback-only. Do not expose it on
  `0.0.0.0`, a LAN, or the public internet.
