"""Container backend for the native benchmark environment.

Docker lives inside WSL on this host, so the adapter talks to it through ``wsl.exe``. The backend is
a narrow protocol: it can start a container, exec inside it, copy files, list published ports and
destroy it. Tests inject a fake; production uses :class:`WslDockerBackend`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ContainerError(RuntimeError):
    """The container runtime refused a command."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    read_only: bool = False

    def render(self, translate: Any) -> str:
        host = translate(self.source)
        suffix = ":ro" if self.read_only else ""
        return f"{host}:{self.target}{suffix}"


class ContainerBackend(Protocol):
    def run_container(
        self,
        *,
        name: str,
        image: str,
        mounts: Sequence[Mount],
        ports: Sequence[int],
        env: Mapping[str, str],
        labels: Mapping[str, str],
        platform: str | None = None,
    ) -> str: ...

    def exec_in(
        self,
        name: str,
        command: str,
        *,
        timeout_seconds: int = 300,
        user: str | None = None,
    ) -> CommandResult: ...

    def copy_from(self, name: str, remote_path: str, local_path: Path) -> None: ...

    def copy_to(self, name: str, local_path: Path, remote_path: str) -> None: ...

    def container_exists(self, name: str) -> bool: ...

    def destroy(self, name: str) -> None: ...

    def published_port(self, name: str, container_port: int) -> int | None: ...


class WslDockerBackend:
    """Docker inside a WSL distribution."""

    def __init__(self, *, distro: str = "Ubuntu", docker: str = "docker") -> None:
        self.distro = distro
        self.docker = docker

    # -- path translation -------------------------------------------------
    @staticmethod
    def to_wsl_path(path: Path | str) -> str:
        text = str(path)
        if len(text) >= 2 and text[1] == ":":
            drive = text[0].lower()
            rest = text[2:].replace("\\", "/")
            return f"/mnt/{drive}{rest}"
        return text.replace("\\", "/")

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 600,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["wsl.exe", "-d", self.distro, "--", *args]
        try:
            return subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ContainerError(f"container command timed out after {timeout}s: {args}") from error
        except OSError as error:
            raise ContainerError(f"cannot reach WSL docker: {error}") from error

    # -- protocol ---------------------------------------------------------
    def run_container(
        self,
        *,
        name: str,
        image: str,
        mounts: Sequence[Mount],
        ports: Sequence[int],
        env: Mapping[str, str],
        labels: Mapping[str, str],
        platform: str | None = None,
    ) -> str:
        args = [self.docker, "run", "-d", "--name", name]
        for key, value in labels.items():
            args.extend(["--label", f"{key}={value}"])
        if platform:
            args.extend(["--platform", platform])
        for mount in mounts:
            args.extend(["-v", mount.render(self.to_wsl_path)])
        for port in ports:
            args.extend(["-p", f"127.0.0.1:0:{port}"])
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        args.extend([image, "/bin/bash", "-lc", "tail -f /dev/null"])
        result = self._run(args, timeout=300)
        if result.returncode != 0:
            raise ContainerError(
                "docker run failed: "
                + (result.stderr or result.stdout).decode("utf-8", "replace")[:800]
            )
        return result.stdout.decode("utf-8", "replace").strip()

    def exec_in(
        self,
        name: str,
        command: str,
        *,
        timeout_seconds: int = 300,
        user: str | None = None,
    ) -> CommandResult:
        args = [self.docker, "exec"]
        if user:
            args.extend(["--user", user])
        args.extend([name, "bash", "-lc", command])
        result = self._run(args, timeout=timeout_seconds + 10)
        return CommandResult(
            exit_code=result.returncode,
            stdout=result.stdout.decode("utf-8", "replace"),
            stderr=result.stderr.decode("utf-8", "replace"),
        )

    def copy_from(self, name: str, remote_path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            [self.docker, "cp", f"{name}:{remote_path}", self.to_wsl_path(local_path)]
        )
        if result.returncode != 0:
            raise ContainerError(
                f"docker cp out failed: {result.stderr.decode('utf-8', 'replace')[:400]}"
            )

    def copy_to(self, name: str, local_path: Path, remote_path: str) -> None:
        result = self._run(
            [self.docker, "cp", self.to_wsl_path(local_path), f"{name}:{remote_path}"]
        )
        if result.returncode != 0:
            raise ContainerError(
                f"docker cp in failed: {result.stderr.decode('utf-8', 'replace')[:400]}"
            )

    def container_exists(self, name: str) -> bool:
        result = self._run([self.docker, "ps", "-a", "--format", "{{.Names}}"], timeout=60)
        if result.returncode != 0:
            raise ContainerError(
                f"docker ps failed: {result.stderr.decode('utf-8', 'replace')[:400]}"
            )
        return name in {
            line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines()
        }

    def destroy(self, name: str) -> None:
        result = self._run([self.docker, "rm", "-f", name], timeout=120)
        if result.returncode != 0:
            raise ContainerError(
                f"docker rm failed: {result.stderr.decode('utf-8', 'replace')[:400]}"
            )

    def published_port(self, name: str, container_port: int) -> int | None:
        result = self._run([self.docker, "port", name, f"{container_port}/tcp"], timeout=60)
        if result.returncode != 0:
            return None
        text = result.stdout.decode("utf-8", "replace").strip().splitlines()
        if not text:
            return None
        _, _, port = text[0].rpartition(":")
        return int(port) if port.isdigit() else None


class WslShell:
    """Run a host shell command inside WSL (the Host for the Docker daemon here)."""

    def __init__(self, *, distro: str = "Ubuntu") -> None:
        self.distro = distro

    def run(self, command: str, *, timeout: int) -> CommandResult:
        try:
            result = subprocess.run(
                ["wsl.exe", "-d", self.distro, "--", "bash", "-lc", command],
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise ContainerError(f"host command timed out after {timeout}s") from error
        except OSError as error:
            raise ContainerError(f"cannot reach WSL: {error}") from error
        return CommandResult(
            exit_code=result.returncode,
            stdout=result.stdout.decode("utf-8", "replace"),
            stderr=result.stderr.decode("utf-8", "replace"),
        )


class BashGrader:
    """Run the original ``grader/run.sh`` with the host bash (inside WSL here)."""

    def __init__(self, shell: WslShell | None = None) -> None:
        self.shell = shell or WslShell()

    def run(
        self, *, task_dir: Path, output_dir: Path, reward_path: Path, timeout: int
    ) -> CommandResult:
        translate = WslDockerBackend.to_wsl_path
        script = translate(task_dir / "grader" / "run.sh")
        command = (
            f"OUTPUT_DIR={translate(output_dir)} REWARD_JSON={translate(reward_path)} bash {script}"
        )
        return self.shell.run(command, timeout=timeout)


__all__ = [
    "BashGrader",
    "CommandResult",
    "ContainerBackend",
    "ContainerError",
    "Mount",
    "WslDockerBackend",
    "WslShell",
]
