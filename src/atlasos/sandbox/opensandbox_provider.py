"""Thin Atlas adapter over the self-hosted OpenSandbox Python SDK.

The adapter translates the existing AnyCode ``SandboxProvider`` contract into OpenSandbox SDK
calls. It is not a second runtime, scheduler, policy engine, idempotency layer, or recovery
authority. Atlas M5 still decides which task owns which sandbox; OpenSandbox only supplies the
isolated Docker-backed machine.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from anycode.contracts.models import ContractError
from anycode.sandbox import (
    SandboxActionResult,
    SandboxCapabilities,
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateResult,
    SandboxEvidence,
    SandboxFileResult,
    SandboxHandle,
    SandboxHealth,
    SandboxOutputChunk,
    SandboxSpec,
)
from anycode.security.redaction import safe_exception_message

from atlasos.sandbox.models import SandboxLostError
from atlasos.sandbox.reattachment import (
    SandboxAttachFailed,
    SandboxNetworkPolicyUnavailable,
    SandboxNetworkPolicyViolation,
    SandboxProviderUnavailable,
    SandboxReattachmentObservation,
    SandboxRemoteNotFound,
    atlas_metadata_digest,
    compute_control_plane_digest,
    normalize_atlas_metadata,
    normalize_provider_created_at,
    normalize_remote_state,
)

_OPEN_SANDBOX_LIMITATIONS = (
    "Docker container isolation; actual strength depends on the local OpenSandbox and Docker "
    "runtime configuration.",
    "Network-none enforcement depends on the OpenSandbox egress sidecar; Atlas verifies it with "
    "an empirical egress probe.",
    "Point-in-time snapshots are not exposed through this adapter.",
    "Verified provider-instance reattachment is explicit; automatic cross-process recovery and "
    "ownership locking are not implemented.",
)


@dataclass(frozen=True)
class OpenSandboxOptions:
    endpoint: str = "localhost:8080"
    protocol: str = "http"
    timeout_minutes: int = 20
    ready_timeout_seconds: int = 180
    cpu: str = "1"
    memory: str = "1Gi"
    use_server_proxy: bool = True
    request_timeout_seconds: int = 60


class OpenSandboxProvider:
    """AnyCode SandboxProvider backed by a local OpenSandbox server."""

    def __init__(
        self,
        options: OpenSandboxOptions | None = None,
        *,
        api_key: str | None = None,
        sandbox_factory: Callable[[SandboxSpec], Awaitable[Any]] | None = None,
        health_probe: Callable[[], Awaitable[tuple[bool, str]]] | None = None,
    ) -> None:
        self.options = options or OpenSandboxOptions()
        self._api_key = api_key
        self._sandbox_factory = sandbox_factory
        self._health_probe = health_probe
        self._sandboxes: dict[str, Any] = {}

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            provider="opensandbox",
            isolation="container",
            networking="allowlist",
            persistent_filesystem=True,
            snapshots=False,
            command_streaming=False,
            cancellation=True,
            file_transfer=True,
            evidence=True,
            limitations=_OPEN_SANDBOX_LIMITATIONS,
        )

    def _connection_config(self) -> Any:
        try:
            from opensandbox.config import ConnectionConfig
        except ImportError as error:
            raise ImportError(
                'OpenSandboxProvider requires the optional "opensandbox" package. '
                'Install it with: pip install "atlasos[sandbox]"'
            ) from error
        return ConnectionConfig(
            domain=self.options.endpoint,
            protocol=self.options.protocol,
            api_key=self._api_key,
            use_server_proxy=self.options.use_server_proxy,
            request_timeout=timedelta(seconds=self.options.request_timeout_seconds),
        )

    def _network_policy(self, spec: SandboxSpec) -> Any | None:
        if spec.network == "unrestricted":
            return None
        try:
            from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule
        except ImportError as error:
            raise ImportError(
                "The installed OpenSandbox SDK has no NetworkPolicy model."
            ) from error
        if spec.network == "none":
            return NetworkPolicy(defaultAction="deny")
        targets = (*spec.allowed_domains, *spec.allowed_cidrs)
        if not targets:
            raise ValueError("network='allowlist' requires a domain or CIDR")
        return NetworkPolicy(
            defaultAction="deny",
            egress=[NetworkRule(action="allow", target=target) for target in targets],
        )

    @staticmethod
    def _metadata_value(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value)[:63]
        if not cleaned or not cleaned[0].isalnum():
            cleaned = f"atlas-{cleaned}"[:63]
        if not cleaned[-1].isalnum():
            cleaned = f"{cleaned}-atlas"[:63]
        return cleaned

    def _metadata(self, spec: SandboxSpec) -> dict[str, str]:
        metadata = {
            str(key): self._metadata_value(str(value)) for key, value in spec.labels.items()
        }
        metadata.update(
            {
                "atlas.run_id": self._metadata_value(spec.run_id),
                "atlas.task_id": self._metadata_value(spec.task_id or "none"),
                "atlas.correlation_id": self._metadata_value(spec.correlation_id),
                "atlas.provider": "opensandbox",
                "atlas.m5": "1",
                "atlas.config_digest": self._metadata_value(
                    str(spec.labels.get("atlas_config_digest", "none"))
                ),
            }
        )
        worker_id = spec.labels.get("atlas_worker_id")
        if worker_id is not None:
            metadata["atlas.worker_id"] = self._metadata_value(str(worker_id))
        return metadata

    async def _default_sandbox_factory(self, spec: SandboxSpec) -> Any:
        try:
            from opensandbox import Sandbox
        except ImportError as error:
            raise ImportError(
                'OpenSandboxProvider requires the optional "opensandbox" package. '
                'Install it with: pip install "atlasos[sandbox]"'
            ) from error
        if spec.network == "allowlist" and not (spec.allowed_domains or spec.allowed_cidrs):
            raise ValueError("network='allowlist' requires a domain or CIDR")
        return await Sandbox.create(
            spec.image,
            timeout=timedelta(minutes=self.options.timeout_minutes),
            ready_timeout=timedelta(seconds=self.options.ready_timeout_seconds),
            resource={"cpu": self.options.cpu, "memory": self.options.memory},
            metadata=self._metadata(spec),
            network_policy=self._network_policy(spec),
            connection_config=self._connection_config(),
        )

    async def _create_sandbox(self, spec: SandboxSpec) -> Any:
        if self._sandbox_factory is not None:
            return await self._sandbox_factory(spec)
        return await self._default_sandbox_factory(spec)

    def _sandbox(self, handle: SandboxHandle) -> Any:
        sandbox = self._sandboxes.get(handle.id)
        if sandbox is None:
            # Do not reconnect by id: M5 has not proven durable ownership of a fresh process's
            # handle. An in-process cache miss fails closed instead of silently reattaching.
            raise SandboxLostError(
                f"OpenSandbox handle {handle.id!r} is not owned by this runtime; "
                "verified reattachment is required"
            )
        return sandbox

    def continuity_identity_digest(self) -> str:
        """Stable control-plane identity; excludes credentials and client timeouts."""

        return compute_control_plane_digest(
            provider_name="opensandbox",
            base_url=self._connection_config().get_base_url(),
            use_server_proxy=self.options.use_server_proxy,
        )

    async def inspect_existing(self, sandbox_id: str) -> SandboxReattachmentObservation:
        """Inspect via the control plane only; never connect as part of inspection."""

        try:
            from opensandbox import SandboxManager
            from opensandbox.exceptions import (
                SandboxApiException,
                SandboxConnectionException,
                SandboxInternalException,
                SandboxTimeoutException,
            )
        except ImportError as error:
            raise SandboxProviderUnavailable(f"OpenSandbox SDK is unavailable: {error}") from error

        manager: Any | None = None
        try:
            manager = await SandboxManager.create(self._connection_config())
            info = await manager.get_sandbox_info(sandbox_id)
            return SandboxReattachmentObservation(
                sandbox_id=str(info.id),
                state=normalize_remote_state(str(info.status.state)),
                created_at=normalize_provider_created_at(info.created_at),
                metadata=normalize_atlas_metadata(info.metadata),
                metadata_digest=atlas_metadata_digest(info.metadata),
            )
        except SandboxApiException as error:
            if error.status_code in (404, 410):
                raise SandboxRemoteNotFound(
                    f"OpenSandbox sandbox {sandbox_id!r} does not exist"
                ) from error
            raise SandboxProviderUnavailable(
                f"OpenSandbox control plane could not inspect {sandbox_id!r}: {error}"
            ) from error
        except (
            SandboxConnectionException,
            SandboxTimeoutException,
            SandboxInternalException,
        ) as error:
            raise SandboxProviderUnavailable(
                f"OpenSandbox control plane is unavailable while inspecting {sandbox_id!r}: {error}"
            ) from error
        except Exception as error:  # noqa: BLE001 - normalize SDK-independent failures
            from opensandbox.exceptions import SandboxException

            if isinstance(error, SandboxException):
                raise SandboxProviderUnavailable(
                    f"OpenSandbox inspection failed for {sandbox_id!r}: {error}"
                ) from error
            raise SandboxProviderUnavailable(
                f"OpenSandbox inspection failed for {sandbox_id!r}: {error}"
            ) from error
        finally:
            if manager is not None:
                with contextlib.suppress(Exception):
                    await manager.close()

    async def attach_existing(self, sandbox_id: str) -> None:
        """Connect to a verified existing sandbox and install only the local handle cache."""

        try:
            from opensandbox import Sandbox
        except ImportError as error:
            raise SandboxAttachFailed(f"OpenSandbox SDK is unavailable: {error}") from error
        try:
            sandbox = await Sandbox.connect(
                sandbox_id,
                connection_config=self._connection_config(),
                connect_timeout=timedelta(seconds=self.options.ready_timeout_seconds),
            )
        except Exception as error:  # noqa: BLE001 - attach failure is a typed M5 failure
            raise SandboxAttachFailed(
                f"OpenSandbox could not connect to verified sandbox {sandbox_id!r}: {error}"
            ) from error
        self._sandboxes[sandbox_id] = sandbox

    async def detach_local(self, sandbox_id: str) -> None:
        """Close only local SDK resources; the remote sandbox is never destroyed here."""

        sandbox = self._sandboxes.pop(sandbox_id, None)
        if sandbox is None:
            return
        # Surface local-close failures: ownership release must fail closed rather than hand
        # off while the old SDK connection may still be live.
        await sandbox.close()

    async def verify_network_none(self, sandbox_id: str) -> None:
        """Read the effective sidecar policy and prove that no egress is allowed.

        This is a point-in-time check. It does not claim that the policy can never change later.
        """

        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            raise SandboxNetworkPolicyUnavailable(
                f"cannot verify network policy for sandbox {sandbox_id!r}: no connected client"
            )
        try:
            policy = await sandbox.get_egress_policy()
        except Exception as error:  # noqa: BLE001 - policy read failure is fail-closed
            raise SandboxNetworkPolicyUnavailable(
                f"cannot read effective network policy for sandbox {sandbox_id!r}: {error}"
            ) from error
        default_action = getattr(policy, "default_action", None)
        if default_action != "deny":
            raise SandboxNetworkPolicyViolation(
                f"sandbox {sandbox_id!r} network default action is "
                f"{default_action!r}, expected 'deny'"
            )
        rules = getattr(policy, "egress", None) or []
        for rule in rules:
            action = getattr(rule, "action", None)
            if action != "deny":
                raise SandboxNetworkPolicyViolation(
                    f"sandbox {sandbox_id!r} has a non-deny egress rule "
                    f"(action={action!r}); network=none requires no allow rules"
                )

    async def create(self, spec: SandboxSpec) -> SandboxCreateResult:
        if spec.snapshot:
            return SandboxCreateResult(
                ok=False,
                error=ContractError(
                    code="sandbox_snapshot_unsupported",
                    message=(
                        "The OpenSandbox adapter does not expose point-in-time snapshot restore."
                    ),
                ),
            )
        if spec.secret_references:
            return SandboxCreateResult(
                ok=False,
                error=ContractError(
                    code="sandbox_secrets_unsupported",
                    message=(
                        "OpenSandbox credential-vault references are out of scope for Atlas M5."
                    ),
                ),
            )
        if not spec.image:
            return SandboxCreateResult(
                ok=False,
                error=ContractError(
                    code="sandbox_image_required",
                    message="OpenSandbox requires a trusted image reference.",
                ),
            )
        try:
            sandbox = await self._create_sandbox(spec)
            sandbox_id = str(sandbox.id)
            self._sandboxes[sandbox_id] = sandbox
            handle = SandboxHandle(
                id=sandbox_id,
                provider="opensandbox",
                run_id=spec.run_id,
                task_id=spec.task_id,
                correlation_id=spec.correlation_id,
                context=spec.context,
                capabilities=self.capabilities(),
                metadata={
                    "runtime": "docker",
                    "network": spec.network,
                    "image": spec.image,
                },
            )
            return SandboxCreateResult(ok=True, handle=handle)
        except Exception as error:
            return SandboxCreateResult(
                ok=False,
                error=ContractError(
                    code="sandbox_create_failed",
                    message=safe_exception_message(error),
                    retryable=True,
                ),
            )

    @staticmethod
    def _execution_text(execution: Any, stream: str) -> str:
        messages = getattr(getattr(execution, "logs", None), stream, None) or []
        return "".join(str(getattr(message, "text", "")) for message in messages)

    async def execute(self, handle: SandboxHandle, command: SandboxCommand) -> SandboxCommandResult:
        try:
            from opensandbox.models.execd import RunCommandOpts
        except ImportError as error:
            return SandboxCommandResult(
                ok=False,
                error=ContractError(
                    code="sandbox_sdk_missing",
                    message=safe_exception_message(error),
                    retryable=True,
                ),
            )
        try:
            sandbox = self._sandbox(handle)
            opts = RunCommandOpts(
                working_directory=command.cwd,
                timeout=(
                    timedelta(seconds=command.timeout_seconds)
                    if command.timeout_seconds is not None
                    else None
                ),
                envs=command.environment or None,
            )
            execution = await sandbox.commands.run(command.command, opts=opts)
            stdout = self._execution_text(execution, "stdout")
            stderr = self._execution_text(execution, "stderr")
            exit_code = getattr(execution, "exit_code", None)
            error = getattr(execution, "error", None)
            if exit_code is None and error is not None:
                exit_code = getattr(error, "exit_code", None)
                if exit_code is None:
                    exit_code = 1
                message = str(getattr(error, "value", "") or "")
                if message:
                    stderr = f"{stderr}\n{message}".strip()
            if exit_code is None:
                exit_code = 0
            evidence = SandboxEvidence.from_bytes(
                "command",
                f"{stdout}\0{stderr}".encode(),
                metadata={"exit_code": exit_code},
            )
            # ``ok`` reports that the remote command ran and was observed; the exit code carries
            # command failure. Atlas' bash tool preserves non-zero output and marks the tool result
            # as an error itself.
            return SandboxCommandResult(
                ok=True,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                evidence=evidence,
            )
        except SandboxLostError:
            raise
        except Exception as error:
            return SandboxCommandResult(
                ok=False,
                error=ContractError(
                    code="sandbox_command_failed",
                    message=safe_exception_message(error),
                    retryable=True,
                ),
            )

    def stream(
        self, handle: SandboxHandle, command: SandboxCommand
    ) -> AsyncIterable[SandboxOutputChunk]:
        async def generate() -> AsyncIterator[SandboxOutputChunk]:
            result = await self.execute(handle, command)
            if result.error is not None:
                yield SandboxOutputChunk(stream="error", data=result.error.message, sequence=1)
                return
            sequence = 0
            for stream_name, data in (("stdout", result.stdout), ("stderr", result.stderr)):
                if data:
                    sequence += 1
                    yield SandboxOutputChunk(
                        stream=stream_name,  # type: ignore[arg-type]
                        data=data,
                        sequence=sequence,
                    )

        return generate()

    async def write_file(self, handle: SandboxHandle, path: str, data: bytes) -> SandboxFileResult:
        try:
            sandbox = self._sandbox(handle)
            await sandbox.files.write_file(path, data, mode=0o644)
            return SandboxFileResult(
                ok=True,
                evidence=SandboxEvidence.from_bytes("file.write", data, metadata={"path": path}),
            )
        except SandboxLostError:
            raise
        except Exception as error:
            return SandboxFileResult(
                ok=False,
                error=ContractError(
                    code="sandbox_file_write_failed",
                    message=safe_exception_message(error),
                ),
            )

    async def read_file(self, handle: SandboxHandle, path: str) -> SandboxFileResult:
        try:
            sandbox = self._sandbox(handle)
            data = bytes(await sandbox.files.read_bytes(path))
            return SandboxFileResult(
                ok=True,
                data=data,
                evidence=SandboxEvidence.from_bytes("file.read", data, metadata={"path": path}),
            )
        except SandboxLostError:
            raise
        except Exception as error:
            return SandboxFileResult(
                ok=False,
                error=ContractError(
                    code="sandbox_file_read_failed",
                    message=safe_exception_message(error),
                ),
            )

    async def cancel(self, handle: SandboxHandle) -> SandboxActionResult:
        try:
            sandbox = self._sandbox(handle)
            await sandbox.kill()
            await sandbox.close()
            self._sandboxes.pop(handle.id, None)
            return SandboxActionResult(
                ok=True,
                evidence=SandboxEvidence.from_bytes("sandbox.cancel", handle.id.encode()),
            )
        except SandboxLostError:
            raise
        except Exception as error:
            return SandboxActionResult(
                ok=False,
                error=ContractError(
                    code="sandbox_cancel_failed",
                    message=safe_exception_message(error),
                ),
            )

    async def snapshot(self, handle: SandboxHandle, name: str) -> SandboxActionResult:
        del handle, name
        return SandboxActionResult(
            ok=False,
            error=ContractError(
                code="sandbox_snapshot_unsupported",
                message="The OpenSandbox adapter does not expose point-in-time snapshots.",
            ),
        )

    async def destroy(self, handle: SandboxHandle) -> SandboxActionResult:
        try:
            sandbox = self._sandbox(handle)
            await sandbox.destroy()
            self._sandboxes.pop(handle.id, None)
            return SandboxActionResult(
                ok=True,
                evidence=SandboxEvidence.from_bytes("sandbox.destroy", handle.id.encode()),
            )
        except SandboxLostError:
            raise
        except Exception as error:
            return SandboxActionResult(
                ok=False,
                error=ContractError(
                    code="sandbox_destroy_failed",
                    message=safe_exception_message(error),
                ),
            )

    async def _default_health_probe(self) -> tuple[bool, str]:
        try:
            import httpx
        except ImportError as error:
            return False, safe_exception_message(error)
        url = f"{self.options.protocol}://{self.options.endpoint}/health"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
            if response.status_code == 200 and response.json().get("status") == "healthy":
                return True, "healthy"
            return False, f"health status {response.status_code}"
        except Exception as error:
            return False, safe_exception_message(error)

    async def health(self) -> SandboxHealth:
        probe = self._health_probe or self._default_health_probe
        try:
            healthy, message = await probe()
        except Exception as error:
            return SandboxHealth(status="unavailable", message=safe_exception_message(error))
        return SandboxHealth(status="healthy" if healthy else "unavailable", message=message)


__all__ = ["OpenSandboxOptions", "OpenSandboxProvider"]
