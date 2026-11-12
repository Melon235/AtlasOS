"""B0 smoke driver: start the native environment, optionally demonstrate, grade, tear down.

Three modes, all against the pinned upstream task:

``env``        start the container + mock services, report health, destroy.
``reference``  additionally copy the task's private reference answer into the outputs and run the
               original grader. This is a *grader self-check*, not a benchmark attempt: no agent
               runs and the result must never be reported as a task score.
``script``     run one shell command inside the container before grading (research aid).

Usage::

    set CAB_UPSTREAM=D:\\Work\\_cabbench
    python -m experiments.business_benchmarks.commerce_agent_bench.smoke --task-id api-slack-...
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from experiments.business_benchmarks.commerce_agent_bench.container import WslDockerBackend
from experiments.business_benchmarks.commerce_agent_bench.environment import (
    EnvironmentConfig,
    NativeEnvironment,
)
from experiments.business_benchmarks.commerce_agent_bench.grader import NativeGrader
from experiments.business_benchmarks.commerce_agent_bench.upstream import (
    load_task,
    manifest_payload,
    task_digests,
    text_only_collection,
    upstream_root,
    verify_commit,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "experiments" / "evidence" / "stage-b0"
DEFAULT_TASK = "api-slack-supplier-quote-thread-consolidation"


def run_smoke(
    task_id: str,
    *,
    mode: str = "env",
    upstream: Path | None = None,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    command: str | None = None,
    keep_container: bool = False,
) -> dict[str, Any]:
    root = upstream or upstream_root()
    commit = verify_commit(root)
    task = load_task(task_id, root=root)
    digests = task_digests(task)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = evidence_root / "runs" / f"{stamp}-{mode}-{task_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    backend = WslDockerBackend()
    environment = NativeEnvironment(
        task,
        run_dir,
        backend,
        config=EnvironmentConfig(platform="linux/amd64"),
    )
    handle = environment.create()
    record: dict[str, Any] = {
        "stage": "b0",
        "mode": mode,
        "upstream": {
            **manifest_payload(),
            "verified_commit": commit,
            "task_in_text_only_collection": task_id in text_only_collection(root),
            "digests": digests.as_dict(),
        },
        "task": {
            "task_id": task.task_id,
            "language": task.definition["task"].get("language"),
            "difficulty": task.definition["task"].get("difficulty"),
            "agent_timeout_sec": task.agent_timeout_seconds,
            "max_actions": task.max_actions,
            "verifier_timeout_sec": task.verifier_timeout_seconds,
            "runtime_mocks": sorted(task.runtime_mocks),
        },
        "environment": handle.as_dict(),
        "command": None,
        "grade": None,
        "cleanup": None,
        "started_at": stamp,
    }
    if mode == "script" and handle.usable and command:
        result = backend.exec_in(handle.container, command, timeout_seconds=300)
        record["command"] = {
            "exit_code": result.exit_code,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }
    if mode == "reference" and handle.usable:
        reference = task.private_dir / "expected_answer.json"
        if reference.is_file():
            handle.outputs_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(reference, handle.outputs_dir / "answer.json")
        record["grade"] = (
            NativeGrader()
            .evaluate(task, outputs_dir=handle.outputs_dir, reward_path=run_dir / "reward.json")
            .as_dict()
        )
        record["grade"]["note"] = "grader self-check with the private reference answer"
    if not keep_container:
        record["cleanup"] = environment.teardown(handle)
    (run_dir / "smoke.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    record["evidence_path"] = str(run_dir / "smoke.json")
    return record


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Commerce Agent Bench native smoke test")
    parser.add_argument("--task-id", default=DEFAULT_TASK)
    parser.add_argument("--mode", choices=["env", "reference", "script"], default="env")
    parser.add_argument("--upstream", default=None)
    parser.add_argument("--command", default=None)
    parser.add_argument("--keep-container", action="store_true")
    args = parser.parse_args(argv)
    record = run_smoke(
        args.task_id,
        mode=args.mode,
        upstream=Path(args.upstream) if args.upstream else None,
        command=args.command,
        keep_container=args.keep_container,
    )
    print(
        json.dumps(
            {k: record[k] for k in ("mode", "environment", "command", "grade", "cleanup")}, indent=2
        )
    )
    print(f"evidence: {record['evidence_path']}")
    return record


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()


__all__ = ["DEFAULT_TASK", "run_smoke", "main"]
