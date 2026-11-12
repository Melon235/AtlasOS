"""Native grader integration.

The adapter never scores anything itself. It runs the task's original ``grader/run.sh`` exactly as
upstream does (host bash, ``OUTPUT_DIR`` + ``REWARD_JSON``) and reads the reward file the grader
writes. Passing is the upstream rule: every required check must pass.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from experiments.business_benchmarks.commerce_agent_bench.container import (
    BashGrader,
    ContainerError,
)
from experiments.business_benchmarks.commerce_agent_bench.upstream import NativeTask

GRADER_OK = "GRADER_OK"
GRADER_ERROR = "GRADER_ERROR"
GRADER_TIMEOUT = "GRADER_TIMEOUT"
GRADER_MISSING_REWARD = "GRADER_MISSING_REWARD"


@dataclass(frozen=True)
class NativeScore:
    task_id: str
    status: str
    passed: bool
    score: float | None = None
    checks_passed: int | None = None
    checks_total: int | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    environment_failure: bool = False
    reward_path: str | None = None
    stdout: str = ""
    stderr: str = ""
    timings_ms: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "passed": self.passed,
            "score": self.score,
            "checks_passed": self.checks_passed,
            "checks_total": self.checks_total,
            "checks": self.checks,
            "environment_failure": self.environment_failure,
            "reward_path": self.reward_path,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
            "timings_ms": self.timings_ms,
        }


class NativeGrader:
    def __init__(self, grader: BashGrader | None = None) -> None:
        self.grader = grader or BashGrader()

    def evaluate(
        self,
        task: NativeTask,
        *,
        outputs_dir: Path,
        reward_path: Path,
    ) -> NativeScore:
        started = time.perf_counter()
        reward_path.parent.mkdir(parents=True, exist_ok=True)
        if reward_path.exists():
            reward_path.unlink()
        if not outputs_dir.is_dir():
            return NativeScore(
                task_id=task.task_id,
                status=GRADER_MISSING_REWARD,
                passed=False,
                environment_failure=True,
                stderr="outputs directory is missing; nothing was delivered",
                timings_ms={"total_ms": _elapsed(started)},
            )
        try:
            result = self.grader.run(
                task_dir=task.root,
                output_dir=outputs_dir,
                reward_path=reward_path,
                timeout=task.verifier_timeout_seconds,
            )
        except ContainerError as error:
            message = str(error)
            return NativeScore(
                task_id=task.task_id,
                status=GRADER_TIMEOUT if "timed out" in message else GRADER_ERROR,
                passed=False,
                environment_failure=True,
                stderr=message,
                timings_ms={"total_ms": _elapsed(started)},
            )
        timings = {"total_ms": _elapsed(started)}
        if not reward_path.is_file():
            status = GRADER_TIMEOUT if result.exit_code in (124, 137) else GRADER_ERROR
            return NativeScore(
                task_id=task.task_id,
                status=status,
                passed=False,
                environment_failure=True,
                stdout=result.stdout,
                stderr=result.stderr or f"grader exited {result.exit_code} without a reward file",
                timings_ms=timings,
            )
        try:
            payload = json.loads(reward_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            return NativeScore(
                task_id=task.task_id,
                status=GRADER_ERROR,
                passed=False,
                environment_failure=True,
                stdout=result.stdout,
                stderr=f"reward file is not JSON: {error}",
                reward_path=str(reward_path),
                timings_ms=timings,
            )
        return _score_from_payload(task.task_id, payload, result, reward_path, timings)


def _score_from_payload(
    task_id: str,
    payload: dict[str, Any],
    result: Any,
    reward_path: Path,
    timings: dict[str, float],
) -> NativeScore:
    checks_total = payload.get("checks_total")
    checks_passed = payload.get("checks_passed")
    score = payload.get("score")
    total = int(checks_total) if isinstance(checks_total, int | float) else None
    passed_count = int(checks_passed) if isinstance(checks_passed, int | float) else None
    upstream_pass = bool(total) and passed_count == total
    # The published rule is "every required check passes"; a 1.0 score must agree with it.
    if isinstance(score, int | float) and total:
        upstream_pass = upstream_pass and float(score) >= 1.0 - 1e-9
    return NativeScore(
        task_id=task_id,
        status=GRADER_OK,
        passed=upstream_pass,
        score=float(score) if isinstance(score, int | float) else None,
        checks_passed=passed_count,
        checks_total=total,
        checks=list(payload.get("checks_breakdown") or []),
        reward_path=str(reward_path),
        stdout=result.stdout,
        stderr=result.stderr,
        timings_ms=timings,
    )


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


__all__ = [
    "GRADER_ERROR",
    "GRADER_MISSING_REWARD",
    "GRADER_OK",
    "GRADER_TIMEOUT",
    "NativeGrader",
    "NativeScore",
]
