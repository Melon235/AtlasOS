"""Stage R1-0: read-only attribution tests on synthetic evidence (no provider, no sandbox)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from experiments.evaluation.models import canonical_digest
from experiments.evaluation.stage_r1_0_analysis import (
    BUDGET_EXHAUSTED,
    DELIVERABLE_INCOMPLETE,
    WORKSPACE_INTERFERENCE,
    RequestAttribution,
    RunContext,
    _function_state,
    attribute_requests,
    build_run,
    deliverable_state,
    digest_dictionary,
    duplicate_work,
    paths_in_text,
    workspace_interference,
)

STUB = '''"""Frozen consumer module. Implement it against docs/INTERFACE.md."""


def summarize(rows: list[tuple[str, int]]) -> str:
    """Return '<earliest>|<latest>|<total>' for the given (date, amount) rows."""
    raise NotImplementedError("summarize")
'''

IMPLEMENTED = '''"""Frozen consumer module. Implement it against docs/INTERFACE.md."""

from providers.clock import from_epoch, to_epoch


def summarize(rows: list[tuple[str, int]]) -> str:
    if not rows:
        raise ValueError("summarize() requires at least one row")
    epochs = [(to_epoch(text), amount) for text, amount in rows]
    return (
        f"{from_epoch(min(value for value, _ in epochs))}|"
        f"{from_epoch(max(value for value, _ in epochs))}|"
        f"{sum(amount for _, amount in epochs)}"
    )
'''


# --------------------------------------------------------------------------- attribution


def test_attribute_requests_joins_by_request_id() -> None:
    samples = [
        {
            "request_id": 1,
            "worker": "coding_worker:run-a",
            "actual_prompt_tokens": 100,
            "actual_completion_tokens": 10,
        },
        {
            "request_id": 2,
            "worker": "coding_worker:run-b",
            "actual_prompt_tokens": 200,
            "actual_completion_tokens": 20,
        },
    ]
    calls = [
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cache_hit_tokens": 60,
            "cache_miss_tokens": 40,
        },
        {
            "prompt_tokens": 200,
            "completion_tokens": 20,
            "cache_hit_tokens": 150,
            "cache_miss_tokens": 50,
        },
    ]
    rows, quality = attribute_requests(samples, calls)
    assert [row.quality for row in rows] == ["EXACT", "EXACT"]
    assert rows[0].cache_hit_tokens == 60
    assert rows[1].worker == "coding_worker:run-b"
    assert quality["unmatched_calls"] == 0


def test_attribute_requests_falls_back_to_the_token_pair() -> None:
    # The settlement order differs from the request order under concurrency.
    samples = [
        {
            "request_id": 1,
            "worker": "w1",
            "actual_prompt_tokens": 200,
            "actual_completion_tokens": 20,
        },
        {
            "request_id": 2,
            "worker": "w2",
            "actual_prompt_tokens": 100,
            "actual_completion_tokens": 10,
        },
    ]
    calls = [
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cache_hit_tokens": 1,
            "cache_miss_tokens": 2,
        },
        {
            "prompt_tokens": 200,
            "completion_tokens": 20,
            "cache_hit_tokens": 3,
            "cache_miss_tokens": 4,
        },
    ]
    rows, quality = attribute_requests(samples, calls)
    assert [row.quality for row in rows] == ["MULTISET", "MULTISET"]
    assert rows[0].cache_hit_tokens == 3
    assert quality["multiset"] == 2


def test_attribute_requests_marks_ambiguous_instead_of_guessing() -> None:
    samples = [
        {
            "request_id": 9,
            "worker": "w1",
            "actual_prompt_tokens": 100,
            "actual_completion_tokens": 10,
        },
        {
            "request_id": 10,
            "worker": "w2",
            "actual_prompt_tokens": 100,
            "actual_completion_tokens": 10,
        },
    ]
    calls = [
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cache_hit_tokens": 1,
            "cache_miss_tokens": 2,
        },
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cache_hit_tokens": 3,
            "cache_miss_tokens": 4,
        },
    ]
    rows, quality = attribute_requests(samples, calls)
    assert [row.quality for row in rows] == ["AMBIGUOUS", "AMBIGUOUS"]
    assert all(row.cache_hit_tokens is None for row in rows)
    assert quality["ambiguous"] == 2


# --------------------------------------------------------------------------- deliverables


def test_function_state_detects_a_stub_with_a_docstring() -> None:
    state = _function_state(STUB)
    assert state["functions"] == ["summarize"]
    assert state["stubs"] == ["summarize"]


def test_function_state_sees_an_implemented_module() -> None:
    state = _function_state(IMPLEMENTED)
    assert state["stubs"] == []
    assert "providers.clock" in state["imports"]


def test_deliverable_state_distinguishes_stub_missing_and_implemented(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(STUB, encoding="utf-8")
    (tmp_path / "b.py").write_text(IMPLEMENTED, encoding="utf-8")
    assert deliverable_state(tmp_path, "a.py", STUB)["state"] == "STUB"
    assert deliverable_state(tmp_path, "b.py", STUB)["state"] == "IMPLEMENTED"
    assert deliverable_state(tmp_path, "c.py", STUB)["state"] == "MISSING"


# --------------------------------------------------------------------------- paths


def test_paths_in_text_normalises_the_workspace_prefix() -> None:
    paths = paths_in_text("cat /workspace/project/ledger/money.py and ./docs/INTERFACE.md")
    assert paths == {"ledger/money.py", "docs/INTERFACE.md"}


# --------------------------------------------------------------------------- interference


def _worker(task_id: str, path: str, state: str) -> dict[str, Any]:
    return {
        "worker_key": f"eval-1-{task_id}",
        "task_id": task_id,
        "deliverables": [{"path": path, "state": state}],
    }


def _bash(worker: str, command: str) -> dict[str, Any]:
    return {
        "worker": worker,
        "tool": "bash",
        "input": {"command": command},
        "ts": "2026-11-11T00:00:00Z",
        "state": "COMPLETED",
    }


def test_interference_detects_a_sibling_revert() -> None:
    workers = [
        _worker("entries", "ledger/entries.py", "IMPLEMENTED"),
        _worker("money", "ledger/money.py", "STUB"),
    ]
    calls = [_bash("eval-1-entries", "cd /workspace/project && git checkout -- ledger/money.py")]
    findings = workspace_interference(workers, calls)
    assert len(findings) == 1
    assert findings[0]["target"] == "ledger/money.py"
    assert findings[0]["victim_state"] == "STUB"


def test_interference_ignores_read_only_git_and_own_files() -> None:
    workers = [
        _worker("entries", "ledger/entries.py", "IMPLEMENTED"),
        _worker("money", "ledger/money.py", "IMPLEMENTED"),
    ]
    calls = [
        _bash("eval-1-entries", "git stash list && git reflog | head && git status --short"),
        _bash("eval-1-entries", "git checkout -- ledger/entries.py"),
        _bash("eval-1-money", "git restore ledger/money.py"),
    ]
    assert workspace_interference(workers, calls) == []


def test_interference_flags_a_whole_tree_reset() -> None:
    workers = [_worker("a", "state.json", "STUB")]
    calls = [_bash("eval-1-a", "cd /workspace/project && git reset --hard HEAD~0")]
    findings = workspace_interference(workers, calls)
    assert findings and findings[0]["target"] == "WHOLE_TREE"


# --------------------------------------------------------------------------- duplicates


def test_duplicate_work_counts_shared_reads_and_touches() -> None:
    calls = [
        {
            "worker": "eval-1-a",
            "tool": "file_read",
            "input": {"path": "/workspace/project/ledger/money.py"},
            "ts": "1",
        },
        {
            "worker": "eval-1-b",
            "tool": "bash",
            "input": {"command": "cat ledger/money.py"},
            "ts": "2",
        },
    ]
    duplicate = duplicate_work([], calls)
    assert duplicate["shared_read_count"] == 0  # only one explicit file_read
    assert duplicate["shared_touches"]["ledger/money.py"] == ["eval-1-a", "eval-1-b"]


# --------------------------------------------------------------------------- end to end


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _transcript(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, event in enumerate(events, start=1):
        lines.append(
            json.dumps(
                {
                    "seq": index,
                    "ts": event["ts"],
                    "kind": "lifecycle",
                    "format_version": 1,
                    "payload": {"atlas_trajectory": event["event"]},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _proposal(worker: str, tool: str, call_id: str, digest: str, ts: str) -> dict[str, Any]:
    return {
        "ts": ts,
        "event": {
            "event_type": "action_proposed",
            "correlation": {"worker_id": worker, "tool_id": tool, "call_id": call_id},
            "data": {"input_digest": digest},
        },
    }


def _outcome(worker: str, tool: str, call_id: str, state: str, ts: str) -> dict[str, Any]:
    return {
        "ts": ts,
        "event": {
            "event_type": "execution_outcome",
            "correlation": {"worker_id": worker, "tool_id": tool, "call_id": call_id},
            "data": {"state": state},
        },
    }


def test_build_run_recovers_a_lost_worker_and_flags_interference(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    workspace = run_dir / "workspace"
    (workspace / "consumers").mkdir(parents=True)
    (workspace / "providers").mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)
    from experiments.evaluation.stage_r05_tasks import DEFINITIONS

    definition = next(item for item in DEFINITIONS if item.sample_id == "r05-p3-frozen-interface")
    (workspace / "consumers" / "report.py").write_text(
        definition.visible_files["consumers/report.py"], encoding="utf-8"
    )
    (workspace / "providers" / "clock.py").write_text("# implemented\n", encoding="utf-8")
    write_call = {"path": "/workspace/project/consumers/report.py", "content": IMPLEMENTED}
    revert = {"command": "cd /workspace/project && git checkout -- consumers/report.py"}
    read_contract = {"path": "/workspace/project/docs/INTERFACE.md"}

    result = {
        "identity": {"run_id": "run", "git_commit": "abc", "provider": "fake", "model": "m"},
        "environment": {"budget_mode": "guarded"},
        "worker_results": {
            "coding_worker:run-clock": {
                "success": True,
                "output": "done",
                "stop_reason": {"code": "success", "message": "ok"},
            },
            "coding_worker:run-summary": {
                "success": True,
                "output": "done",
                "stop_reason": {"code": "success", "message": "ok"},
            },
        },
        "tool_calls": [
            {
                "agent": "coding_worker:run-clock",
                "tool_name": "bash",
                "input": revert,
                "output": "restored",
                "duration_ms": 5.0,
            },
            {
                "agent": "coding_worker:run-summary",
                "tool_name": "file_write",
                "input": write_call,
                "output": "Overwrote",
                "duration_ms": 5.0,
            },
        ],
        "metrics": {
            "prompt_tokens": 300,
            "completion_tokens": 30,
            "budget": {
                "limit_tokens": 1000,
                "used_tokens": 330,
                "remaining_tokens": 670,
                "stop_reason": None,
                "mode": "guarded",
                "workers": {
                    "coding_worker:run-clock": {"actual_tokens": 110, "completed": 1, "started": 1},
                    "coding_worker:run-summary": {
                        "actual_tokens": 220,
                        "completed": 2,
                        "started": 2,
                    },
                },
                "samples": [
                    {
                        "request_id": 1,
                        "worker": "coding_worker:run-clock",
                        "actual_prompt_tokens": 100,
                        "actual_completion_tokens": 10,
                        "actual_tokens": 110,
                    },
                    {
                        "request_id": 2,
                        "worker": "coding_worker:run-summary",
                        "actual_prompt_tokens": 90,
                        "actual_completion_tokens": 10,
                        "actual_tokens": 100,
                    },
                    {
                        "request_id": 3,
                        "worker": "coding_worker:run-summary",
                        "actual_prompt_tokens": 110,
                        "actual_completion_tokens": 10,
                        "actual_tokens": 120,
                    },
                ],
            },
            "routing": {"decision": {"route": "delegate_parallel"}},
        },
        "oracle": {"passed": False, "checks": {"hidden_tests_passed": False}},
        "success": False,
        "failure_class": "ORACLE_FAILURE",
    }
    _write_json(run_dir / "result.json", result)
    _write_json(
        run_dir / "workers.json",
        {
            "workers": {
                "run-clock": {
                    "profile_id": "coding_worker",
                    "assignment": {"task": "Read /workspace/project/docs/INTERFACE.md"},
                },
                "run-summary": {
                    "profile_id": "coding_worker",
                    "assignment": {"task": "Implement /workspace/project/consumers/report.py"},
                },
            }
        },
    )
    (run_dir / "provider_calls.jsonl").write_text(
        "\n".join(
            json.dumps(call)
            for call in (
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "cache_hit_tokens": 40,
                    "cache_miss_tokens": 60,
                    "cost_usd": 0.001,
                },
                {
                    "prompt_tokens": 90,
                    "completion_tokens": 10,
                    "cache_hit_tokens": 30,
                    "cache_miss_tokens": 60,
                    "cost_usd": 0.001,
                },
                {
                    "prompt_tokens": 110,
                    "completion_tokens": 10,
                    "cache_hit_tokens": 50,
                    "cache_miss_tokens": 60,
                    "cost_usd": 0.001,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        run_dir / "stage_e_evidence.json",
        {
            "task_events": [
                {
                    "type": "task_start",
                    "title": "run-clock",
                    "ts": "2026-11-11T00:00:00Z",
                    "task": "t1",
                    "agent": "run-clock",
                },
                {"type": "task_complete", "ts": "2026-11-11T00:00:05Z", "task": "t1"},
                {
                    "type": "task_start",
                    "title": "run-summary",
                    "ts": "2026-11-11T00:00:00Z",
                    "task": "t2",
                    "agent": "run-summary",
                },
                {"type": "task_complete", "ts": "2026-11-11T00:00:06Z", "task": "t2"},
            ]
        },
    )
    _transcript(
        run_dir / "trajectory" / "run" / "transcript.jsonl",
        [
            _proposal(
                "run-summary",
                "file_write",
                "c1",
                canonical_digest(write_call),
                "2026-11-11T00:00:01Z",
            ),
            _outcome("run-summary", "file_write", "c1", "COMPLETED", "2026-11-11T00:00:02Z"),
            _proposal("run-clock", "bash", "c2", canonical_digest(revert), "2026-11-11T00:00:03Z"),
            _outcome("run-clock", "bash", "c2", "COMPLETED", "2026-11-11T00:00:04Z"),
            _proposal(
                "run-clock",
                "file_read",
                "c3",
                canonical_digest(read_contract),
                "2026-11-11T00:00:03Z",
            ),
        ],
    )

    context = RunContext(
        round_id="round",
        track="a",
        definition="r05-p3-frozen-interface",
        arm="a2",
        repetition=0,
        run_dir=run_dir,
        manifest={"benchmark_version": "r0.5.2"},
        result=result,
        evidence=json.loads((run_dir / "stage_e_evidence.json").read_text(encoding="utf-8")),
        provider_calls=[
            json.loads(line)
            for line in (run_dir / "provider_calls.jsonl").read_text(encoding="utf-8").splitlines()
        ],
        workers_json=json.loads((run_dir / "workers.json").read_text(encoding="utf-8")),
        definition_digest="digest",
        definition_current=True,
    )
    run = build_run(context)
    codes = {reason["code"] for reason in run["attribution"]["reasons"]}
    assert WORKSPACE_INTERFERENCE in codes
    assert run["attribution"]["primary"] == WORKSPACE_INTERFERENCE
    assert run["tokens"]["actions"]["observed_actions"] == 3
    assert run["tokens"]["actions"]["recorded_calls"] == 2
    assert run["tokens"]["actions"]["undercount"] == 1
    summary_worker = next(w for w in run["workers"] if w["task_id"] == "summary")
    assert summary_worker["prompt_tokens"] == 200
    assert any(item["write_completed_but_absent"] for item in summary_worker["deliverables"])
    clock_worker = next(w for w in run["workers"] if w["task_id"] == "clock")
    assert clock_worker["read_calls"] == 1


def test_digest_dictionary_recovers_a_repeated_input() -> None:
    payload = {"command": "find /workspace/project -type f"}
    dictionary = digest_dictionary([{"tool_name": "bash", "input": payload}])
    assert dictionary[canonical_digest(payload)]["input"] == payload


@pytest.mark.skipif(
    not (Path("experiments") / "results" / "stage-r0-5").is_dir(),
    reason="raw Stage R0.5 run directories are not part of the repository",
)
def test_collect_only_reads_the_run_directories() -> None:
    from experiments.evaluation.stage_r1_0_analysis import collect

    before = sorted(
        (path.relative_to(Path("experiments")), path.stat().st_mtime_ns)
        for path in (Path("experiments") / "results" / "stage-r0-5").rglob("*")
        if path.is_file()
    )
    payload = collect()
    after = sorted(
        (path.relative_to(Path("experiments")), path.stat().st_mtime_ns)
        for path in (Path("experiments") / "results" / "stage-r0-5").rglob("*")
        if path.is_file()
    )
    assert payload["run_count"] == 30
    assert before == after


def test_primary_priority_prefers_deliverable_loss_over_budget() -> None:
    from experiments.evaluation.stage_r1_0_analysis import _primary

    reasons = [{"code": BUDGET_EXHAUSTED}, {"code": DELIVERABLE_INCOMPLETE}]
    assert _primary(reasons, False) == DELIVERABLE_INCOMPLETE
    assert _primary([], True) == "NONE"


def test_request_attribution_dataclass_is_immutable() -> None:
    row = RequestAttribution(1, "w", 10, 1, 5, 5, "EXACT")
    with pytest.raises(AttributeError):
        row.worker = "other"  # type: ignore[misc]
