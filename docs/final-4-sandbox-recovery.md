# Final-4 — Local Sandbox Environment Recovery

Date: 2026-11-13. Worktree `D:\Work\AtlasRuntime`, branch
`chore/final-1-5-repository-baseline`, `BASE_SHA` = `d801dfed1ebd0f40a9711195296f162f0fd6fb59`.

Scope: diagnose and restore the **existing self-hosted OpenSandbox** path. No new provider, no
commercial service, no core-runtime change.

---

## 1. Verdict

The local sandbox was never broken. **The Final-4 blocker was a diagnostic error in my own probe**,
not a missing credential, not a stopped server, not a runner mis-wiring.

| Question | Answer |
|---|---|
| Is the old local OpenSandbox still usable? | **Yes** — healthy, real create/execute/destroy passed |
| Is Windows → WSL communication working? | **Yes** — `http://localhost:8080/health` → 200 `{"status":"healthy"}` |
| Does the original sandbox image exist? | **Yes** — `atlas-uat-python:3.12`, plus `opensandbox/execd:v1.1.0`, `opensandbox/egress:v1.1.7` |
| What config does the runtime actually load? | default `AtlasConfig()` = `sandbox.provider="fake"`; real sandbox only via `experiments/configs/v2-uat.yaml` (coding) or `eval-benchmark.yaml` (evaluation) |
| Real cause of `SandboxCredentialError`? | wrong call shape: `build_sandbox_provider(SandboxConfig(...))` passed a config where the function expects a provider **name string** |
| Is a local auth key required? | **No** — loopback server started with `OPENSANDBOX_INSECURE_SERVER=YES`; `OPEN_SANDBOX_API_KEY` is optional and only for a server configured with `server.api_key` |
| Did a real sandbox create/execute/destroy succeed? | **Yes** — see §5 |
| Any source change required? | **No** |

---

## 2. Topology verified on this host

```text
Windows 11 (Python 3.12.15 venv)
  └── AtlasRuntime / experiments
        │  http://localhost:8080
        ▼
WSL2 Ubuntu
  └── opensandbox-server 1.2.0 (pid 65647, 127.0.0.1:8080)
        │  Docker Engine API
        ▼
      Docker Engine 29.6.0
        ├── opensandbox/execd:v1.1.0
        ├── opensandbox/egress:v1.1.7
        └── atlas-uat-python:3.12
```

Evidence:

```text
GET http://localhost:8080/health      → 200 {"status":"healthy"}
wsl docker version --format ...       → 29.6.0
docker images (filtered)              → atlas-uat-python:3.12, opensandbox/execd:v1.1.0, opensandbox/egress:v1.1.7
pgrep -af opensandbox-server          → /home/melon/.local/bin/opensandbox-server
```

Server startup mode (from `docs/stages/opensandbox-local.md`): `OPENSANDBOX_INSECURE_SERVER=YES
opensandbox-server`, `[server] host = "127.0.0.1"`, `[egress] mode = "dns+nft"`.

## 3. Root cause of `SandboxCredentialError`

`src/atlasos/sandbox/provider.py:32`:

```python
def build_sandbox_provider(name: str, *, environ=None, opensandbox_options=None) -> SandboxProvider:
    if name == "opensandbox":
        return OpenSandboxProvider(opensandbox_options or OpenSandboxOptions(),
                                   api_key=env.get("OPEN_SANDBOX_API_KEY") or None)
    if name not in REAL_SANDBOX_PROVIDERS:
        raise SandboxCredentialError(
            f"sandbox provider {name!r} is not a configured real Atlas Sandbox provider; ...")
```

Final-4 called it as `build_sandbox_provider(cfg)` with a `SandboxConfig` object. The object is not the
string `"opensandbox"`, so the first branch was skipped and the **wrong-type** branch raised
`SandboxCredentialError`, embedding the whole config repr in the message — which is exactly the
truncated text recorded in the Final-4 report. Reproduced verbatim:

```text
REPRO: SandboxCredentialError
sandbox provider SandboxConfig(enabled=False, provider='opensandbox', endpoint='localhost:8080', ...)
```

Correct calls (both already used by the shipped code):

```python
build_sandbox_provider("opensandbox")                     # → OpenSandboxProvider
build_sandbox_provider("opensandbox", opensandbox_options=OpenSandboxOptions(...))
```

No `OPEN_SANDBOX_API_KEY` is involved in either branch: the key is optional and only sent if the local
server is configured with `server.api_key`.

## 4. What the runtime actually loads

| Entry point | Config source | Provider |
|---|---|---|
| Default `AtlasConfig()` | code defaults | `sandbox.provider="fake"`, `enabled=False` → real sandbox **not** selected |
| `atlas run --mode coding --config experiments/configs/v2-uat.yaml` | `app/coding.py::load_config` → `_build_provider` | `"opensandbox"` → `OpenSandboxProvider`, `is_real=True` |
| `experiments.evaluation` with `experiments/configs/eval-benchmark.yaml` | `runner.py::_build_provider` | `"opensandbox"` → `OpenSandboxProvider` |

Both `_build_provider` implementations pass `sandbox.provider` (a string) plus `OpenSandboxOptions`
built from the same YAML fields, i.e. **the runner wiring is already correct**. The only requirement is
that a real-sandbox config is passed explicitly; the default config intentionally selects `fake` so that
offline tests never touch a container.

## 5. Real deterministic acceptance (no model)

`experiments/evaluation/final4/sandbox_recovery_check.py` — uses the shipped provider, image and export
helper:

```text
health                healthy
capabilities          provider=opensandbox isolation=container networking=allowlist file_transfer=true
sandbox_id            95b15e69-d5c1-4660-8a96-54187f212e87
create_ok             true
write_ok              true        (marker.txt = "ATLAS-SANDBOX-RECOVERY-OK\n")
read_matches          true
exit_code             0
command_output        42 | marker.txt | pytest 9.1.1
python_ok             true
pytest_present        true
export_ok             true        (exported workspace contains marker.txt)
destroy_ok            true
health_after          healthy
```

The container ran inside WSL Docker with `network="none"` declared on the spec, so the untrusted-code
boundary is the real container, not the host.

## 6. Cleanup and leakage check

`docker ps -a` filtered for `sandbox|atlas` after the run: **0 containers**. Only the sandbox created by
this check (id above) existed during the run and it was destroyed by handle; no other container,
volume or sandbox belonging to earlier work was touched.

## 7. Consequence for the Final-4 report

The Final-4 statement

> `SANDBOX_BLOCKED: opensandbox raises SandboxCredentialError`

is **wrong** and is superseded by this document. The corrected status:

- local sandbox environment: **PASS** (restored / never actually lost);
- live model runs: still **BLOCKED**, now for exactly one reason — no `DEEPSEEK_API_KEY` in this
  workspace — not because of the sandbox;
- native-vs-Atlas container parity: the container backend is available, so the remaining gap is only
  the neutral adapter's container binding, not the server.

## 8. Commands to re-run the check

```powershell
Set-Location D:\Work\AtlasRuntime
.\.venv\Scripts\python.exe -m experiments.evaluation.final4.sandbox_recovery_check
```

```powershell
# from WSL, to confirm the server and images
curl http://127.0.0.1:8080/health
docker images | grep -E 'atlas-uat|opensandbox'
```

## 9. Required source change

**None.** No `src/`, dependency, config or CI change was needed or made. The only new files are this
report and the deterministic check script.
