"""Hidden-oracle execution inside a disposable, network-disabled Sandbox.

The pre-R1-2H oracle ran ``pytest`` on the Host inside ``run_dir/oracle-workspace``, which meant
agent-generated Python was imported with the Host environment (including provider API keys) in
scope. This module removes that: the exported workspace and the hidden test files are uploaded into
a fresh sandbox, ``pytest`` runs there, and the sandbox is destroyed before the verdict is returned.

Everything that does not execute agent code still comes from
:func:`experiments.evaluation.oracle.static_oracle_checks`, so the two paths score identically.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.identity import ExecutionContext
from anycode.sandbox import SandboxCommand, SandboxProvider, SandboxSpec
from experiments.evaluation.fixtures import workspace_hashes
from experiments.evaluation.models import BenchmarkTask, OracleOutcome, canonical_digest
from experiments.evaluation.oracle import static_oracle_checks

#: Oracle execution modes. ``sandbox`` is the default; ``host`` exists only to reproduce pre-R1-2H
#: numbers and re-introduces Host code execution.
ORACLE_MODES: tuple[str, ...] = ("sandbox", "host")
ORACLE_MODE_SANDBOX = "sandbox"
ORACLE_MODE_HOST = "host"

HIDDEN_TESTS_PASSED = "HIDDEN_TESTS_PASSED"
HIDDEN_TESTS_FAILED = "HIDDEN_TESTS_FAILED"
TEST_COLLECTION_FAILED = "TEST_COLLECTION_FAILED"
NO_TESTS_COLLECTED = "NO_TESTS_COLLECTED"
ORACLE_TIMEOUT = "ORACLE_TIMEOUT"
SANDBOX_PROVISIONING_FAILURE = "SANDBOX_PROVISIONING_FAILURE"
SANDBOX_EXECUTION_FAILURE = "SANDBOX_EXECUTION_FAILURE"

#: Kinds that mean the deliverable is wrong. Everything else is an environment defect and must not
#: be reported as a model failure.
FUNCTIONAL_FAILURE_KINDS: frozenset[str] = frozenset({HIDDEN_TESTS_FAILED, TEST_COLLECTION_FAILED})

_IGNORED_PARTS = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules", "dist", "build"}
)
_MAX_FILES = 600
_MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class OracleSandboxConfig:
    image: str | None
    timeout_seconds: int = 180
    workspace: str = "/workspace/oracle"
    max_files: int = _MAX_FILES
    max_file_bytes: int = _MAX_FILE_BYTES


@dataclass(frozen=True)
class HiddenTestRun:
    kind: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    sandbox_id: str | None = None
    cleanup_failure: str | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.kind == HIDDEN_TESTS_PASSED

    @property
    def environment_failure(self) -> bool:
        return self.kind not in FUNCTIONAL_FAILURE_KINDS and self.kind != HIDDEN_TESTS_PASSED


def oracle_digests(
    task: BenchmarkTask,
    *,
    workspace: Path,
    execution_mode: str,
    image: str | None,
) -> dict[str, str]:
    """Three independent digests, keeping ``fixture_digest`` semantics untouched."""

    contract = canonical_digest(
        {
            "hidden_files": task.hidden_files,
            "required_artifacts": list(task.required_artifacts),
            "allowed_paths": list(task.allowed_paths),
            "forbidden_paths": list(task.forbidden_paths),
            "tool_expectations": task.tool_expectations,
        }
    )
    snapshot = canonical_digest(workspace_hashes(workspace))
    execution = canonical_digest(
        {
            "oracle_contract_digest": contract,
            "workspace_digest": snapshot,
            "execution_mode": execution_mode,
            "image": image,
        }
    )
    return {
        "oracle_contract_digest": contract,
        "oracle_fixture_digest": task.fixture_digest,
        "oracle_execution_digest": execution,
        "oracle_workspace_digest": snapshot,
    }


def classify_pytest_exit(returncode: int | None, stdout: str, stderr: str) -> str:
    """Map a pytest result onto a defect kind.

    Collection errors come from the agent's own modules (bad import, syntax error), so they stay a
    deliverable failure. A missing test session, a usage error or an internal error is an oracle
    environment problem and must not be charged to the model.
    """

    text = f"{stdout}\n{stderr}"
    if returncode == 0:
        return HIDDEN_TESTS_PASSED
    if "during collection" in text or "ERROR collecting" in text:
        return TEST_COLLECTION_FAILED
    if returncode == 1:
        return HIDDEN_TESTS_FAILED
    if returncode == 5:
        return NO_TESTS_COLLECTED
    if returncode in (2, 3, 4):
        return SANDBOX_EXECUTION_FAILURE
    return SANDBOX_EXECUTION_FAILURE


def _collect_upload(
    workspace: Path, hidden_files: dict[str, str], *, max_files: int, max_file_bytes: int
) -> tuple[list[tuple[str, bytes]], str | None]:
    """Build the upload list: exported workspace first, hidden tests last.

    Hidden test content never touches the exported workspace on disk; it exists only in memory here
    and inside the throwaway sandbox.
    """

    payload: list[tuple[str, bytes]] = []
    for item in sorted(workspace.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(workspace).as_posix()
        if _IGNORED_PARTS.intersection(relative.split("/")):
            continue
        data = item.read_bytes()
        if len(data) > max_file_bytes:
            return [], f"workspace file {relative} exceeds the oracle upload limit"
        payload.append((relative, data))
        if len(payload) > max_files:
            return [], "workspace exceeds the oracle file limit"
    for relative, content in sorted(hidden_files.items()):
        data = content.encode("utf-8")
        if len(data) > max_file_bytes:
            return [], f"hidden file {relative} exceeds the oracle upload limit"
        payload.append((relative, data))
        if len(payload) > max_files:
            return [], "oracle upload exceeds the file limit"
    return payload, None


def _oracle_context(run_id: str) -> ExecutionContext:
    return ExecutionContext(
        principal="system:oracle",
        tenant_scope="atlas-oracle",
        classification="internal",
        attributes={"run_id": run_id, "atlas_oracle": "1"},
    )


async def run_hidden_tests_in_sandbox(
    provider: SandboxProvider,
    *,
    task: BenchmarkTask,
    workspace: Path,
    run_id: str,
    config: OracleSandboxConfig,
) -> HiddenTestRun:
    """Upload, execute and destroy. No Host process ever imports the deliverable."""

    timings: dict[str, float] = {}
    started = time.perf_counter()
    upload, problem = await asyncio.to_thread(
        _collect_upload,
        workspace,
        task.hidden_files,
        max_files=config.max_files,
        max_file_bytes=config.max_file_bytes,
    )
    if problem is not None:
        timings["total_ms"] = _ms(started)
        return HiddenTestRun(
            kind=SANDBOX_PROVISIONING_FAILURE,
            returncode=None,
            stderr=problem,
            timings_ms=timings,
        )

    spec = SandboxSpec(
        run_id=f"{run_id}-oracle",
        task_id="oracle",
        correlation_id=f"{run_id}:oracle",
        context=_oracle_context(run_id),
        image=config.image,
        network="none",
        persistent=False,
        labels={"atlas_oracle": "1"},
    )
    created_at = time.perf_counter()
    try:
        created = await provider.create(spec)
    except Exception as error:  # noqa: BLE001 - any provider error is an environment defect
        timings["creation_ms"] = _ms(created_at)
        timings["total_ms"] = _ms(started)
        return HiddenTestRun(
            kind=SANDBOX_PROVISIONING_FAILURE,
            returncode=None,
            stderr=f"{type(error).__name__}: {error}",
            timings_ms=timings,
        )
    timings["creation_ms"] = _ms(created_at)
    if not created.ok or created.handle is None:
        timings["total_ms"] = _ms(started)
        return HiddenTestRun(
            kind=SANDBOX_PROVISIONING_FAILURE,
            returncode=None,
            stderr=f"sandbox creation refused: {created.error}",
            timings_ms=timings,
        )

    handle = created.handle
    handle_id = handle.id
    upload_started = time.perf_counter()
    upload_error: str | None = None
    for relative, data in upload:
        written = await provider.write_file(handle, f"{config.workspace}/{relative}", data)
        if not written.ok:
            upload_error = f"upload of {relative} failed: {written.error}"
            break
    timings["upload_ms"] = _ms(upload_started)
    if upload_error is not None:
        cleanup = await _destroy(provider, handle)
        timings["cleanup_ms"] = cleanup[1]
        timings["total_ms"] = _ms(started)
        return HiddenTestRun(
            kind=SANDBOX_PROVISIONING_FAILURE,
            returncode=None,
            stderr=upload_error,
            sandbox_id=handle_id,
            cleanup_failure=cleanup[0],
            timings_ms=timings,
        )

    execute_started = time.perf_counter()
    returncode: int | None = None
    stdout = ""
    stderr = ""
    kind = SANDBOX_EXECUTION_FAILURE
    try:
        result = await provider.execute(
            handle,
            SandboxCommand(
                command="python3 -m pytest -q --tb=short",
                cwd=config.workspace,
                environment={
                    "PYTHONPATH": config.workspace,
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                timeout_seconds=config.timeout_seconds,
            ),
        )
        returncode = result.exit_code
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if result.error is not None:
            kind = SANDBOX_EXECUTION_FAILURE
        else:
            kind = classify_pytest_exit(returncode, stdout, stderr)
    except TimeoutError:
        kind = ORACLE_TIMEOUT
        stderr = f"oracle execution exceeded {config.timeout_seconds}s"
    except Exception as error:  # noqa: BLE001
        kind = SANDBOX_EXECUTION_FAILURE
        stderr = f"{type(error).__name__}: {error}"
    timings["execution_ms"] = _ms(execute_started)

    cleanup_failure, cleanup_ms = await _destroy(provider, handle)
    timings["cleanup_ms"] = cleanup_ms
    timings["total_ms"] = _ms(started)
    return HiddenTestRun(
        kind=kind,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        sandbox_id=handle_id,
        cleanup_failure=cleanup_failure,
        timings_ms=timings,
    )


def oracle_failure_class(outcome: OracleOutcome) -> str | None:
    """Map an oracle verdict onto the run failure class, keeping model and environment apart.

    ``None`` means the oracle did not fail. An environment defect never becomes ``ORACLE_FAILURE``:
    a dead sandbox or a missing pytest session says nothing about the deliverable.
    """

    if outcome.passed:
        return None
    if outcome.environment_failure:
        return "ENVIRONMENT_FAILURE"
    return "ORACLE_FAILURE"


async def _destroy(provider: SandboxProvider, handle: Any) -> tuple[str | None, float]:
    started = time.perf_counter()
    try:
        result = await provider.destroy(handle)
    except Exception as error:  # noqa: BLE001 - a failed destroy must be reported, not hidden
        return f"{type(error).__name__}: {error}", _ms(started)
    if result is not None and getattr(result, "ok", True) is False:
        return f"provider refused destroy: {getattr(result, 'error', None)}", _ms(started)
    return None, _ms(started)


async def evaluate_oracle_isolated(
    task: BenchmarkTask,
    *,
    workspace: Path,
    baseline_hashes: dict[str, str],
    tool_calls: list[dict[str, Any]],
    worker_results: dict[str, Any],
    run_dir: Path,
    provider: SandboxProvider,
    config: OracleSandboxConfig,
    host_fixture_unchanged: bool = True,
    run_id: str = "evaluation",
) -> OracleOutcome:
    """Sandboxed equivalent of :func:`experiments.evaluation.oracle.evaluate_oracle`."""

    checks, details, reasons = static_oracle_checks(
        task,
        workspace=workspace,
        baseline_hashes=baseline_hashes,
        tool_calls=tool_calls,
        worker_results=worker_results,
        host_fixture_unchanged=host_fixture_unchanged,
    )
    digests = oracle_digests(
        task, workspace=workspace, execution_mode=ORACLE_MODE_SANDBOX, image=config.image
    )
    if not task.hidden_files:
        return OracleOutcome(
            passed=all(checks.values()),
            checks=checks,
            details={**details, "digests": digests},
            failure_reasons=reasons,
            execution_mode=ORACLE_MODE_SANDBOX,
            digests=digests,
            timings_ms={"total_ms": 0.0},
        )

    run = await run_hidden_tests_in_sandbox(
        provider, task=task, workspace=workspace, run_id=run_id, config=config
    )
    log = f"=== stdout ===\n{run.stdout}\n=== stderr ===\n{run.stderr}\n"
    await asyncio.to_thread(_write_evidence, run_dir, log, run, digests)

    checks["hidden_tests_passed"] = run.passed
    details["hidden_tests"] = {
        "returncode": run.returncode,
        "stdout_tail": run.stdout[-4000:],
        "stderr_tail": run.stderr[-4000:],
        "failure_kind": run.kind,
        "sandbox_id": run.sandbox_id,
    }
    details["digests"] = digests
    details["oracle_execution"] = "sandbox"
    if run.environment_failure:
        reasons.append(f"oracle environment failure: {run.kind}")
    elif not run.passed:
        # Same wording as the Host oracle so historical and new failure_reasons stay comparable.
        reasons.append(f"hidden tests failed with exit code {run.returncode}")

    passed = all(checks.values()) and not run.environment_failure
    return OracleOutcome(
        passed=passed,
        checks=checks,
        details=details,
        failure_reasons=[] if passed else reasons,
        execution_mode=ORACLE_MODE_SANDBOX,
        failure_kind=run.kind,
        environment_failure=run.environment_failure,
        cleanup_failure=run.cleanup_failure,
        digests=digests,
        timings_ms=run.timings_ms,
    )


def _write_evidence(run_dir: Path, log: str, run: HiddenTestRun, digests: dict[str, str]) -> None:
    """Persist the sandbox evidence (run in a worker thread, never on the event loop)."""

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "oracle.log").write_text(log, encoding="utf-8")
    with contextlib.suppress(Exception):
        (run_dir / "oracle_sandbox.json").write_text(
            _json(
                {
                    "kind": run.kind,
                    "returncode": run.returncode,
                    "sandbox_id": run.sandbox_id,
                    "cleanup_failure": run.cleanup_failure,
                    "timings_ms": run.timings_ms,
                    "digests": digests,
                }
            ),
            encoding="utf-8",
        )


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


__all__ = [
    "FUNCTIONAL_FAILURE_KINDS",
    "HIDDEN_TESTS_FAILED",
    "HIDDEN_TESTS_PASSED",
    "NO_TESTS_COLLECTED",
    "ORACLE_MODES",
    "ORACLE_MODE_HOST",
    "ORACLE_MODE_SANDBOX",
    "ORACLE_TIMEOUT",
    "OracleSandboxConfig",
    "SANDBOX_EXECUTION_FAILURE",
    "SANDBOX_PROVISIONING_FAILURE",
    "TEST_COLLECTION_FAILED",
    "HiddenTestRun",
    "classify_pytest_exit",
    "evaluate_oracle_isolated",
    "oracle_digests",
    "oracle_failure_class",
    "run_hidden_tests_in_sandbox",
]
