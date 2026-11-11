"""Stage G3-4a: visibility verification from *inside* a real OpenSandbox container.

Gated by ``ATLASOS_RUN_LIVE_SANDBOX=1`` because it creates and destroys a real sandbox. It does not
call a model. Run it once per report round:

    $env:ATLASOS_RUN_LIVE_SANDBOX = "1"
    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_stage_g3_4a_isolation_live.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxCommand
from experiments.evaluation.fixtures import materialize_fixture, materialize_oracle, stage_git_repo
from experiments.evaluation.models import BenchmarkTask, WorkerStep
from experiments.evaluation.stage_g3_4a_integrity import (
    FIXTURE_COMMIT_LABEL,
    canary_for,
    enforce_evaluation_isolation,
)

from atlasos.sandbox import ProjectSandboxManager

pytestmark = pytest.mark.skipif(
    os.environ.get("ATLASOS_RUN_LIVE_SANDBOX") != "1",
    reason="set ATLASOS_RUN_LIVE_SANDBOX=1 to create a real OpenSandbox sandbox",
)

SAMPLE = "g34a-live"
CANARY_SEED = "live-oracle"


def _task() -> BenchmarkTask:
    canary = canary_for(CANARY_SEED)
    return BenchmarkTask(
        sample_id=SAMPLE,
        category="multi_worker",
        mode="coding",
        prompt="Implement add(a, b) in calc.py.",
        target="hidden oracle passes",
        visible_files={
            "README.md": "# demo project\n",
            "calc.py": "def add(a: int, b: int) -> int:\n    raise NotImplementedError\n",
            "tests/test_public.py": "from calc import add\n\n\ndef test_public() -> None:\n"
            "    assert callable(add)\n",
            "pyproject.toml": "[project]\nname = 'demo'\n",
        },
        hidden_files={
            "tests/test_hidden_live.py": (
                "from calc import add\n\n\n"
                "def test_hidden_live() -> None:\n    assert add(1, 2) == 3\n"
                f"    # {canary}\n"
            )
        },
        workers=(WorkerStep("solo", f"{SAMPLE}-solo", "Implement calc.py add."),),
        allowed_paths=("calc.py",),
        forbidden_paths=("tests/**",),
        required_artifacts=("calc.py",),
    )


async def _run(provider, handle, command: str):
    return await provider.execute(
        handle, SandboxCommand(command=command, cwd="/", timeout_seconds=120)
    )


@pytest.mark.asyncio
async def test_live_sandbox_cannot_reach_private_artifacts(tmp_path: Path) -> None:
    from experiments.evaluation.runner import RunnerSettings, _build_provider, _project_config  # noqa: PLC2701

    from atlasos.config import load_config

    config = load_config(RunnerSettings().config_path)
    if config.sandbox.provider != "opensandbox":
        pytest.skip("live isolation check requires the opensandbox provider")

    task = _task()
    canary = canary_for(CANARY_SEED)

    fixture = tmp_path / "fixture"
    materialize_fixture(task, fixture)
    stage_git_repo(fixture, label=FIXTURE_COMMIT_LABEL)
    oracle_dir = materialize_oracle(task, tmp_path / "run" / "oracle-workspace")

    provider = _build_provider(config)
    manager = ProjectSandboxManager(provider, _project_config(config), run_id="run-g34a-live")
    binding = await manager.ensure_binding(
        task_id=f"{SAMPLE}-workspace",
        source_repo=str(fixture),
        context=ExecutionContext(principal="user:test", attributes={"run_id": "run-g34a-live"}),
        worker_id="w1",
    )
    handle = binding.sandbox_handle
    try:
        # 1. Before cleanup the staging bundle is visible (this is the confirmed leak path).
        before = await _run(provider, handle, "test -e /tmp/project.bundle && echo present")
        assert "present" in before.stdout

        report = await enforce_evaluation_isolation(provider, handle)
        assert report.staging_bundle_present_before is True
        assert report.staging_bundle_removed is True
        assert report.passed is True, report.failures

        # 2. The staging bundle is gone.
        after = await _run(provider, handle, "test -e /tmp/project.bundle && echo present")
        assert "present" not in after.stdout

        # 3. No oracle canary anywhere in the container (content-based, not name-based).
        sweep = await _run(
            provider,
            handle,
            (
                "grep -rl --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev "
                f"'{canary}' / 2>/dev/null | head -5"
            ),
        )
        assert sweep.ok, sweep.error
        assert sweep.stdout.strip() == "", f"canary leaked into: {sweep.stdout}"

        # 4. No private test name is reachable.
        names = await _run(
            provider,
            handle,
            "find / -xdev -maxdepth 4 -name 'test_hidden*' -not -path '/proc/*' 2>/dev/null | head",
        )
        assert names.stdout.strip() == ""

        # 5. Public workspace is still intact and usable.
        read = await _run(provider, handle, "cat /workspace/project/calc.py")
        assert "def add" in read.stdout
        public_tests = await _run(provider, handle, "ls /workspace/project/tests")
        assert "test_public.py" in public_tests.stdout

        # 6. The workspace git history no longer advertises the benchmark sample.
        log = await _run(provider, handle, "git -C /workspace/project log --pretty=%s")
        assert task.sample_id not in log.stdout
        assert "evaluation" not in log.stdout.lower()

        # 7. Symlink / traversal attempts cannot reach the host oracle directory.
        alias = await _run(
            provider,
            handle,
            (
                "ln -sfn /workspace/project/../../oracle-workspace /tmp/escape; "
                "ls -a /tmp/escape 2>&1 | head -3; "
                "cat /tmp/escape/tests/test_hidden_live.py 2>&1 | head -3"
            ),
        )
        # The path is echoed by the shell ("no such file"), but no content can be read back.
        assert canary not in alias.stdout
        assert "No such file or directory" in alias.stdout

        # 8. Normal temporary-file workflow is unaffected.
        temp = await _run(
            provider,
            handle,
            (
                'd=$(mktemp -d) && echo ok > "$d/f.txt" && cat "$d/f.txt" && '
                "python3 -c 'import tempfile,pathlib;"
                'p=pathlib.Path(tempfile.mkdtemp())/"x";p.write_text("py");print(p.read_text())\''
            ),
        )
        assert temp.ok and temp.exit_code == 0, temp.stderr or temp.error
        assert "ok" in temp.stdout and "py" in temp.stdout

        # 9. The hidden oracle still loads and grades on the trusted side.
        assert (oracle_dir / "tests" / "test_hidden_live.py").is_file()
        assert canary in (oracle_dir / "tests" / "test_hidden_live.py").read_text(encoding="utf-8")
    finally:
        await manager.close(f"{SAMPLE}-workspace")
