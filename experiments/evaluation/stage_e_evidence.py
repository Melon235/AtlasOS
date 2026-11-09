"""Read-only evidence association for Stage E multi-worker runs.

M7 lifecycle events prove that a worker existed; they do not prove that a downstream worker
consumed an upstream artifact. This module joins the evaluation's per-task progress events, the
downstream worker's actual prompt, its tool calls, the M5-exported workspace, and the independent
oracle. It never writes runtime state and never decides execution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
                elif "content" in block:
                    parts.append(str(block["content"]))
        return "\n".join(parts)
    return str(content)


def _first_user_text(conversations: dict[str, Any], *task_ids: str) -> str:
    tasks = conversations.get("tasks") or {}
    for task_id in task_ids:
        entry = tasks.get(task_id) or {}
        for message in entry.get("messages") or []:
            if message.get("role") == "user":
                return _text(message.get("content"))
    return ""


def _worker_windows(root: Path) -> dict[str, tuple[str | None, str | None]]:
    """Observed first/last M7 event timestamp per worker (evidence, not authority)."""

    windows: dict[str, list[str]] = {}
    for transcript in (root / "trajectory").rglob("transcript.jsonl"):
        for line in transcript.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = (record.get("payload") or {}).get("atlas_trajectory") or {}
            worker_id = (event.get("correlation") or {}).get("worker_id")
            timestamp = record.get("ts")
            if not worker_id or not timestamp:
                continue
            windows.setdefault(str(worker_id), []).append(str(timestamp))
    return {
        worker_id: (min(timestamps), max(timestamps))
        for worker_id, timestamps in windows.items()
        if timestamps
    }


def _event_window(
    events: list[dict[str, Any]], title: str
) -> tuple[str | None, str | None, float | None]:
    started: str | None = None
    finished: str | None = None
    for event in events:
        if event.get("title") != title:
            continue
        if event.get("type") == "task_start" and started is None:
            started = event.get("ts")
        elif event.get("type") == "task_complete":
            finished = event.get("ts")
    if started and finished:
        try:
            from datetime import datetime

            duration = (
                datetime.fromisoformat(finished) - datetime.fromisoformat(started)
            ).total_seconds() * 1000.0
        except ValueError:
            duration = None
    else:
        duration = None
    return started, finished, duration


def collect_evidence(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    result = _load(root / "result.json", {})
    evidence = _load(root / "stage_e_evidence.json", {})
    workers_payload = _load(root / "workers.json", {})
    conversations = _load(root / "tool_conversations.json", {})
    workers = workers_payload.get("workers") if isinstance(workers_payload, dict) else None
    if isinstance(workers, dict):
        workers = list(workers.values())
    if workers is None:
        workers = workers_payload if isinstance(workers_payload, list) else []
    worker_records = {
        str(record.get("worker_id")): record for record in workers if isinstance(record, dict)
    }
    run_id = (result.get("identity") or {}).get("run_id") or evidence.get("identity", {}).get(
        "run_id"
    )
    evidence_steps = evidence.get("task", {}).get("workers") or []
    result_steps = (result.get("task") or {}).get("workers") or []
    steps = evidence_steps or [
        {"task_id": str(item), "title": str(item), "depends_on": []}
        for item in result_steps
        if not isinstance(item, dict)
    ]
    worker_results = result.get("worker_results") or evidence.get("worker_results") or {}
    tool_calls = result.get("tool_calls") or evidence.get("tool_calls") or []
    events = evidence.get("task_events") or []
    observed_windows = _worker_windows(root)
    step_rows: list[dict[str, Any]] = []
    by_task: dict[str, dict[str, Any]] = {}
    for step in steps:
        step_task_id = str(step.get("task_id") or "")
        runtime_task_id = f"{run_id}-{step_task_id}" if run_id else step_task_id
        agent = next(
            (name for name in worker_results if name.endswith(f"-{step_task_id}")),
            None,
        )
        worker_id = agent.split(":", 1)[-1] if agent else runtime_task_id
        record = worker_records.get(worker_id, {})
        prompt = _first_user_text(conversations, step_task_id, runtime_task_id)
        started, finished, duration_ms = _event_window(events, str(step.get("title") or ""))
        observed_start, observed_end = observed_windows.get(worker_id, (None, None))
        if started is None:
            started = observed_start
        if finished is None:
            finished = observed_end
        if started and finished:
            from datetime import datetime

            try:
                duration_ms = (
                    datetime.fromisoformat(finished) - datetime.fromisoformat(started)
                ).total_seconds() * 1000.0
            except ValueError:
                duration_ms = None
        calls = [call for call in tool_calls if call.get("agent") == agent]
        row = {
            "step_task_id": step_task_id,
            "runtime_task_id": runtime_task_id,
            "title": step.get("title"),
            "depends_on": step.get("depends_on") or [],
            "worker_id": worker_id,
            "profile_id": record.get("profile_id"),
            "worker_created_at": record.get("created_at"),
            "started_at": started,
            "finished_at": finished,
            "duration_ms": duration_ms,
            "prompt_chars": len(prompt),
            "prompt_bytes": len(prompt.encode("utf-8")),
            "prompt": prompt,
            "agent": agent,
            "agent_success": bool((worker_results.get(agent) or {}).get("success"))
            if agent
            else None,
            "output": str((worker_results.get(agent) or {}).get("output") or ""),
            "tool_calls": len(calls),
            "tool_names": [call.get("tool_name") for call in calls],
        }
        by_task[step_task_id] = row
        step_rows.append(row)

    edges: list[dict[str, Any]] = []
    for row in step_rows:
        downstream_prompt = row["prompt"]
        downstream_calls = [call for call in tool_calls if call.get("agent") == row.get("agent")]
        for dependency in row["depends_on"]:
            upstream = by_task.get(str(dependency))
            if upstream is None:
                edges.append(
                    {
                        "from": dependency,
                        "to": row["step_task_id"],
                        "handoff_complete": False,
                        "consumed": False,
                        "reason": "upstream step is absent",
                    }
                )
                continue
            upstream_output = upstream["output"]
            if not upstream_output.strip():
                edges.append(
                    {
                        "from": dependency,
                        "to": row["step_task_id"],
                        "handoff_complete": False,
                        "consumed": False,
                        "reason": "upstream produced no output",
                    }
                )
                continue
            in_prompt = upstream_output.strip() in downstream_prompt
            downstream_tool_text = "\n".join(
                f"{call.get('input')}\n{call.get('output')}" for call in downstream_calls
            )
            token = upstream_output.strip()[:160]
            consumed_by_tool = bool(token and token in downstream_tool_text)
            edges.append(
                {
                    "from": dependency,
                    "to": row["step_task_id"],
                    "handoff_bytes": len(upstream_output.encode("utf-8")) if in_prompt else 0,
                    "handoff_complete": bool(in_prompt),
                    "consumed": bool(in_prompt or consumed_by_tool),
                    "consumed_via": "prompt"
                    if in_prompt
                    else ("tool" if consumed_by_tool else None),
                }
            )

    return {
        "run_id": run_id,
        "sample_id": result.get("sample_id"),
        "variant": (result.get("environment") or {}).get("variant"),
        "success": bool(result.get("success")),
        "failure_class": result.get("failure_class"),
        "oracle_passed": bool((result.get("oracle") or {}).get("passed")),
        "worker_results": worker_results,
        "steps": step_rows,
        "edges": edges,
        "handoff_complete": all(edge.get("handoff_complete") for edge in edges) if edges else True,
        "handoff_consumed": all(edge.get("consumed") for edge in edges) if edges else True,
        "dag_nodes": len(step_rows),
        "critical_path_nodes": _critical_path(edges),
        "metrics": result.get("metrics") or {},
    }


def _critical_path(edges: list[dict[str, Any]]) -> int:
    predecessors: dict[str, set[str]] = {}
    for edge in edges:
        predecessors.setdefault(str(edge["to"]), set()).add(str(edge["from"]))
    depth: dict[str, int] = {}

    def visit(node: str, seen: set[str]) -> int:
        if node in depth:
            return depth[node]
        if node in seen:
            return 1
        parents = predecessors.get(node, set())
        value = 1 if not parents else 1 + max(visit(parent, seen | {node}) for parent in parents)
        depth[node] = value
        return value

    return max((visit(node, set()) for node in predecessors), default=1)


def summarize_evidence(run_dirs: list[str | Path]) -> dict[str, Any]:
    runs = [collect_evidence(path) for path in run_dirs]
    if not runs:
        return {"runs": 0}
    handoff = [run for run in runs if run["edges"]]
    return {
        "runs": len(runs),
        "success": sum(1 for run in runs if run["success"]),
        "handoff_runs": len(handoff),
        "handoff_complete": sum(1 for run in handoff if run["handoff_complete"]),
        "handoff_consumed": sum(1 for run in handoff if run["handoff_consumed"]),
        "worker_failures": sum(
            1 for run in runs for step in run["steps"] if step.get("agent_success") is False
        ),
        "dag_nodes": sum(run["dag_nodes"] for run in runs),
        "runs_detail": runs,
    }
