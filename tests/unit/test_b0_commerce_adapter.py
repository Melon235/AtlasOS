"""B0: adapter semantics for the third-party Commerce Agent Bench.

These tests never touch Docker. They pin the parts the adapter owns: pinned-commit refusal, task
integrity, native environment staging, hidden-material isolation, native grader parsing and cleanup
reporting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from experiments.business_benchmarks.commerce_agent_bench import upstream
from experiments.business_benchmarks.commerce_agent_bench.container import (
    CommandResult,
    ContainerError,
    Mount,
)
from experiments.business_benchmarks.commerce_agent_bench.environment import (
    EnvironmentConfig,
    NativeEnvironment,
)
from experiments.business_benchmarks.commerce_agent_bench.grader import (
    GRADER_ERROR,
    GRADER_MISSING_REWARD,
    GRADER_OK,
    NativeGrader,
)
from experiments.business_benchmarks.commerce_agent_bench.upstream import (
    PINNED_COMMIT,
    PINNED_IMAGE,
    NativeTask,
    UpstreamError,
    task_digests,
)

TASK_ID = "api-slack-supplier-quote-thread-consolidation"
TASK_REL = "api/slack/supplier-quote-thread-consolidation"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build_task(tmp_path: Path, *, extra_private: str = "answer") -> NativeTask:
    root = tmp_path / "datasets_domain_v1" / TASK_REL
    _write(root / "task.md", "# Task\nConsolidate the quotes.\n")
    _write(
        root / "task.toml",
        "\n".join(
            [
                'schema_version = "1.0"',
                "[task]",
                f'id = "{TASK_ID}"',
                'language = "en"',
                'entrypoint = "task.md"',
                'files_dir = "workspace"',
                'test = "grader/run.sh"',
                'rubric = "rubric.json"',
                'private_dir = "private"',
                "[agent]",
                "timeout_sec = 1800",
                "max_actions = 60",
                "[verifier]",
                'type = "script"',
                "timeout_sec = 120",
                "[environment]",
                "requires_network = false",
                "mock_services = true",
                f'base_images = ["{PINNED_IMAGE}"]',
            ]
        )
        + "\n",
    )
    _write(root / "rubric.json", json.dumps({"checks": [{"id": "a"}]}))
    _write(root / "workspace" / "README.md", "public\n")
    _write(root / "workspace" / "data" / "rows.csv", "a,b\n1,2\n")
    _write(root / "grader" / "run.sh", "#!/usr/bin/env bash\nexit 0\n")
    _write(root / "grader" / "verify_task.py", "print('ok')\n")
    _write(root / "private" / "expected_answer.json", extra_private)
    _write(root / "services" / "start_services.sh", "#!/usr/bin/env bash\ntrue\n")
    return NativeTask(
        task_id=TASK_ID,
        root=root,
        definition={
            "task": {"id": TASK_ID},
            "agent": {"timeout_sec": 1800, "max_actions": 60},
            "verifier": {"timeout_sec": 120},
            "environment": {"base_images": [PINNED_IMAGE], "mock_services": True},
        },
        prompt="# Task\n",
        rubric={"checks": []},
        image=PINNED_IMAGE,
        workspace=root / "workspace",
        grader_script=root / "grader" / "run.sh",
        private_dir=root / "private",
    )


class FakeBackend:
    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.execs: list[tuple[str, str, int]] = []
        self.destroyed: list[str] = []
        self.copies_out: list[tuple[str, str, Path]] = []
        self.run_error: Exception | None = None
        self.services_result = CommandResult(exit_code=0)
        self.probe_result = CommandResult(exit_code=0)
        self.destroy_error: Exception | None = None
        self.ports = {9110: 49110}

    def run_container(self, *, name, image, mounts, ports, env, labels, platform=None):
        if self.run_error is not None:
            raise self.run_error
        self.runs.append(
            {
                "name": name,
                "image": image,
                "mounts": list(mounts),
                "ports": list(ports),
                "env": dict(env),
                "labels": dict(labels),
                "platform": platform,
            }
        )
        return "container-id"

    def exec_in(self, name, command, *, timeout_seconds=300, user=None):
        self.execs.append((name, command, timeout_seconds))
        if "start_services.sh" in command:
            return self.services_result
        return self.probe_result

    def copy_from(self, name, remote_path, local_path):
        self.copies_out.append((name, remote_path, local_path))

    def copy_to(self, name, local_path, remote_path):
        return None

    def container_exists(self, name):
        return bool(self.runs)

    def destroy(self, name):
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append(name)

    def published_port(self, name, container_port):
        return self.ports.get(container_port)


class FakeGraderRunner:
    def __init__(self, *, result: CommandResult, reward: dict | None) -> None:
        self.result = result
        self.reward = reward
        self.calls: list[dict] = []

    def run(self, *, task_dir, output_dir, reward_path, timeout):
        self.calls.append(
            {"task_dir": task_dir, "output_dir": output_dir, "reward_path": reward_path}
        )
        if self.reward is not None:
            reward_path.parent.mkdir(parents=True, exist_ok=True)
            reward_path.write_text(json.dumps(self.reward), encoding="utf-8")
        return self.result


# --------------------------------------------------------------------------- upstream pinning
def test_pinned_commit_marker_is_accepted_only_with_matching_value(tmp_path: Path) -> None:
    (tmp_path / "datasets_domain_v1").mkdir()
    (tmp_path / "UPSTREAM_COMMIT").write_text(PINNED_COMMIT + "\n", encoding="utf-8")
    assert upstream.verify_commit(tmp_path) == PINNED_COMMIT
    (tmp_path / "UPSTREAM_COMMIT").write_text("0" * 40, encoding="utf-8")
    with pytest.raises(UpstreamError):
        upstream.verify_commit(tmp_path)


def test_unpinned_checkout_is_refused(tmp_path: Path) -> None:
    (tmp_path / "datasets_domain_v1").mkdir()
    with pytest.raises(UpstreamError):
        upstream.verify_commit(tmp_path)


def test_task_integrity_detects_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = build_task(tmp_path)
    pinned = task_digests(task).as_dict()
    monkeypatch.setattr(upstream, "recorded_digests", lambda: {TASK_ID: pinned})
    upstream.verify_task_integrity(task)
    _write(task.workspace / "README.md", "tampered\n")
    with pytest.raises(UpstreamError):
        upstream.verify_task_integrity(task)


# --------------------------------------------------------------------------- environment
def test_environment_stages_workspace_starts_services_and_tears_down(tmp_path: Path) -> None:
    task = build_task(tmp_path)
    backend = FakeBackend()
    env = NativeEnvironment(task, tmp_path / "run", backend, config=EnvironmentConfig(platform=""))
    handle = env.create()
    assert handle.usable, handle.environment_failure
    assert (handle.workspace_dir / "README.md").read_text(encoding="utf-8") == "public\n"
    run = backend.runs[0]
    assert run["image"] == PINNED_IMAGE
    targets = {mount.target: mount for mount in run["mounts"]}
    assert targets["/task"].read_only is False
    assert targets[f"/benchmark/tasks/{TASK_ID}"].read_only is True
    assert targets[f"/benchmark/private/{TASK_ID}"].read_only is True
    assert handle.services_started is True
    assert any("start_services.sh" in command for _, command, _ in backend.execs)
    assert env.teardown(handle) is None
    assert backend.destroyed == [handle.container]


def test_private_material_never_lands_in_the_workspace(tmp_path: Path) -> None:
    task = build_task(tmp_path, extra_private="SECRET-ANSWER")
    backend = FakeBackend()
    handle = NativeEnvironment(task, tmp_path / "run", backend).create()
    staged = {
        path.relative_to(handle.workspace_dir).as_posix()
        for path in handle.workspace_dir.rglob("*")
        if path.is_file()
    }
    assert staged == {"README.md", "data/rows.csv"}
    for path in handle.workspace_dir.rglob("*"):
        if path.is_file():
            assert "SECRET-ANSWER" not in path.read_text(encoding="utf-8", errors="replace")


def test_container_failure_is_reported_as_environment_failure(tmp_path: Path) -> None:
    task = build_task(tmp_path)
    backend = FakeBackend()
    backend.run_error = ContainerError("no such image")
    handle = NativeEnvironment(task, tmp_path / "run", backend).create()
    assert handle.usable is False
    assert "container_start_failed" in str(handle.environment_failure)


def test_service_failure_is_reported(tmp_path: Path) -> None:
    task = build_task(tmp_path)
    backend = FakeBackend()
    backend.services_result = CommandResult(exit_code=1, stderr="mock crashed")
    handle = NativeEnvironment(task, tmp_path / "run", backend).create()
    assert handle.usable is False
    assert "services_failed" in str(handle.environment_failure)


def test_cleanup_failure_is_reported_not_swallowed(tmp_path: Path) -> None:
    task = build_task(tmp_path)
    backend = FakeBackend()
    env = NativeEnvironment(task, tmp_path / "run", backend)
    handle = env.create()
    backend.destroy_error = ContainerError("daemon gone")
    error = env.teardown(handle)
    assert error is not None and "daemon gone" in error


def test_mount_rendering_translates_windows_paths() -> None:
    mount = Mount(Path(r"D:\Work\AtlasOS\experiments"), "/task", read_only=True)
    from experiments.business_benchmarks.commerce_agent_bench.container import WslDockerBackend

    rendered = mount.render(WslDockerBackend.to_wsl_path)
    assert rendered == "/mnt/d/Work/AtlasOS/experiments:/task:ro"


# --------------------------------------------------------------------------- grader
def test_native_grader_passes_only_when_every_check_passes(tmp_path: Path) -> None:
    task = build_task(tmp_path)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    runner = FakeGraderRunner(
        result=CommandResult(exit_code=0, stdout='{"score": 1.0}'),
        reward={"score": 1.0, "checks_passed": 7, "checks_total": 7, "checks_breakdown": []},
    )
    score = NativeGrader(runner).evaluate(  # type: ignore[arg-type]
        task, outputs_dir=outputs, reward_path=tmp_path / "reward.json"
    )
    assert score.status == GRADER_OK
    assert score.passed is True
    assert score.checks_passed == 7 == score.checks_total


def test_native_grader_rejects_partial_checks(tmp_path: Path) -> None:
    task = build_task(tmp_path)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    runner = FakeGraderRunner(
        result=CommandResult(exit_code=1),
        reward={"score": 0.57, "checks_passed": 4, "checks_total": 7},
    )
    score = NativeGrader(runner).evaluate(  # type: ignore[arg-type]
        task, outputs_dir=outputs, reward_path=tmp_path / "reward.json"
    )
    assert score.passed is False
    assert score.score == pytest.approx(0.57)


def test_native_grader_without_reward_file_is_an_environment_failure(tmp_path: Path) -> None:
    task = build_task(tmp_path)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    runner = FakeGraderRunner(result=CommandResult(exit_code=2, stderr="boom"), reward=None)
    score = NativeGrader(runner).evaluate(  # type: ignore[arg-type]
        task, outputs_dir=outputs, reward_path=tmp_path / "reward.json"
    )
    assert score.status == GRADER_ERROR
    assert score.passed is False
    assert score.environment_failure is True


def test_native_grader_missing_outputs_never_scores(tmp_path: Path) -> None:
    task = build_task(tmp_path)
    score = NativeGrader(FakeGraderRunner(result=CommandResult(0), reward={})).evaluate(  # type: ignore[arg-type]
        task, outputs_dir=tmp_path / "absent", reward_path=tmp_path / "reward.json"
    )
    assert score.status == GRADER_MISSING_REWARD
    assert score.environment_failure is True
