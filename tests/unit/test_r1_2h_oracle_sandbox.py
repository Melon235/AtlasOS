"""R1-2H: the hidden oracle must run in a disposable sandbox, never on the Host."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from anycode.sandbox import (
    SandboxActionResult,
    SandboxCapabilities,
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateResult,
    SandboxFileResult,
    SandboxHandle,
    SandboxSpec,
)
from experiments.evaluation.fixtures import materialize_fixture
from experiments.evaluation.models import OracleOutcome
from experiments.evaluation.sandboxed_oracle import (
    HIDDEN_TESTS_FAILED,
    HIDDEN_TESTS_PASSED,
    NO_TESTS_COLLECTED,
    ORACLE_TIMEOUT,
    SANDBOX_EXECUTION_FAILURE,
    SANDBOX_PROVISIONING_FAILURE,
    TEST_COLLECTION_FAILED,
    OracleSandboxConfig,
    classify_pytest_exit,
    evaluate_oracle_isolated,
    oracle_digests,
    oracle_failure_class,
    run_hidden_tests_in_sandbox,
)
from experiments.evaluation.stage_r05_tasks import definition_by_id, variants

#: Pinned: R1-2H must not change the historical fixture digest algorithm.
P2_FIXTURE_DIGEST = "2ea6eba85296ab60ab8e32de7fbb13ed9f8e51051058ffc69acf93a80ee68dbd"


class ScriptedProvider:
    """Deterministic provider: records the upload, returns a scripted pytest result."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "3 passed in 0.01s\n",
        stderr: str = "",
        create_error: Exception | None = None,
        create_ok: bool = True,
        execute_error: Exception | None = None,
        execute_error_result: SandboxCommandResult | None = None,
        destroy_ok: bool = True,
        destroy_error: Exception | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.create_error = create_error
        self.create_ok = create_ok
        self.execute_error = execute_error
        self.execute_error_result = execute_error_result
        self.destroy_ok = destroy_ok
        self.destroy_error = destroy_error
        self.specs: list[SandboxSpec] = []
        self.uploads: dict[str, bytes] = {}
        self.commands: list[SandboxCommand] = []
        self.destroyed: list[str] = []
        self.create_count = 0

    async def create(self, spec: SandboxSpec) -> SandboxCreateResult:
        self.specs.append(spec)
        if self.create_error is not None:
            raise self.create_error
        if not self.create_ok:
            return SandboxCreateResult(ok=False, error=None)
        self.create_count += 1
        return SandboxCreateResult(
            ok=True,
            handle=SandboxHandle(
                id=f"oracle-sandbox-{self.create_count}",
                provider="fake",
                run_id=spec.run_id,
                task_id=spec.task_id,
                correlation_id=spec.correlation_id,
                context=spec.context,
                capabilities=SandboxCapabilities(
                    provider="fake",
                    isolation="container",
                    networking="none",
                    persistent_filesystem=False,
                    snapshots=False,
                    command_streaming=False,
                    cancellation=True,
                    file_transfer=True,
                    evidence=False,
                ),
            ),
        )

    async def write_file(self, handle: SandboxHandle, path: str, data: bytes) -> SandboxFileResult:
        del handle
        self.uploads[path] = data
        return SandboxFileResult(ok=True, data=data)

    async def execute(self, handle: SandboxHandle, command: SandboxCommand) -> SandboxCommandResult:
        del handle
        self.commands.append(command)
        if self.execute_error is not None:
            raise self.execute_error
        if self.execute_error_result is not None:
            return self.execute_error_result
        return SandboxCommandResult(
            ok=True, exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr
        )

    async def destroy(self, handle: SandboxHandle) -> SandboxActionResult:
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append(handle.id)
        return SandboxActionResult(ok=self.destroy_ok)


def task():
    return variants(definition_by_id("r05-p2-modules"))[0]


def _workspace(tmp_path: Path):
    benchmark = task()
    workspace = materialize_fixture(benchmark, tmp_path / "workspace")
    baseline = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in benchmark.visible_files.items()
    }
    return benchmark, workspace, baseline


def _run(tmp_path: Path, provider: ScriptedProvider, *, benchmark=None, baseline=None):
    benchmark = benchmark or task()
    workspace = materialize_fixture(benchmark, tmp_path / "workspace")
    baseline = baseline or {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in benchmark.visible_files.items()
    }
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    outcome = asyncio.run(
        evaluate_oracle_isolated(
            benchmark,
            workspace=workspace,
            baseline_hashes=baseline,
            tool_calls=[],
            worker_results={},
            run_dir=run_dir,
            provider=provider,  # type: ignore[arg-type]
            config=OracleSandboxConfig(image="python:3.12"),
            run_id="run-r12h",
        )
    )
    return outcome, workspace, run_dir


def test_hidden_tests_pass_inside_the_sandbox(tmp_path: Path) -> None:
    outcome, _, _ = _run(tmp_path, ScriptedProvider(exit_code=0))
    assert outcome.passed is True
    assert outcome.checks["hidden_tests_passed"] is True
    assert outcome.execution_mode == "sandbox"
    assert outcome.failure_kind == HIDDEN_TESTS_PASSED
    assert outcome.environment_failure is False


def test_assertion_failure_is_a_deliverable_defect(tmp_path: Path) -> None:
    provider = ScriptedProvider(exit_code=1, stdout="1 failed, 2 passed\n")
    outcome, _, _ = _run(tmp_path, provider)
    assert outcome.passed is False
    assert outcome.failure_kind == HIDDEN_TESTS_FAILED
    assert outcome.environment_failure is False
    assert outcome.failure_reasons == ["hidden tests failed with exit code 1"]
    assert oracle_failure_class(outcome) == "ORACLE_FAILURE"


def test_missing_module_is_reported_as_a_collection_failure(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        exit_code=1,
        stdout=(
            "ERROR collecting tests/test_hidden_p2.py\n"
            "ModuleNotFoundError: No module named 'ledger'\n"
        ),
    )
    outcome, _, _ = _run(tmp_path, provider)
    assert outcome.passed is False
    assert outcome.failure_kind == TEST_COLLECTION_FAILED
    assert outcome.environment_failure is False


def test_syntax_error_in_the_deliverable_is_a_deliverable_defect(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        exit_code=2,
        stdout="ERROR collecting tests/test_hidden_p2.py\nSyntaxError: invalid syntax\n",
    )
    outcome, _, _ = _run(tmp_path, provider)
    assert outcome.failure_kind == TEST_COLLECTION_FAILED
    assert outcome.environment_failure is False


def test_sandbox_creation_failure_is_environmental() -> None:
    provider = ScriptedProvider(create_error=RuntimeError("no capacity"))
    run = asyncio.run(
        run_hidden_tests_in_sandbox(
            provider,  # type: ignore[arg-type]
            task=task(),
            workspace=Path("."),
            run_id="run-r12h",
            config=OracleSandboxConfig(image=None),
        )
    )
    assert run.kind == SANDBOX_PROVISIONING_FAILURE
    assert run.environment_failure is True
    assert provider.destroyed == []


def test_refused_creation_is_environmental(tmp_path: Path) -> None:
    outcome, _, _ = _run(tmp_path, ScriptedProvider(create_ok=False))
    assert outcome.passed is False
    assert outcome.environment_failure is True
    assert oracle_failure_class(outcome) == "ENVIRONMENT_FAILURE"


def test_timeout_is_environmental_not_a_model_failure(tmp_path: Path) -> None:
    provider = ScriptedProvider(execute_error=TimeoutError("timed out"))
    outcome, _, _ = _run(tmp_path, provider)
    assert outcome.failure_kind == ORACLE_TIMEOUT
    assert outcome.environment_failure is True
    assert oracle_failure_class(outcome) == "ENVIRONMENT_FAILURE"
    assert provider.destroyed  # the sandbox is still cleaned up


def test_no_tests_collected_is_environmental(tmp_path: Path) -> None:
    outcome, _, _ = _run(tmp_path, ScriptedProvider(exit_code=5, stdout="no tests ran\n"))
    assert outcome.failure_kind == NO_TESTS_COLLECTED
    assert outcome.environment_failure is True


def test_destroy_failure_is_reported_and_never_silent(tmp_path: Path) -> None:
    provider = ScriptedProvider(destroy_error=RuntimeError("destroy refused"))
    outcome, _, _ = _run(tmp_path, provider)
    assert outcome.cleanup_failure is not None
    assert "destroy refused" in outcome.cleanup_failure
    # the verdict itself is still the pytest verdict
    assert outcome.passed is True


def test_destroy_refusal_without_exception_is_reported(tmp_path: Path) -> None:
    outcome, _, _ = _run(tmp_path, ScriptedProvider(destroy_ok=False))
    assert outcome.cleanup_failure is not None


def test_hidden_tests_never_reach_the_exported_workspace(tmp_path: Path) -> None:
    benchmark, workspace, baseline = _workspace(tmp_path)
    before = {item.relative_to(workspace).as_posix() for item in workspace.rglob("*")}
    provider = ScriptedProvider(exit_code=0)
    outcome, _, _ = _run(tmp_path, provider, benchmark=benchmark, baseline=baseline)
    after = {item.relative_to(workspace).as_posix() for item in workspace.rglob("*")}
    assert before == after
    assert not (workspace / "tests").exists()
    uploaded = set(provider.uploads)
    hidden = set(benchmark.hidden_files)
    assert hidden, "fixture must have hidden files for this test to be meaningful"
    for relative in hidden:
        assert f"/workspace/oracle/{relative}" in uploaded
        assert (workspace / relative).exists() is False
    assert outcome.passed is True


def test_host_never_executes_agent_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"Host executed a subprocess: {args} {kwargs}")

    monkeypatch.setattr("subprocess.run", _boom)
    monkeypatch.setattr("subprocess.Popen", _boom)
    outcome, _, _ = _run(tmp_path, ScriptedProvider(exit_code=0))
    assert outcome.passed is True


def test_no_host_secret_reaches_the_oracle_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-secret")
    provider = ScriptedProvider(exit_code=0)
    outcome, _, _ = _run(tmp_path, provider)
    assert outcome.passed is True
    spec = provider.specs[0]
    assert spec.secret_references == {}
    assert spec.network == "none"
    assert spec.persistent is False
    assert spec.image == "python:3.12"
    command = provider.commands[0]
    assert set(command.environment) == {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    assert "sk-live-secret" not in repr(command)


def test_oracle_sandbox_is_independent_from_the_worker_sandbox(tmp_path: Path) -> None:
    provider = ScriptedProvider(exit_code=0)
    _run(tmp_path, provider)
    spec = provider.specs[0]
    assert spec.run_id == "run-r12h-oracle"
    assert spec.task_id == "oracle"
    assert spec.labels == {"atlas_oracle": "1"}
    assert provider.create_count == 1
    assert provider.destroyed == ["oracle-sandbox-1"]


def test_oracle_outcome_stays_backward_compatible(tmp_path: Path) -> None:
    outcome, _, _ = _run(tmp_path, ScriptedProvider(exit_code=1, stdout="1 failed\n"))
    payload = outcome.as_dict()
    for key in ("passed", "checks", "details", "failure_reasons"):
        assert key in payload
    assert set(payload["checks"]) >= {
        "host_fixture_unchanged",
        "forbidden_paths_unchanged",
        "only_allowed_paths_changed",
        "required_artifacts_present",
        "hidden_tests_passed",
    }
    assert payload["details"]["hidden_tests"]["returncode"] == 1
    assert payload["execution_mode"] == "sandbox"


def test_legacy_outcome_defaults_stay_host_compatible() -> None:
    """The dataclass default is the pre-R1-2H shape, so old call sites keep their meaning."""

    outcome = OracleOutcome(passed=True)
    assert isinstance(outcome, OracleOutcome)
    assert outcome.execution_mode == "host"
    assert outcome.digests == {}
    assert outcome.timings_ms == {}
    assert outcome.environment_failure is False
    assert outcome.as_dict()["passed"] is True


def test_fixture_digest_is_unchanged_and_new_digests_are_additive(tmp_path: Path) -> None:
    benchmark, workspace, _ = _workspace(tmp_path)
    digests = oracle_digests(
        benchmark, workspace=workspace, execution_mode="sandbox", image="python:3.12"
    )
    assert digests["oracle_fixture_digest"] == benchmark.fixture_digest
    assert benchmark.fixture_digest == P2_FIXTURE_DIGEST
    assert set(digests) == {
        "oracle_contract_digest",
        "oracle_fixture_digest",
        "oracle_execution_digest",
        "oracle_workspace_digest",
    }
    # recomputation is stable and independent of the image only for the contract digest
    other = oracle_digests(
        benchmark, workspace=workspace, execution_mode="sandbox", image="python:3.11"
    )
    assert other["oracle_contract_digest"] == digests["oracle_contract_digest"]
    assert other["oracle_execution_digest"] != digests["oracle_execution_digest"]


@pytest.mark.parametrize(
    ("returncode", "text", "expected"),
    [
        (0, "", HIDDEN_TESTS_PASSED),
        (1, "1 failed", HIDDEN_TESTS_FAILED),
        (2, "!!! Interrupted: 1 error during collection !!!", TEST_COLLECTION_FAILED),
        (5, "no tests ran", NO_TESTS_COLLECTED),
        (3, "internal error", SANDBOX_EXECUTION_FAILURE),
        (4, "usage error", SANDBOX_EXECUTION_FAILURE),
    ],
)
def test_pytest_exit_classification(returncode: int, text: str, expected: str) -> None:
    assert classify_pytest_exit(returncode, text, "") == expected


def test_provider_command_failure_is_environmental(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        execute_error_result=SandboxCommandResult(
            ok=False, exit_code=None, error=None, stderr="transport down"
        )
    )
    outcome, _, _ = _run(tmp_path, provider)
    assert outcome.environment_failure is True
    assert outcome.failure_kind == SANDBOX_EXECUTION_FAILURE


def test_verification_layer_is_independent_of_the_oracle_sandbox() -> None:
    import atlasos.verification.engine as verification_engine

    source = Path(verification_engine.__file__).read_text(encoding="utf-8")
    assert "sandboxed_oracle" not in source
    assert "evaluate_oracle" not in source


def test_runner_defaults_to_the_isolated_oracle() -> None:
    from experiments.evaluation.runner import RunnerSettings

    settings = RunnerSettings()
    assert settings.oracle_mode == "sandbox"
    assert settings.digest() != replace(settings, oracle_mode="host").digest()
