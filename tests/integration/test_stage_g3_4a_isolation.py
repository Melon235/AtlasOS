"""Stage G3-4a: evaluation-side isolation of private artifacts from the worker sandbox.

These tests use the fake sandbox provider and a command-recording stub, so they never touch a real
container and never call a model. The live variant is
``tests/integration/test_stage_g3_4a_isolation_live.py``.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from anycode.sandbox import SandboxCommand, SandboxCommandResult
from experiments.evaluation.fixtures import (
    materialize_fixture,
    materialize_oracle,
    stage_git_repo,
    workspace_hashes,
)
from experiments.evaluation.models import BenchmarkTask, WorkerStep
from experiments.evaluation.oracle import evaluate_oracle
from experiments.evaluation.stage_g3_4a_integrity import (
    FIXTURE_COMMIT_LABEL,
    STAGING_BUNDLE,
    canary_for,
    enforce_evaluation_isolation,
)

CANARY_SEED = "stage-g3-4a-oracle"
PRIVATE_TEST = "tests/test_hidden_canary.py"


def _task(sample_id: str = "g34a-sample") -> BenchmarkTask:
    return BenchmarkTask(
        sample_id=sample_id,
        category="multi_worker",
        mode="coding",
        prompt="Implement add(a, b) in calc.py.",
        target="hidden oracle passes",
        visible_files={
            "README.md": "# project\n",
            "calc.py": "def add(a: int, b: int) -> int:\n    raise NotImplementedError\n",
            "tests/test_public.py": "from calc import add\n\n\ndef test_add() -> None:\n"
            "    assert callable(add)\n",
            "pyproject.toml": "[project]\nname = 'demo'\n",
        },
        hidden_files={
            PRIVATE_TEST: (
                "from calc import add\n\n\n"
                f"def test_hidden() -> None:\n    assert add(2, 3) == 5\n"
                # unique marker so a leak is detectable without relying on a file name
                f"    # {canary_for(CANARY_SEED)}\n"
            )
        },
        workers=(WorkerStep("solo", "g34a-solo", "Implement calc.py add."),),
        allowed_paths=("calc.py",),
        forbidden_paths=("tests/**",),
        required_artifacts=("calc.py",),
    )


def _fixture_repo(root: Path) -> Path:
    repo = root / "fixture"
    materialize_fixture(_task(), repo)
    stage_git_repo(repo, label=FIXTURE_COMMIT_LABEL)
    return repo


@dataclass
class _StubProvider:
    """Records commands and simulates an empty sandbox (no staging bundle)."""

    responses: dict[str, str] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    bundle_present: bool = True

    async def execute(self, handle: Any, command: SandboxCommand) -> SandboxCommandResult:
        del handle
        self.commands.append(command.command)
        text = command.command
        if text.startswith("test -e"):
            present = self.bundle_present
            return SandboxCommandResult(
                ok=True, exit_code=0, stdout="present\n" if present else "absent\n"
            )
        if text.startswith("rm -f "):
            self.bundle_present = False
            return SandboxCommandResult(ok=True, exit_code=0, stdout="")
        if text.startswith("find /"):
            return SandboxCommandResult(ok=True, exit_code=0, stdout="")
        return SandboxCommandResult(ok=True, exit_code=0, stdout="")


# --- Test A: public workspace stays visible ------------------------------------------------


def test_public_workspace_is_staged_for_the_worker(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    staged = set(workspace_hashes(repo))

    assert {"README.md", "calc.py", "tests/test_public.py", "pyproject.toml"} <= staged
    assert PRIVATE_TEST not in staged
    assert not (repo / "tests" / "test_hidden_canary.py").exists()


# --- Test B: the private oracle never enters the fixture ------------------------------------


def test_private_oracle_is_materialized_only_outside_the_fixture(tmp_path: Path) -> None:
    task = _task()
    repo = _fixture_repo(tmp_path)
    oracle_dir = materialize_oracle(task, tmp_path / "oracle-workspace")
    canary = canary_for(CANARY_SEED)

    assert (oracle_dir / PRIVATE_TEST).is_file()
    assert canary in (oracle_dir / PRIVATE_TEST).read_text(encoding="utf-8")
    # Nothing under the worker-visible fixture may carry the canary.
    for path in repo.rglob("*"):
        if path.is_file():
            assert canary not in path.read_text(encoding="utf-8", errors="replace")


# --- Test C: grading artifacts are not part of any staged file -----------------------------


def test_grading_logs_are_not_staged_into_the_fixture(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    canary = canary_for(CANARY_SEED)

    grading_log = tmp_path / "run" / "oracle.log"
    grading_log.parent.mkdir(parents=True)
    grading_log.write_text(f"=== stdout ===\n{canary}\n1 passed\n", encoding="utf-8")

    haystack = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in repo.rglob("*")
        if path.is_file()
    )
    assert canary not in haystack
    # The harness keeps its own log readable on the trusted side.
    assert canary in grading_log.read_text(encoding="utf-8")


# --- Test D: two runs do not share staging state -------------------------------------------


def test_runs_are_isolated_by_directory(tmp_path: Path) -> None:
    task = _task()
    run_a = tmp_path / "runs" / "a"
    run_b = tmp_path / "runs" / "b"
    for run in (run_a, run_b):
        run.mkdir(parents=True)
        materialize_fixture(task, run / "fixture")
        materialize_oracle(task, run / "oracle-workspace")

    canary_a = canary_for("run-a")
    canary_b = canary_for("run-b")
    (run_a / "oracle-workspace" / PRIVATE_TEST).write_text(canary_a, encoding="utf-8")
    (run_b / "oracle-workspace" / PRIVATE_TEST).write_text(canary_b, encoding="utf-8")

    assert canary_a not in "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (run_b / "fixture").rglob("*")
        if path.is_file()
    )
    assert canary_b not in "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (run_a / "fixture").rglob("*")
        if path.is_file()
    )


# --- Test E: the sandbox still supports normal temporary-file work -------------------------


def test_normal_posix_temp_workflow_still_works(tmp_path: Path) -> None:
    from anycode.identity.context import ExecutionContext
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    from atlasos.sandbox import SandboxProjectConfig
    from atlasos.sandbox.models import trusted_sandbox_spec

    provider = FakeSandboxProvider(tmp_path / "provider")
    config = SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef")
    spec = trusted_sandbox_spec(
        run_id="run-g34a",
        task_id="task",
        worker_id="w1",
        context=ExecutionContext(principal="user:test", attributes={"run_id": "run-g34a"}),
        config=config,
    )
    created = asyncio.run(provider.create(spec))
    assert created.ok and created.handle is not None
    handle = created.handle

    # Language-neutral: a temporary directory, a temp file round-trip and a piped subprocess.
    script = (
        'python3 -c "import pathlib,subprocess,sys,tempfile;'
        "d=tempfile.mkdtemp();p=pathlib.Path(d)/'f.txt';p.write_text('hello');"
        "print(p.read_text());"
        "print(p.parent.exists());"
        "print(subprocess.run([sys.executable,'-c','print(42)'],capture_output=True,text=True).stdout.strip())\""
    )
    result = asyncio.run(
        provider.execute(handle, SandboxCommand(command=script, cwd="/", timeout_seconds=60))
    )
    assert result.ok and result.exit_code == 0, result.stderr or result.error
    assert "hello" in result.stdout
    assert "True" in result.stdout
    assert "42" in result.stdout


# --- Test F: the oracle still evaluates on the trusted side --------------------------------


def test_oracle_still_evaluates_after_isolation(tmp_path: Path) -> None:
    task = _task()
    workspace = tmp_path / "workspace"
    materialize_fixture(task, workspace)
    (workspace / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )
    baseline = workspace_hashes(tmp_path / "baseline")
    (tmp_path / "baseline").mkdir()
    materialize_fixture(task, tmp_path / "baseline")
    baseline = workspace_hashes(tmp_path / "baseline")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    outcome = evaluate_oracle(
        task,
        workspace=workspace,
        baseline_hashes=baseline,
        tool_calls=[],
        worker_results={"worker": {"success": True, "output": "done"}},
        run_dir=run_dir,
        host_fixture_unchanged=True,
    )

    assert outcome.checks["hidden_tests_passed"] is True
    assert outcome.passed is True
    assert (run_dir / "oracle.log").is_file()
    assert (run_dir / "oracle-workspace" / PRIVATE_TEST).is_file()


# --- Test G: the isolation step removes staging and fails closed ---------------------------


def test_isolation_step_removes_staging_bundle() -> None:
    provider = _StubProvider()
    report = asyncio.run(enforce_evaluation_isolation(provider, handle=None))

    assert report.passed is True
    assert report.staging_bundle_present_before is True
    assert report.staging_bundle_removed is True
    assert report.private_name_hits == []
    assert provider.bundle_present is False
    assert any(command.startswith("rm -f ") for command in provider.commands)
    assert any("find /" in command for command in provider.commands)
    assert STAGING_BUNDLE in provider.commands[0]


def test_isolation_step_fails_closed_when_a_private_name_is_reachable() -> None:
    @dataclass
    class _Leaky(_StubProvider):
        async def execute(self, handle: Any, command: SandboxCommand) -> SandboxCommandResult:
            self.commands.append(command.command)
            if command.command.startswith("find /"):
                return SandboxCommandResult(
                    ok=True, exit_code=0, stdout="/opt/tests/test_hidden_canary.py\n"
                )
            if command.command.startswith("test -e"):
                return SandboxCommandResult(ok=True, exit_code=0, stdout="absent\n")
            return SandboxCommandResult(ok=True, exit_code=0, stdout="")

    report = asyncio.run(enforce_evaluation_isolation(_Leaky(), handle=None))

    assert report.passed is False
    assert report.private_name_hits == ["/opt/tests/test_hidden_canary.py"]
    assert report.as_dict()["isolation_checks_passed"] is False


# --- fixture commit message must not advertise the benchmark ------------------------------


def test_fixture_commit_message_is_neutral(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    message = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert message == FIXTURE_COMMIT_LABEL
    assert "evaluation" not in message.lower()
    assert "g34a" not in message.lower()


# --- workspace tools still refuse host paths -------------------------------------------------


def test_workspace_tools_refuse_paths_outside_the_project() -> None:
    from types import SimpleNamespace

    from atlasos.sandbox import SandboxWorkspaceTools

    manager = SimpleNamespace(
        require_ready=lambda task_id: SimpleNamespace(
            workspace_root="/workspace/project",
            sandbox_handle=SimpleNamespace(id="sbx-1"),
        )
    )
    tools = SandboxWorkspaceTools(
        cast(Any, manager), "task-g34a", provider=cast(Any, SimpleNamespace())
    )

    for path in ("../../etc/passwd", "/etc/passwd", "/workspace/project/../secrets"):
        with pytest.raises(ValueError):
            tools._logical(path)  # noqa: SLF001 - the isolation boundary under test

    assert tools._logical("calc.py") == "/workspace/project/calc.py"  # noqa: SLF001
    assert (  # noqa: SLF001
        tools._logical("/workspace/project/calc.py") == "/workspace/project/calc.py"
    )
