"""Native task environment: the upstream container, workspace and mock services.

Every run gets its own container, its own copy of the task workspace and its own mock state. The
upstream task directory is mounted read-only so the adapter cannot rewrite task data, and the
private directory is mounted read-only for the services that need fixtures.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from experiments.business_benchmarks.commerce_agent_bench.container import (
    CommandResult,
    ContainerBackend,
    ContainerError,
    Mount,
)
from experiments.business_benchmarks.commerce_agent_bench.upstream import NativeTask

REMOTE_WORKSPACE = "/task"
REMOTE_OUTPUTS = "/data/amt-out"
REMOTE_LOGS = "/data/logs"


@dataclass(frozen=True)
class EnvironmentConfig:
    image: str | None = None
    platform: str = "linux/amd64"
    startup_timeout_seconds: int = 180
    start_services: bool = True


@dataclass
class EnvironmentHandle:
    task_id: str
    container: str
    run_dir: Path
    workspace_dir: Path
    outputs_dir: Path
    logs_dir: Path
    timings_ms: dict[str, float] = field(default_factory=dict)
    services_started: bool = False
    published_ports: dict[int, int] = field(default_factory=dict)
    environment_failure: str | None = None

    @property
    def usable(self) -> bool:
        return self.environment_failure is None

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "container": self.container,
            "run_dir": str(self.run_dir),
            "workspace_dir": str(self.workspace_dir),
            "outputs_dir": str(self.outputs_dir),
            "services_started": self.services_started,
            "published_ports": {str(k): v for k, v in self.published_ports.items()},
            "timings_ms": self.timings_ms,
            "environment_failure": self.environment_failure,
        }


class NativeEnvironment:
    def __init__(
        self,
        task: NativeTask,
        run_dir: Path,
        backend: ContainerBackend,
        *,
        config: EnvironmentConfig | None = None,
        container_name: str | None = None,
    ) -> None:
        self.task = task
        self.run_dir = run_dir
        self.backend = backend
        self.config = config or EnvironmentConfig()
        self.container_name = container_name or f"atlas-cab-{run_dir.name}"

    # -- lifecycle --------------------------------------------------------
    def create(self) -> EnvironmentHandle:
        timings: dict[str, float] = {}
        workspace = self.run_dir / "workspace"
        # The task writes its deliverables to ``/task/outputs`` (upstream contract), which is the
        # workspace mount, not the separate ``/data/amt-out`` mirror. The graded directory is
        # therefore ``workspace/outputs``.
        outputs = workspace / "outputs"
        artifacts = self.run_dir / "outputs"
        logs = self.run_dir / "host-logs"
        started = time.perf_counter()
        for path in (workspace, outputs, artifacts, logs):
            path.mkdir(parents=True, exist_ok=True)
        self._stage_workspace(workspace)
        timings["stage_ms"] = _ms(started)

        handle = EnvironmentHandle(
            task_id=self.task.task_id,
            container=self.container_name,
            run_dir=self.run_dir,
            workspace_dir=workspace,
            outputs_dir=outputs,
            logs_dir=logs,
            timings_ms=timings,
        )

        ports = sorted(
            {
                *([self.task.host_published_port] if self.task.host_published_port else []),
                *[
                    int(mock["port"])
                    for mock in self.task.runtime_mocks.values()
                    if mock.get("port") is not None
                ],
            }
        )
        container_started = time.perf_counter()
        try:
            self.backend.run_container(
                name=self.container_name,
                image=self.config.image or self.task.image,
                mounts=[
                    Mount(workspace, REMOTE_WORKSPACE),
                    Mount(artifacts, REMOTE_OUTPUTS),
                    Mount(logs, REMOTE_LOGS),
                    Mount(self.task.root, f"/benchmark/tasks/{self.task.task_id}", read_only=True),
                    Mount(
                        self.task.private_dir,
                        f"/benchmark/private/{self.task.task_id}",
                        read_only=True,
                    ),
                ],
                ports=ports,
                env={"BENCH_TASK_ID": self.task.task_id, "ATLAS_ADAPTER": "b0"},
                labels={"commerce-agent-bench": "true", "atlas.b0": "true"},
                platform=self.config.platform,
            )
        except ContainerError as error:
            handle.environment_failure = f"container_start_failed: {error}"
            timings["container_ms"] = _ms(container_started)
            return handle
        timings["container_ms"] = _ms(container_started)

        for port in ports:
            try:
                published = self.backend.published_port(self.container_name, port)
            except ContainerError:
                published = None
            if published is not None:
                handle.published_ports[port] = published

        if self.config.start_services and self._has_service_script():
            services_started = time.perf_counter()
            result = self._start_services()
            timings["services_ms"] = _ms(services_started)
            if not result.ok:
                handle.environment_failure = (
                    f"services_failed(exit={result.exit_code}): {result.stderr.strip()[:400]}"
                )
                return handle
            handle.services_started = True

        health_started = time.perf_counter()
        failure = self._wait_healthy()
        timings["health_ms"] = _ms(health_started)
        if failure is not None:
            handle.environment_failure = failure
        return handle

    def teardown(self, handle: EnvironmentHandle) -> str | None:
        """Destroy the container; return a cleanup error string when it cannot be confirmed."""

        started = time.perf_counter()
        try:
            self.backend.destroy(handle.container)
            handle.timings_ms["cleanup_ms"] = _ms(started)
            return None
        except ContainerError as error:
            handle.timings_ms["cleanup_ms"] = _ms(started)
            return str(error)

    # -- helpers ----------------------------------------------------------
    def _stage_workspace(self, workspace: Path) -> None:
        for item in sorted(self.task.workspace.rglob("*")):
            if item.is_dir():
                (workspace / item.relative_to(self.task.workspace)).mkdir(
                    parents=True, exist_ok=True
                )
                continue
            if item.is_symlink():
                continue
            target = workspace / item.relative_to(self.task.workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)

    def _has_service_script(self) -> bool:
        return (self.task.root / "services" / "start_services.sh").is_file()

    def _start_services(self) -> CommandResult:
        script = f"/benchmark/tasks/{self.task.task_id}/services/start_services.sh"
        command = (
            f"BENCH_TASK_DIR=/benchmark/tasks/{self.task.task_id} "
            f"BENCH_PRIVATE_DIR=/benchmark/private/{self.task.task_id} "
            f"BENCH_WORKDIR={REMOTE_WORKSPACE} "
            f"BENCH_OUTPUT_DIR={REMOTE_WORKSPACE}/outputs "
            f"bash {script}"
        )
        return self.backend.exec_in(
            self.container_name, command, timeout_seconds=self.config.startup_timeout_seconds
        )

    def _wait_healthy(self) -> str | None:
        probes: list[int] = [
            int(mock["port"])
            for mock in self.task.runtime_mocks.values()
            if mock.get("port") is not None
        ]
        if probes:
            for port in probes:
                if not self._probe_http(port):
                    return f"mock_service_unhealthy: port {port}"
            return None
        if self._has_service_script():
            # The task-local service script does not publish a port list to the adapter; the
            # authoritative signal is the process it started plus the audit directory it creates.
            probe = (
                "pgrep -f mock_api_server.py >/dev/null && "
                f"test -d {REMOTE_WORKSPACE}/outputs/mock_audit"
            )
            result = self.backend.exec_in(self.container_name, probe, timeout_seconds=30)
            if not result.ok:
                return "mock_service_unhealthy: task service did not start"
            return None
        return None

    def _probe_http(self, port: int) -> bool:
        result = self.backend.exec_in(
            self.container_name,
            f'python3 -c "import urllib.request;'
            f"urllib.request.urlopen('http://127.0.0.1:{port}/health', timeout=2).read()\"",
            timeout_seconds=30,
        )
        return result.ok


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


__all__ = [
    "EnvironmentConfig",
    "EnvironmentHandle",
    "NativeEnvironment",
    "REMOTE_OUTPUTS",
    "REMOTE_WORKSPACE",
]
