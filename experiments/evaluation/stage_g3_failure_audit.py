"""Stage G3-0: read-only failure critical-path audit for the multi-worker runs.

Consumes only existing Stage E1 artifacts (``result.json``, ``tool_conversations.json``,
``trajectory/*/transcript.jsonl``, ``oracle.log``, ``workspace/``) plus the Stage G2b-0
reconstruction helpers. It never calls a model, never starts a sandbox and never writes into a
run directory.

Outputs (``experiments/results/stage-g3-audit/``):
``selected_cases.json``, ``critical_paths.csv``, ``side_effect_cases.csv``,
``worker_contracts.csv``, ``integration_failures.csv``, ``tool_offload_estimates.csv``,
``issues.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.context.tokenizer import select_tokenizer
from experiments.evaluation.stage_e_evidence import collect_evidence
from experiments.evaluation.stage_g2b_diagnosis import PromptCounter, conversations

REPO_ROOT = Path(__file__).resolve().parents[2]
E1_ROUND = REPO_ROOT / "experiments" / "results" / "stage-e1" / "e1-20261109T102030Z"
OUTPUT_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-audit"

VERIFY_PATTERNS = re.compile(
    r"\b(pytest|pyright|ruff|mypy|unittest|npm test|python3? -m pytest|py_compile|tox)\b"
)
EXPLORE_PATTERNS = re.compile(
    r"^\s*(cat|head|tail|ls|find|grep|sed -n|wc|git (log|status|diff|show|branch|reflog|cat-file|"
    r"ls-tree|rev-list|bundle)|env|pwd)\b"
)
#: Commands that look for graders, hidden tests or artifacts outside the project workspace.
ORACLE_PROBE_PATTERNS = re.compile(
    r"(find / |find /tmp|\.stdout|\.stderr|project\.bundle|git bundle|git reflog|git log --all|"
    r"git cat-file|git rev-list|/tmp/|test_hidden|hidden test|oracle)"
)
IMPLEMENT_TOOLS = {"file_write"}
EDIT_TOOLS = {"file_edit"}
READ_TOOLS = {"file_read", "grep", "list_files"}
STUB_MARKER = "NotImplementedError"


@dataclass
class Run:
    run_dir: Path
    result: dict[str, Any]
    manifest_pair: dict[str, Any] = field(default_factory=dict)

    @property
    def sample_id(self) -> str:
        return str(self.result["sample_id"])

    @property
    def variant(self) -> str:
        return "a0" if self.sample_id.endswith("-a0") else "a1"

    @property
    def run_id(self) -> str:
        return str(self.result["identity"]["run_id"])

    @property
    def repetition(self) -> int:
        match = re.match(r"^r(\d+)-", self.run_dir.name)
        return int(match.group(1)) if match else -1

    @property
    def oracle_passed(self) -> bool | None:
        return (self.result.get("oracle") or {}).get("passed")


def load_runs() -> list[Run]:
    runs: list[Run] = []
    for path in sorted({p.parent for p in (E1_ROUND / "runs").glob("*/*/*/result.json")}):
        payload = json.loads((path / "result.json").read_text(encoding="utf-8"))
        runs.append(Run(run_dir=path, result=payload))
    return runs


# --------------------------------------------------------------------------- selection


def worker_stops(run: Run) -> dict[str, str]:
    return {
        name: str((info.get("stop_reason") or {}).get("code"))
        for name, info in (run.result.get("worker_results") or {}).items()
    }


def last_tool_for(run: Run, worker: str) -> dict[str, Any] | None:
    calls = [call for call in (run.result.get("tool_calls") or []) if call.get("agent") == worker]
    return calls[-1] if calls else None


def select_cases(runs: list[Run]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    by_sample: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        by_sample[run.sample_id].append(run)

    # A: highest-cost e1-02-report A1 run with max_turns workers + same-repetition A0 control.
    candidates = [
        run for run in by_sample["e1-02-report-a1"] if "max_turns" in worker_stops(run).values()
    ]
    candidates.sort(key=lambda run: -(run.result["metrics"].get("prompt_tokens") or 0))
    if candidates:
        primary = candidates[0]
        cases.append(
            {
                "case_id": "A-a1-max-turns",
                "stage": "stage-e1",
                "run_id": primary.run_id,
                "sample_id": primary.sample_id,
                "repetition": primary.repetition,
                "worker_id": "",
                "stop_reason": "max_turns",
                "oracle_success": primary.oracle_passed,
                "selection_reason": (
                    "highest prompt-token e1-02-report A1 run containing a max_turns worker"
                ),
                "run_dir": str(primary.run_dir),
            }
        )
        control = next(
            (run for run in by_sample["e1-02-report-a0"] if run.repetition == primary.repetition),
            None,
        )
        if control is not None:
            cases.append(
                {
                    "case_id": "A-a0-control",
                    "stage": "stage-e1",
                    "run_id": control.run_id,
                    "sample_id": control.sample_id,
                    "repetition": control.repetition,
                    "worker_id": "",
                    "stop_reason": "control",
                    "oracle_success": control.oracle_passed,
                    "selection_reason": "A0 single-worker control at the same repetition",
                    "run_dir": str(control.run_dir),
                }
            )

    # B: side_effect_unknown workers. The authoritative tool is the M4 record with
    # retry_safe=False, not the last entry of result.tool_calls (a batch may contain both).
    side_cases = 0
    seen: Counter[tuple[str, int | None]] = Counter()
    for run in runs:
        for worker, stop in worker_stops(run).items():
            if stop != "side_effect_unknown" or side_cases >= 3:
                continue
            outcomes = [row for row in trajectory_outcomes(run) if row["retry_safe"] is False]
            if not outcomes:
                continue
            outcome = outcomes[-1]
            tool = str(outcome.get("tool_id") or "?")
            stop_message = str(
                ((run.result.get("worker_results") or {}).get(worker) or {})
                .get("stop_reason", {})
                .get("message", "")
            )
            exit_code = _exit_code(stop_message)
            if seen[(tool, exit_code)]:
                continue
            seen[(tool, exit_code)] += 1
            side_cases += 1
            cases.append(
                {
                    "case_id": f"B-{side_cases}-{tool}-exit{exit_code}",
                    "stage": "stage-e1",
                    "run_id": run.run_id,
                    "sample_id": run.sample_id,
                    "repetition": run.repetition,
                    "worker_id": worker,
                    "stop_reason": stop,
                    "oracle_success": run.oracle_passed,
                    "selection_reason": (
                        f"side_effect_unknown; M4 record marks {tool} retry_safe=False with "
                        f"completion_kind={outcome.get('completion_kind')} and exit={exit_code}"
                    ),
                    "run_dir": str(run.run_dir),
                }
            )
            break

    # C: e1-07-export A1 runs where every worker reported success but the oracle failed.
    export = [
        run
        for run in by_sample["e1-07-export-a1"]
        if run.oracle_passed is False
        and all(
            bool(info.get("success")) for info in (run.result.get("worker_results") or {}).values()
        )
    ]
    for index, run in enumerate(export[:2], start=1):
        cases.append(
            {
                "case_id": f"C-{index}-all-workers-success",
                "stage": "stage-e1",
                "run_id": run.run_id,
                "sample_id": run.sample_id,
                "repetition": run.repetition,
                "worker_id": "",
                "stop_reason": "success",
                "oracle_success": run.oracle_passed,
                "selection_reason": (
                    "every worker returned success with stop_reason=success, oracle still failed"
                ),
                "run_dir": str(run.run_dir),
            }
        )

    # D: e1-12-conflict A1 success and failure.
    conflict = by_sample["e1-12-conflict-a1"]
    success = next((run for run in conflict if run.oracle_passed is True), None)
    failure = next((run for run in conflict if run.oracle_passed is False), None)
    for label, run in (("D-1-success", success), ("D-2-failure", failure)):
        if run is None:
            continue
        cases.append(
            {
                "case_id": label,
                "stage": "stage-e1",
                "run_id": run.run_id,
                "sample_id": run.sample_id,
                "repetition": run.repetition,
                "worker_id": "",
                "stop_reason": "success" if run.oracle_passed else "failed",
                "oracle_success": run.oracle_passed,
                "selection_reason": "e1-12-conflict A1 collaboration "
                + ("success" if run.oracle_passed else "failure"),
                "run_dir": str(run.run_dir),
            }
        )
    return cases


# --------------------------------------------------------------------------- critical path


def _summarize_input(tool: str, payload: dict[str, Any]) -> str:
    if tool == "bash":
        command = " ".join(str(payload.get("command") or "").split())
        return command[:160]
    if "path" in payload:
        return str(payload.get("path"))[:160]
    return json.dumps(payload, default=str)[:160]


def classify_turn(tool: str, payload: dict[str, Any], *, prior_verify_failed: bool) -> str:
    if tool in READ_TOOLS:
        return "EXPLORE"
    if tool in IMPLEMENT_TOOLS:
        return "IMPLEMENT"
    if tool in EDIT_TOOLS:
        return "REPAIR" if prior_verify_failed else "IMPLEMENT"
    if tool == "bash":
        command = " ".join(str(payload.get("command") or "").split())
        if VERIFY_PATTERNS.search(command):
            return "VERIFY"
        if EXPLORE_PATTERNS.search(command):
            return "EXPLORE"
        if command.startswith("cd ") and ("cat " in command or "find " in command):
            return "EXPLORE"
        if "assert " in command:
            return "VERIFY"
        return "OTHER"
    return "OTHER"


def is_oracle_probe(tool: str, payload: dict[str, Any]) -> bool:
    if tool != "bash":
        return False
    command = " ".join(str(payload.get("command") or "").split())
    return bool(ORACLE_PROBE_PATTERNS.search(command))


def critical_path(run: Run) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task_key, messages in conversations(run.run_dir).items():
        names: dict[str, str] = {}
        inputs: dict[str, dict[str, Any]] = {}
        turn = 0
        verify_failed = False
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                turn += 1
                for block in message.get("content") or []:
                    if block.get("type") == "tool_use":
                        names[str(block.get("id"))] = str(block.get("name"))
                        inputs[str(block.get("id"))] = dict(block.get("input") or {})
                continue
            for block in message.get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id"))
                tool = names.get(call_id, "?")
                payload = inputs.get(call_id, {})
                is_error = bool(block.get("is_error"))
                phase = classify_turn(tool, payload, prior_verify_failed=verify_failed)
                if phase == "VERIFY" and is_error:
                    verify_failed = True
                if phase in {"IMPLEMENT", "REPAIR"}:
                    verify_failed = False
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, default=str)
                rows.append(
                    {
                        "run_id": run.run_id,
                        "sample_id": run.sample_id,
                        "task_key": task_key,
                        "turn_index": turn,
                        "call_id": call_id,
                        "tool": tool,
                        "phase": phase,
                        "oracle_probe": is_oracle_probe(tool, payload),
                        "input_summary": _summarize_input(tool, payload),
                        "result_chars": len(text),
                        "is_error": is_error,
                        "exit_code": _exit_code(text),
                        "result_head": " ".join(text.split())[:180],
                    }
                )
    return rows


_EXIT_RE = re.compile(r"exit code (\d+)")


def _exit_code(text: str) -> int | None:
    match = _EXIT_RE.search(text)
    return int(match.group(1)) if match else None


def group_turns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse tool calls into worker turns (one model request may hold several calls)."""

    buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[(row["task_key"], int(row["turn_index"]))].append(row)
    turns: list[dict[str, Any]] = []
    for (task_key, turn_index), calls in sorted(buckets.items()):
        phases = [row["phase"] for row in calls]
        if any(phase in {"IMPLEMENT", "REPAIR"} for phase in phases):
            phase = next(phase for phase in phases if phase in {"IMPLEMENT", "REPAIR"})
        elif "VERIFY" in phases:
            phase = "VERIFY"
        elif "EXPLORE" in phases:
            phase = "EXPLORE"
        else:
            phase = "OTHER"
        turns.append(
            {
                "task_key": task_key,
                "turn_index": turn_index,
                "phase": phase,
                "tools": [row["tool"] for row in calls],
                "oracle_probe": any(row["oracle_probe"] for row in calls),
                "result_chars": sum(row["result_chars"] for row in calls),
                "is_error": any(row["is_error"] for row in calls),
            }
        )
    return turns


def trajectory_outcomes(run: Run) -> list[dict[str, Any]]:
    records = []
    for path in run.run_dir.glob("trajectory/*/transcript.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            atlas = (record.get("payload") or {}).get("atlas_trajectory") or {}
            if atlas.get("event_type") != "execution_outcome":
                continue
            data = atlas.get("data") or {}
            records.append(
                {
                    "run_id": run.run_id,
                    "task_key": str((atlas.get("correlation") or {}).get("worker_id") or ""),
                    "tool_id": str(data.get("tool_id")),
                    "state": str(data.get("state")),
                    "completion_kind": str(data.get("completion_kind")),
                    "retry_safe": data.get("retry_safe"),
                    "result_size": data.get("result_size"),
                }
            )
    return records


# --------------------------------------------------------------------------- integration


def final_workspace(run: Run) -> dict[str, Any]:
    workspace = run.run_dir / "workspace"
    files: dict[str, str] = {}
    if workspace.is_dir():
        for path in workspace.rglob("*.py"):
            if ".git" in path.parts:
                continue
            files[str(path.relative_to(workspace)).replace("\\", "/")] = path.read_text(
                encoding="utf-8", errors="replace"
            )
    return files


def oracle_tail(run: Run, limit: int = 1200) -> str:
    path = run.run_dir / "oracle.log"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


# --------------------------------------------------------------------------- offload


def offload_estimate(runs: list[Run], counter: PromptCounter) -> dict[str, Any]:
    """Counterfactual: archive >4000-char tool results at ingress, keep 200+200 preview."""

    total = 0
    large = 0
    large_chars = 0
    large_tokens = 0
    resend_tokens = 0
    after_tokens = 0
    by_tool: Counter[str] = Counter()
    per_run: list[dict[str, Any]] = []
    for run in runs:
        if run.variant != "a1":
            continue
        run_large = 0
        run_resend = 0
        run_after = 0
        for _task_key, messages in conversations(run.run_dir).items():
            names: dict[str, str] = {}
            for message in messages:
                for block in message.get("content") or []:
                    if block.get("type") == "tool_use":
                        names[str(block.get("id"))] = str(block.get("name"))
            for index, message in enumerate(messages):
                for block in message.get("content") or []:
                    if block.get("type") != "tool_result":
                        continue
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content, default=str)
                    tool = names.get(str(block.get("tool_use_id")), "?")
                    total += 1
                    if len(text) <= 4000:
                        continue
                    tokens = counter.count(text)
                    preview = counter.count(text[:200] + text[-200:])
                    large += 1
                    large_chars += len(text)
                    large_tokens += tokens
                    by_tool[tool] += 1
                    run_large += 1
                    # A tool result is re-sent once per subsequent request of the same worker.
                    later_turns = sum(
                        1 for follow in messages[index + 1 :] if follow.get("role") == "assistant"
                    )
                    run_resend += tokens * later_turns
                    run_after += preview * later_turns
                    resend_tokens += tokens * later_turns
                    after_tokens += preview * later_turns
        per_run.append(
            {
                "run_id": run.run_id,
                "sample_id": run.sample_id,
                "large_results": run_large,
                "resend_tokens": run_resend,
                "resend_tokens_after": run_after,
            }
        )
    savings = resend_tokens - after_tokens
    return {
        "all_tool_results": total,
        "large_tool_results": large,
        "large_result_ratio": round(large / total, 4) if total else None,
        "large_result_chars": large_chars,
        "large_result_tokens": large_tokens,
        "estimated_resend_tokens": resend_tokens,
        "estimated_tokens_after_ingress_offload": after_tokens,
        "estimated_savings": savings,
        "savings_ratio": round(savings / resend_tokens, 4) if resend_tokens else None,
        "by_tool": dict(by_tool),
        "per_run": per_run,
        "data_type": "ESTIMATED (tiktoken over reconstructed tool results)",
    }


# --------------------------------------------------------------------------- issues


def build_issues(
    side_rows: list[dict[str, Any]], phases: dict[str, Any], integration: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    executed_errors = [row for row in side_rows if row["classification"] == "A_EXECUTED_ERROR"]
    if executed_errors:
        first = executed_errors[0]
        issues.append(
            {
                "issue_id": "G3-I01",
                "category": "side_effect",
                "evidence": (
                    f"{first['run_id']}/{first['worker_id']} turn {first['turn_index']}: "
                    f"{first['tool']} exited {first['exit_code']} and execution_outcome="
                    f"{first['m4_state']}/{first['completion_kind']} retry_safe="
                    f"{first['outcome_retry_safe']}"
                ),
                "root_cause": (
                    "AtlasOS bash returns is_error=(exit_code!=0) without retry_safe; AnyCode "
                    "_normalize_result maps any error of a side-effecting tool to "
                    "retry_safe=False, and the runner stops the worker with side_effect_unknown "
                    "although the command ran to completion with a known exit code."
                ),
                "confidence": "CONFIRMED",
                "impact": f"{len(executed_errors)} of {len(side_rows)} audited side_effect cases",
                "minimal_fix": (
                    "Set retry_safe=True on ToolResult for commands that completed with a known "
                    "exit code (exit code present, no transport error/timeout); keep "
                    "retry_safe=False for SandboxLostError/timeout/unknown transport."
                ),
                "target_module": "src/atlasos/sandbox/tools.py (bash/grep/list_files/file_*)",
                "verification": (
                    "Fake-provider run where bash exits 1: worker must continue (no "
                    "side_effect_unknown) and the model sees the failing output."
                ),
            }
        )
    denied = [row for row in side_rows if row["classification"] == "B_PRE_EXECUTION_DENY"]
    if denied:
        issues.append(
            {
                "issue_id": "G3-I02",
                "category": "side_effect",
                "evidence": (
                    f"{denied[0]['run_id']}/{denied[0]['worker_id']}: {denied[0]['result_head']}"
                ),
                "root_cause": (
                    "M3 gate denials return ToolResult(retry_safe=False) via the _failure "
                    "default, so a policy denial before execution also ends the worker as "
                    "side_effect_unknown."
                ),
                "confidence": "LIKELY" if denied else "UNDETERMINED",
                "impact": f"{len(denied)} audited case(s)",
                "minimal_fix": (
                    "Use retry_safe=True (and a distinct code) for denials that provably did not "
                    "execute; reserve retry_safe=False for post-dispatch uncertainty."
                ),
                "target_module": "src/atlasos/actions/gate.py",
                "verification": (
                    "Unit test: a denied tool keeps the worker alive and returns a denial."
                ),
            }
        )
    if phases.get("oracle_probe_turns", 0) >= 3:
        issues.append(
            {
                "issue_id": "G3-I03",
                "category": "max_turns",
                "evidence": (
                    f"{phases['case_run_id']}: {phases['oracle_probe_turns']} of "
                    f"{phases['total_tool_turns']} tool turns search for grading artifacts "
                    f"(git history/bundle, /tmp stdout, find /); first file_write at turn "
                    f"{phases['first_implement_turn']}"
                ),
                "root_cause": (
                    "No deliverable boundary in the worker prompt: the worker hunts for hidden "
                    "tests instead of implementing, and each probe round injects large logs into "
                    "the conversation, so the 14-turn budget is spent on search + replay."
                ),
                "confidence": "CONFIRMED",
                "impact": "e1-02-report A1 workers that hit max_turns",
                "minimal_fix": (
                    "Add one verifiable deliverable clause to the worker prompt (implement the "
                    "module + run the module's own smoke check; do not search outside "
                    "/workspace/project). No DAG change, no turn-limit change."
                ),
                "target_module": "worker task text (experiments/evaluation/stage_e_tasks.py)",
                "verification": (
                    "Rerun e1-02-report: oracle_probe turns must drop to 0 and first "
                    "file_write must move to turn <= 4."
                ),
            }
        )
    explore_share = (
        phases.get("explore_turns", 0) / phases["total_tool_turns"]
        if phases.get("total_tool_turns")
        else 0
    )
    if explore_share >= 0.5:
        issues.append(
            {
                "issue_id": "G3-I05",
                "category": "contract",
                "evidence": (
                    f"{phases['case_run_id']}: EXPLORE is {phases['explore_turns']}/"
                    f"{phases['total_tool_turns']} tool turns; only "
                    f"{phases['implement_turns'] + phases['repair_turns']} write/repair turns"
                ),
                "root_cause": (
                    "The worker contract states a goal but no minimal evidence set, so the "
                    "exploration phase is unbounded relative to the 14-turn limit."
                ),
                "confidence": "LIKELY",
                "impact": "all A1 tasks with a broad read-then-write shape",
                "minimal_fix": (
                    "Bound exploration explicitly in the task text (for example: read the "
                    "module and its direct callers, then implement) instead of restructuring "
                    "the DAG."
                ),
                "target_module": "worker task text (experiments/evaluation/stage_e_tasks.py)",
                "verification": "Compare EXPLORE share before/after on the same task.",
            }
        )
    if integration:
        issues.append(
            {
                "issue_id": "G3-I04",
                "category": "integration",
                "evidence": "; ".join(
                    f"{row['run_id']}: {row['missing_or_broken']}" for row in integration[:2]
                ),
                "root_cause": (
                    "Workers report success on their own file but the integration contract "
                    "(cross-module signature/format) is never verified by any worker, so a DAG "
                    "where every worker succeeds still fails the hidden tests."
                ),
                "confidence": "LIKELY",
                "impact": "e1-07-export A1 runs where all workers succeeded",
                "minimal_fix": (
                    "Add one integration verification step (or a 'verify both modules together' "
                    "instruction) to the consumer worker's deliverable; no DAG restructure."
                ),
                "target_module": "worker task contract / DAG verification step",
                "verification": "Rerun e1-07-export; the integration check must fail when "
                "signatures mismatch.",
            }
        )
    return issues


# --------------------------------------------------------------------------- main


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def side_effect_rows(run: Run, case_id: str, worker: str) -> dict[str, Any]:
    """Reconstruct one side_effect_unknown stop from M4 records + the AnyCode stop message.

    The authoritative record is the trajectory ``execution_outcome`` whose ``retry_safe`` is
    ``False``: ``anycode/core/runner.py`` stops on the first such record and uses its output as
    the stop message, which is persisted in ``worker_results[worker].stop_reason.message``.
    """

    worker_info = (run.result.get("worker_results") or {}).get(worker) or {}
    stop_message = str((worker_info.get("stop_reason") or {}).get("message") or "")
    outcomes = [row for row in trajectory_outcomes(run) if row["retry_safe"] is False]
    outcome = outcomes[-1] if outcomes else {}
    tool = str(outcome.get("tool_id") or "")
    worker_key = worker.split(":")[-1].rsplit("-", 1)[-1]
    path_rows = [row for row in critical_path(run) if row["task_key"] == worker_key]
    matching = [row for row in path_rows if row["tool"] == tool]
    last = matching[-1] if matching else (path_rows[-1] if path_rows else {})
    text = stop_message or str(last.get("result_head") or "")
    lower = text.lower()
    if "action_denied" in lower or "not_disclosed" in lower or "not registered" in lower:
        classification = "B_PRE_EXECUTION_DENY"
    elif any(token in lower for token in ("timeout", "timed out", "sandbox lost", "transport")):
        classification = "C_UNKNOWN_OUTCOME"
    elif outcome.get("completion_kind") == "EXECUTED_ERROR" or _exit_code(text) is not None:
        classification = "A_EXECUTED_ERROR"
    elif tool and text:
        classification = "C_UNKNOWN_OUTCOME"
    else:
        classification = "D_INSUFFICIENT_EVIDENCE"
    return {
        "case_id": case_id,
        "run_id": run.run_id,
        "worker_id": worker,
        "turn_index": last.get("turn_index"),
        "tool": tool,
        "input_summary": last.get("input_summary"),
        "exit_code": _exit_code(text),
        "is_error": last.get("is_error"),
        "result_chars": len(text),
        "result_head": " ".join(text.split())[:220],
        "m4_state": outcome.get("state"),
        "completion_kind": outcome.get("completion_kind"),
        "outcome_retry_safe": outcome.get("retry_safe"),
        "outcome_result_size": outcome.get("result_size"),
        "classification": classification,
        "data_source": "trajectory.execution_outcome + worker_results.stop_reason.message",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-g3-audit")
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args(argv)
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)

    runs = load_runs()
    by_run_id = {run.run_id: run for run in runs}
    cases = select_cases(runs)
    (output / "selected_cases.json").write_text(
        json.dumps(cases, indent=2, sort_keys=True), encoding="utf-8"
    )

    path_rows: list[dict[str, Any]] = []
    for case in cases:
        run = by_run_id[case["run_id"]]
        path_rows.extend(critical_path(run))
    write_csv(output / "critical_paths.csv", path_rows)

    side_rows: list[dict[str, Any]] = []
    for case in cases:
        if not case["case_id"].startswith("B-"):
            continue
        run = by_run_id[case["run_id"]]
        side_rows.append(side_effect_rows(run, case["case_id"], case["worker_id"]))
    write_csv(output / "side_effect_cases.csv", side_rows)

    # max_turns phase audit for the A case.
    a_case = next((case for case in cases if case["case_id"] == "A-a1-max-turns"), None)
    phases: dict[str, Any] = {}
    if a_case is not None:
        run = by_run_id[a_case["run_id"]]
        case_rows = [row for row in critical_path(run)]
        turns = group_turns(case_rows)
        phase_counts = Counter(row["phase"] for row in turns)
        total_turns = len(turns)
        first_implement = next(
            (row["turn_index"] for row in turns if row["phase"] in {"IMPLEMENT", "REPAIR"}),
            None,
        )
        probe_rows = [row for row in turns if row["oracle_probe"]]
        per_worker: dict[str, dict[str, Any]] = {}
        for worker in sorted({row["task_key"] for row in turns}):
            worker_rows = [row for row in turns if row["task_key"] == worker]
            counts = Counter(row["phase"] for row in worker_rows)
            per_worker[worker] = {
                "turns": len(worker_rows),
                "phases": dict(counts),
                "oracle_probe_turns": sum(1 for row in worker_rows if row["oracle_probe"]),
                "first_implement_turn": next(
                    (
                        row["turn_index"]
                        for row in worker_rows
                        if row["phase"] in {"IMPLEMENT", "REPAIR"}
                    ),
                    None,
                ),
                "max_result_chars": max(
                    (
                        max(
                            (
                                call["result_chars"]
                                for call in case_rows
                                if call["task_key"] == worker
                                and int(call["turn_index"]) == row["turn_index"]
                            ),
                            default=0,
                        )
                        for row in worker_rows
                    ),
                    default=0,
                ),
            }
        phases = {
            "case_run_id": run.run_id,
            "total_tool_turns": total_turns,
            "phase_counts": dict(phase_counts),
            "verify_turns": phase_counts.get("VERIFY", 0),
            "explore_turns": phase_counts.get("EXPLORE", 0),
            "implement_turns": phase_counts.get("IMPLEMENT", 0),
            "repair_turns": phase_counts.get("REPAIR", 0),
            "other_turns": phase_counts.get("OTHER", 0),
            "verify_share": round(phase_counts.get("VERIFY", 0) / total_turns, 3)
            if total_turns
            else None,
            "max_turns_verify_share": round(phase_counts.get("VERIFY", 0) / total_turns, 3)
            if total_turns
            else None,
            "oracle_probe_turns": len(probe_rows),
            "oracle_probe_share": round(len(probe_rows) / total_turns, 3) if total_turns else None,
            "oracle_probe_chars": sum(row["result_chars"] for row in probe_rows),
            "first_implement_turn": first_implement,
            "turns_per_worker": dict(Counter(row["task_key"] for row in turns)),
            "workers": per_worker,
        }

    # worker contracts / final deliverables for the integration cases.
    integration_rows: list[dict[str, Any]] = []
    for case in cases:
        if not case["case_id"].startswith("C-"):
            continue
        run = by_run_id[case["run_id"]]
        files = final_workspace(run)
        stubs = sorted(name for name, text in files.items() if STUB_MARKER in text)
        oracle = run.result.get("oracle") or {}
        checks = oracle.get("checks") or {}
        integration_rows.append(
            {
                "case_id": case["case_id"],
                "run_id": run.run_id,
                "workers": ";".join(sorted(run.result.get("worker_results") or {})),
                "workspace_py_files": ";".join(sorted(files)),
                "stub_markers_left": ";".join(stubs),
                "checks": json.dumps(checks, sort_keys=True),
                "missing_or_broken": (
                    f"stubs left in {stubs}" if stubs else "no stub markers; failure is semantic"
                ),
                "oracle_tail": " ".join(oracle_tail(run, 600).split())[:400],
                "changed_files": ";".join((oracle.get("details") or {}).get("changed_files") or []),
            }
        )
    write_csv(output / "integration_failures.csv", integration_rows)

    collaboration_rows: list[dict[str, Any]] = []
    for case in cases:
        if not case["case_id"].startswith("D-"):
            continue
        run = by_run_id[case["run_id"]]
        ownership: dict[str, list[str]] = defaultdict(list)
        for call in run.result.get("tool_calls") or []:
            if call.get("tool_name") in ("file_write", "file_edit"):
                path = str((call.get("input") or {}).get("path") or "")
                worker = str(call.get("agent") or "").split("-")[-1]
                if path:
                    ownership[worker].append(path.replace("/workspace/project/", ""))
        edges = [
            {
                "from": str(edge.get("from")),
                "to": str(edge.get("to")),
                "handoff_chars": int(edge.get("handoff_chars") or 0),
                "handoff_complete": edge.get("handoff_complete"),
                "consumed": edge.get("consumed"),
            }
            for edge in (collect_evidence(str(run.run_dir)).get("edges") or [])
        ]
        collaboration_rows.append(
            {
                "case_id": case["case_id"],
                "run_id": run.run_id,
                "oracle_passed": run.oracle_passed,
                "workers": ";".join(sorted(run.result.get("worker_results") or {})),
                "worker_stops": json.dumps(worker_stops(run), sort_keys=True),
                "file_ownership": json.dumps(
                    {worker: sorted(set(paths)) for worker, paths in ownership.items()},
                    sort_keys=True,
                ),
                "overlapping_files": ";".join(
                    sorted(
                        {
                            path
                            for paths in ownership.values()
                            for path in paths
                            if sum(1 for other in ownership.values() if path in other) > 1
                        }
                    )
                ),
                "edges": json.dumps(edges, sort_keys=True),
                "changed_files": ";".join(
                    ((run.result.get("oracle") or {}).get("details") or {}).get("changed_files")
                    or []
                ),
                "oracle_tail": " ".join(oracle_tail(run, 500).split())[:320],
            }
        )
    write_csv(output / "collaboration.csv", collaboration_rows)

    contract_rows: list[dict[str, Any]] = []
    for run in runs:
        if run.variant != "a1":
            continue
        for worker, info in (run.result.get("worker_results") or {}).items():
            contract_rows.append(
                {
                    "run_id": run.run_id,
                    "sample_id": run.sample_id,
                    "worker": worker,
                    "worker_success": info.get("success"),
                    "stop_reason": (info.get("stop_reason") or {}).get("code"),
                    "oracle_passed": run.oracle_passed,
                    "deliverable_correct": "UNKNOWN",
                    "tool_calls": info.get("tool_calls"),
                }
            )
    write_csv(output / "worker_contracts.csv", contract_rows)

    counter = PromptCounter(select_tokenizer("tiktoken", model="deepseek-chat"))
    offload = offload_estimate(runs, counter)
    write_csv(output / "tool_offload_estimates.csv", offload.pop("per_run"))
    (output / "offload_summary.json").write_text(
        json.dumps(offload, indent=2, sort_keys=True), encoding="utf-8"
    )

    issues = build_issues(side_rows, phases, integration_rows)
    payload = {
        "round": str(E1_ROUND),
        "runs_scanned": len(runs),
        "cases": cases,
        "max_turns_phases": phases,
        "side_effect_classes": dict(Counter(row["classification"] for row in side_rows)),
        "offload": offload,
        "issues": issues,
    }
    (output / "issues.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: payload[k] for k in ("runs_scanned", "side_effect_classes", "offload")}, indent=2
        )
    )
    print(json.dumps(phases, indent=2))
    for case in cases:
        print(
            case["case_id"],
            case["run_id"],
            case["sample_id"],
            case["stop_reason"],
            case["oracle_success"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
