"""Stage R1-0: read-only failure attribution over the Stage R0.5 runs.

The module answers "where did the extra tokens and the failures come from?" from evidence that
already exists on disk. It never calls a provider, never touches a sandbox, never writes into a run
directory and never re-runs a task.

Evidence and how it is joined
-----------------------------
* ``result.json`` — oracle checks, worker results, tool calls (with agent, tool name, input,
  output), budget ledger (per-request samples carry the worker that made the request).
* ``provider_calls.jsonl`` — per-request prompt/completion/cache tokens and cost, in request order.
* ``trajectory/*/transcript.jsonl`` — event timestamps and the tool-call input digest.
* ``workers.json`` — the exact assignment text each worker received.
* ``workspace/`` — the exported workspace, used for AST-level deliverable checks.

Two joins make worker-level attribution exact instead of estimated:

1. budget sample ``request_id`` → ``provider_calls`` index (request_id == index + 1). Verified per
   run; when the order differs, the multiset of (prompt, completion) pairs must still match, and the
   residual requests are marked ``AMBIGUOUS`` instead of guessed.
2. tool call input → ``sha256(canonical_json(input))`` → ``action_proposed.input_digest`` from the
   trajectory, which supplies the timestamp. Coverage is reported, not assumed.

Anything that cannot be recovered is reported as ``UNAVAILABLE``. No field is estimated silently.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.models import canonical_digest
from experiments.evaluation.stage_r05_analysis import parse_intervals, timeline_metrics
from experiments.evaluation.stage_r05_tasks import DEFINITIONS, benchmark_digest, preregistration

REPO_ROOT = Path(__file__).resolve().parents[2]
R05_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r0-5"
OUT_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r1-0"
EVIDENCE_ROOT = REPO_ROOT / "experiments" / "evidence"

NEVER = "NEVER"
UNAVAILABLE = "UNAVAILABLE"

#: Failure codes (Stage R1-0 §B6). A run may carry several.
CONTEXT_NOT_DELIVERED = "CONTEXT_NOT_DELIVERED"
CONTEXT_AVAILABLE_BUT_NOT_READ = "CONTEXT_AVAILABLE_BUT_NOT_READ"
CONTEXT_READ_BUT_MISUNDERSTOOD = "CONTEXT_READ_BUT_MISUNDERSTOOD"
REDUNDANT_EXPLORATION = "REDUNDANT_EXPLORATION"
IMPLEMENTATION_ERROR = "IMPLEMENTATION_ERROR"
DELIVERABLE_INCOMPLETE = "DELIVERABLE_INCOMPLETE"
PUBLIC_TEST_NOT_RUN = "PUBLIC_TEST_NOT_RUN"
PUBLIC_TEST_FAILED = "PUBLIC_TEST_FAILED"
INTEGRATION_NOT_VERIFIED = "INTEGRATION_NOT_VERIFIED"
INTEGRATION_FAILED = "INTEGRATION_FAILED"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
MAX_TURNS = "MAX_TURNS"
WORKSPACE_INTERFERENCE = "WORKSPACE_INTERFERENCE"
UNKNOWN = "UNKNOWN"

CODE_LABELS = {
    CONTEXT_NOT_DELIVERED: "the needed fact was never delivered to the worker",
    CONTEXT_AVAILABLE_BUT_NOT_READ: "the fact was public and pointed at, but never read",
    CONTEXT_READ_BUT_MISUNDERSTOOD: "the worker read the fact and implemented against it wrongly",
    REDUNDANT_EXPLORATION: "two workers repeated the same reads or commands",
    IMPLEMENTATION_ERROR: "the artifact is wrong although the contract was available",
    DELIVERABLE_INCOMPLETE: "the declared artifact is missing, unchanged or still a stub",
    PUBLIC_TEST_NOT_RUN: "no public test was executed by the worker",
    PUBLIC_TEST_FAILED: "a public test was executed and failed",
    INTEGRATION_NOT_VERIFIED: "no cross-module check was ever executed in this run",
    INTEGRATION_FAILED: "a cross-module check was executed and failed",
    BUDGET_EXHAUSTED: "the shared budget guard stopped the worker",
    MAX_TURNS: "the per-worker turn limit stopped the worker",
    WORKSPACE_INTERFERENCE: (
        "another worker reverted or deleted a path it does not own in the shared workspace"
    ),
    UNKNOWN: "no evidence-backed mechanism was identified",
}

#: Repository-level commands a worker can use to undo a sibling's write in the shared workspace.
#: Read-only forms (``git stash list``, ``git reflog``, ``git status``) must not match.
REVERT_PATHS = re.compile(r"git\s+(?:checkout|restore)\s+(?:--\s+)?(?P<path>[^\s;&|]+)")
REVERT_TREE = re.compile(
    r"git\s+reset\s+--hard\b"
    r"|git\s+stash(?:\s+(?:push|save|clear|drop))?\s*(?:[;&|]|$)"
    r"|git\s+clean\s+-[a-zA-Z]*f"
)

TEST_MARKERS = ("pytest", "unittest", "python -m pytest", "python3 -m pytest")
#: Bash commands that execute project code ad hoc, i.e. the only verification surface these
#: fixtures offer (none of them ships a public test suite).
ADHOC_MARKERS = ("python3 -c", "python -c", "python3 - <<", "python - <<", "python3 ", "python ")


# --------------------------------------------------------------------------- required facts


#: Facts that the *public* task material states and that a worker needs. Each entry names where the
#: fact lives (a file that is staged into the sandbox) and the worker that needs it. Nothing here is
#: derived from the hidden oracle.
@dataclass(frozen=True)
class RequiredFact:
    fact_id: str
    worker_task_id: str
    statement: str
    source_location: str
    symptom: str


FAMILY_FACTS: dict[str, tuple[RequiredFact, ...]] = {
    "P1": (
        RequiredFact(
            "p1-pricing-contract",
            "pricing",
            "the exception each pricing function raises and the value of the prescribed calls",
            "catalog/pricing.py",
            "report line missing or wrong",
        ),
        RequiredFact(
            "p1-inventory-contract",
            "inventory",
            "the exception set of reserve and the values of the prescribed calls",
            "catalog/inventory.py",
            "report line missing or wrong",
        ),
    ),
    "P2": (
        RequiredFact(
            "p2-entries-contract",
            "entries",
            "parse_entry strips each field before validating it; three fields; date shape only",
            "ledger/entries.py",
            "parse_entry rejected a padded field",
        ),
        RequiredFact(
            "p2-money-contract",
            "money",
            "format/parse/split rules including separator and decimal validation",
            "ledger/money.py",
            "money helper rejected or formatted a value wrongly",
        ),
    ),
    "P3": (
        RequiredFact(
            "p3-epoch-zero",
            "clock",
            "to_epoch('1970-01-01') == 0 and from_epoch is its inverse",
            "docs/INTERFACE.md",
            "epoch offset was wrong",
        ),
        RequiredFact(
            "p3-malformed-dates",
            "clock",
            "impossible or malformed dates raise ValueError",
            "docs/INTERFACE.md",
            "invalid dates were accepted or valid ones rejected",
        ),
        RequiredFact(
            "p3-summary-shape",
            "summary",
            "'<earliest>|<latest>|<total>' using the frozen contract, empty list raises ValueError",
            "docs/INTERFACE.md",
            "summarize output or error behaviour was wrong",
        ),
    ),
}


# --------------------------------------------------------------------------- primitives


def _load(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000.0, 3)


def _worker_key(agent: str) -> str:
    return agent.split(":")[-1]


def _task_id_of(worker_key: str, run_id: str) -> str:
    prefix = f"{run_id}-"
    return worker_key[len(prefix) :] if worker_key.startswith(prefix) else worker_key


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
                elif block.get("input") is not None:
                    parts.append(json.dumps(block["input"], sort_keys=True))
    return "\n".join(parts)


# --------------------------------------------------------------------------- provider attribution


@dataclass(frozen=True)
class RequestAttribution:
    request_id: int
    worker: str | None
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    quality: str  # EXACT | MULTISET | AMBIGUOUS


def attribute_requests(
    samples: list[dict[str, Any]], calls: list[dict[str, Any]]
) -> tuple[list[RequestAttribution], dict[str, Any]]:
    """Join budget samples (which name the worker) to provider calls (which carry cache metrics).

    Primary join: ``request_id == call index + 1`` with equal prompt tokens. Residual samples are
    matched to unused calls by their (prompt, completion) pair; duplicate pairs are attributed only
    when the pairing is unique, otherwise the cache fields are left as ``AMBIGUOUS``.
    """

    ordered = sorted(samples, key=lambda item: int(item.get("request_id") or 0))
    used = [False] * len(calls)
    assigned: dict[int, tuple[dict[str, Any], str]] = {}

    # Pass 1: positional join where the prompt tokens agree. Claiming these first keeps a later
    # sample from stealing an exact match during the fallback pass.
    for sample in ordered:
        request_id = int(sample.get("request_id") or 0)
        index = request_id - 1
        prompt = int(sample.get("actual_prompt_tokens") or 0)
        if 0 <= index < len(calls) and not used[index] and calls[index]["prompt_tokens"] == prompt:
            used[index] = True
            assigned[request_id] = (calls[index], "EXACT")

    # Pass 2: residual samples matched by their (prompt, completion) pair, only when unique.
    for sample in ordered:
        request_id = int(sample.get("request_id") or 0)
        if request_id in assigned:
            continue
        prompt = int(sample.get("actual_prompt_tokens") or 0)
        completion = int(sample.get("actual_completion_tokens") or 0)
        candidates = [
            position
            for position, call in enumerate(calls)
            if not used[position]
            and call["prompt_tokens"] == prompt
            and call["completion_tokens"] == completion
        ]
        if len(candidates) == 1:
            used[candidates[0]] = True
            assigned[request_id] = (calls[candidates[0]], "MULTISET")

    rows: list[RequestAttribution] = []
    for sample in ordered:
        request_id = int(sample.get("request_id") or 0)
        prompt = int(sample.get("actual_prompt_tokens") or 0)
        completion = int(sample.get("actual_completion_tokens") or 0)
        worker = sample.get("worker")
        match = assigned.get(request_id)
        if match is None:
            rows.append(
                RequestAttribution(request_id, worker, prompt, completion, None, None, "AMBIGUOUS")
            )
            continue
        call, quality = match
        rows.append(
            RequestAttribution(
                request_id,
                worker,
                prompt,
                completion,
                call.get("cache_hit_tokens"),
                call.get("cache_miss_tokens"),
                quality,
            )
        )

    counts = Counter(row.quality for row in rows)
    leftover = sum(1 for flag in used if not flag)
    return rows, {
        "requests": len(rows),
        "calls": len(calls),
        "exact": counts["EXACT"],
        "multiset": counts["MULTISET"],
        "ambiguous": counts["AMBIGUOUS"],
        "unmatched_calls": leftover,
    }


# --------------------------------------------------------------------------- tool timeline


def digest_dictionary(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index every recorded tool input by its digest across all runs.

    A digest identifies an input exactly, so recovering a lost action's input from a *different*
    run is evidence, not inference. Unique inputs stay ``UNAVAILABLE``.
    """

    dictionary: dict[str, dict[str, Any]] = {}
    for call in calls:
        payload = call.get("input") or {}
        dictionary[canonical_digest(payload)] = {
            "tool": str(call.get("tool_name") or ""),
            "input": payload,
        }
    return dictionary


def action_events(
    run_dir: Path,
    calls: list[dict[str, Any]],
    dictionary: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rebuild the action list from the trajectory, then attach inputs where they were recorded.

    ``result.json`` drops the tool calls of a worker that the budget guard stopped: the trajectory
    still records the proposal and its outcome. Counting only ``result.json`` therefore
    under-reports exactly the failing workers, so the trajectory is the primary inventory and
    ``result.json`` is used only to fill in the input text
    (matched by ``sha256(canonical_json(input))``).
    """

    proposals: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    outcomes: dict[str, str] = {}
    for transcript in sorted((run_dir / "trajectory").rglob("transcript.jsonl")):
        for line in transcript.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            event = (record.get("payload") or {}).get("atlas_trajectory") or {}
            kind = event.get("event_type")
            if kind not in {"action_proposed", "execution_outcome"}:
                continue
            correlation = event.get("correlation") or {}
            call_id = str(correlation.get("call_id") or "")
            if not call_id:
                continue
            if kind == "action_proposed":
                if call_id in proposals:
                    continue
                proposals[call_id] = {
                    "call_id": call_id,
                    "worker": str(correlation.get("worker_id") or ""),
                    "tool": str(correlation.get("tool_id") or ""),
                    "ts": str(record.get("ts") or ""),
                    "digest": str((event.get("data") or {}).get("input_digest") or ""),
                    "state": None,
                }
                order.append(call_id)
            else:
                outcomes[call_id] = str((event.get("data") or {}).get("state") or "")

    pools: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        worker = _worker_key(str(call.get("agent") or ""))
        digest = canonical_digest(call.get("input") or {})
        pools[(worker, digest)].append(call)
    used: Counter[tuple[str, str]] = Counter()

    events: list[dict[str, Any]] = []
    recovered = 0
    recovered_cross_run = 0
    for call_id in order:
        proposal = proposals[call_id]
        key = (proposal["worker"], proposal["digest"])
        pool = pools.get(key) or []
        index = used[key]
        recorded = pool[index] if index < len(pool) else None
        source = UNAVAILABLE
        if recorded is not None:
            used[key] += 1
            recovered += 1
            source = "result.json"
        payload = (recorded or {}).get("input") or None
        if payload is None and dictionary:
            known = dictionary.get(proposal["digest"])
            if known is not None:
                payload = known["input"]
                payload = payload if payload else None
                source = "cross_run_digest"
                recovered_cross_run += 1
        events.append(
            {
                "worker": proposal["worker"],
                "tool": proposal["tool"],
                "ts": proposal["ts"],
                "state": outcomes.get(call_id),
                "input": payload,
                "output": str((recorded or {}).get("output") or "") or None,
                "duration_ms": (recorded or {}).get("duration_ms"),
                "input_source": source if payload is not None else UNAVAILABLE,
            }
        )
    observed = len(events)
    resolved = sum(1 for event in events if event["input"] is not None)
    return events, {
        "observed_actions": observed,
        "recorded_calls": len(calls),
        "recovered_inputs": recovered,
        "recovered_cross_run": recovered_cross_run,
        "input_unavailable": observed - resolved,
        "undercount": max(0, observed - len(calls)),
    }


# --------------------------------------------------------------------------- deliverable AST


def _function_state(source: str) -> dict[str, Any]:
    """Public functions, which of them are still ``raise NotImplementedError``, and imports."""

    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return {"parsed": False, "error": str(error), "functions": [], "stubs": [], "imports": []}
    functions: list[str] = []
    stubs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
            body = list(node.body)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            if len(body) == 1 and isinstance(body[0], ast.Raise):
                raised = body[0].exc
                target = raised.func if isinstance(raised, ast.Call) else raised
                name = getattr(target, "id", None) or getattr(target, "attr", None)
                if name == "NotImplementedError":
                    stubs.append(node.name)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    return {
        "parsed": True,
        "functions": sorted(functions),
        "stubs": sorted(stubs),
        "imports": sorted(set(imports)),
    }


def deliverable_state(
    workspace: Path,
    path: str,
    stub_source: str | None,
) -> dict[str, Any]:
    target = workspace / path
    if not target.is_file():
        return {"path": path, "present": False, "changed": False, "state": "MISSING"}
    content = target.read_text(encoding="utf-8", errors="replace")
    changed = stub_source is not None and content.strip() != stub_source.strip()
    detail: dict[str, Any] = {
        "path": path,
        "present": True,
        "changed": changed,
        "chars": len(content),
        "state": "PRESENT",
    }
    if path.endswith(".py"):
        detail.update(_function_state(content))
        detail["state"] = (
            "STUB"
            if detail.get("stubs") and not changed
            else "PARTIAL"
            if detail.get("stubs")
            else "IMPLEMENTED"
        )
    elif path.endswith(".md"):
        detail["mentions"] = sorted({name for name in _REPORT_SYMBOLS if name in content.lower()})
    return detail


_REPORT_SYMBOLS = (
    "tier_rate",
    "final_price",
    "bulk_discount",
    "cheapest",
    "available",
    "reserve",
    "low_stock",
    "total_units",
)


# --------------------------------------------------------------------------- run context


@dataclass
class RunContext:
    round_id: str
    track: str
    definition: str
    arm: str
    repetition: int
    run_dir: Path
    manifest: dict[str, Any]
    result: dict[str, Any]
    evidence: dict[str, Any]
    provider_calls: list[dict[str, Any]]
    workers_json: dict[str, Any]
    definition_digest: str
    definition_current: bool
    run: dict[str, Any] = field(default_factory=dict)


def _rounds(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, _load(path, {}))
        for path in sorted(root.glob("r05-*/manifest.json"))
        if _load(path, {}).get("track")
    ]


def collect_contexts(root: Path = R05_ROOT) -> tuple[list[RunContext], list[dict[str, Any]]]:
    """Load every run, keeping the version filter explicit instead of silently filtering."""

    frozen = {item.sample_id: benchmark_digest(item) for item in DEFINITIONS}
    contexts: list[RunContext] = []
    excluded: list[dict[str, Any]] = []
    for _manifest_path, manifest in _rounds(root):
        digests = manifest.get("positive_digests") or {}
        for pair in manifest.get("pairs") or []:
            definition_id = str(pair.get("definition") or "")
            recorded = digests.get(definition_id)
            current = frozen.get(definition_id)
            definition_ok = not (recorded and current and recorded != current)
            for arm, payload in sorted((pair.get("runs") or {}).items()):
                run_dir = Path(str(payload.get("run_dir") or ""))
                if not run_dir.is_dir():
                    excluded.append(
                        {
                            "round_id": manifest.get("round_id"),
                            "definition": definition_id,
                            "arm": arm,
                            "reason": "RUN_DIRECTORY_MISSING",
                        }
                    )
                    continue
                if not definition_ok:
                    excluded.append(
                        {
                            "round_id": manifest.get("round_id"),
                            "definition": definition_id,
                            "arm": arm,
                            "reason": "STALE_BENCHMARK_DEFINITION",
                            "recorded_digest": recorded,
                            "frozen_digest": current,
                            "run_dir": run_dir.name,
                        }
                    )
                    continue
                result = _load(run_dir / "result.json", {})
                contexts.append(
                    RunContext(
                        round_id=str(manifest.get("round_id") or ""),
                        track=str(manifest.get("track") or ""),
                        definition=definition_id,
                        arm=arm,
                        repetition=int(pair.get("repetition") or 0),
                        run_dir=run_dir,
                        manifest=manifest,
                        result=result,
                        evidence=_load(run_dir / "stage_e_evidence.json", {}),
                        provider_calls=[
                            json.loads(line)
                            for line in (run_dir / "provider_calls.jsonl")
                            .read_text(encoding="utf-8")
                            .splitlines()
                            if line.strip()
                        ]
                        if (run_dir / "provider_calls.jsonl").is_file()
                        else [],
                        workers_json=_load(run_dir / "workers.json", {}),
                        definition_digest=str(recorded or ""),
                        definition_current=definition_ok,
                    )
                )
    return contexts, excluded


# --------------------------------------------------------------------------- per-worker ledger


def _assignment_text(context: RunContext, worker_key: str) -> str:
    workers = (context.workers_json or {}).get("workers") or {}
    record = workers.get(worker_key) or {}
    return str((record.get("assignment") or {}).get("task") or "")


def _budget_share(context: RunContext) -> dict[str, Any]:
    budget = (context.result.get("metrics") or {}).get("budget") or {}
    return budget.get("workers") or {}


def build_workers(
    context: RunContext, dictionary: dict[str, dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = context.result
    metrics = result.get("metrics") or {}
    budget = metrics.get("budget") or {}
    samples = budget.get("samples") or []
    attributions, join_quality = attribute_requests(samples, context.provider_calls)
    stamped, action_quality = action_events(
        context.run_dir, result.get("tool_calls") or [], dictionary
    )

    by_worker_requests: dict[str, list[RequestAttribution]] = defaultdict(list)
    for row in attributions:
        if row.worker:
            # Budget samples name the worker with its profile prefix ("coding_worker:<run>-<task>").
            by_worker_requests[_worker_key(str(row.worker))].append(row)
    by_worker_calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in stamped:
        by_worker_calls[call["worker"]].append(call)

    run_id = str((result.get("identity") or {}).get("run_id") or context.run_dir.name)
    intervals = {item.agent.split(":")[-1]: item for item in parse_intervals(context.evidence)}
    budget_workers = _budget_share(context)
    workspace = context.run_dir / "workspace"
    definition = next((item for item in DEFINITIONS if item.sample_id == context.definition), None)

    workers: list[dict[str, Any]] = []
    for agent, info in sorted((result.get("worker_results") or {}).items()):
        key = _worker_key(agent)
        task_id = _task_id_of(key, run_id)
        requests = by_worker_requests.get(key) or []
        calls = sorted(by_worker_calls.get(key) or [], key=lambda item: item["ts"] or "")
        prompt_tokens = sum(row.prompt_tokens for row in requests)
        completion_tokens = sum(row.completion_tokens for row in requests)
        cache_hit = sum(
            row.cache_hit_tokens or 0 for row in requests if row.cache_hit_tokens is not None
        )
        cache_miss = sum(
            row.cache_miss_tokens or 0 for row in requests if row.cache_miss_tokens is not None
        )
        cache_known = all(row.quality == "EXACT" for row in requests) and bool(requests)
        write_calls = [call for call in calls if call["tool"] in {"file_write", "file_edit"}]
        read_calls = [call for call in calls if call["tool"] == "file_read"]
        bash_calls = [call for call in calls if call["tool"] == "bash"]
        bash_texts = [str((call.get("input") or {}).get("command") or "") for call in bash_calls]
        test_calls = [
            call
            for call, text in zip(bash_calls, bash_texts, strict=True)
            if any(marker in text for marker in TEST_MARKERS)
        ]
        adhoc_calls = [
            call
            for call, text in zip(bash_calls, bash_texts, strict=True)
            if text
            and not any(marker in text for marker in TEST_MARKERS)
            and any(marker in text for marker in ADHOC_MARKERS)
        ]
        adhoc_failed = [
            call
            for call in adhoc_calls
            if any(marker in str(call.get("output") or "") for marker in ("Traceback", "Error"))
        ]
        interval = intervals.get(key)
        assignment = _assignment_text(context, key)

        deliverable_paths: list[str] = []
        if definition is not None:
            # The worker's own declared artifacts: the ones its assignment names. This is
            # independent of what the worker actually did, so an untouched stub is still reported.
            deliverable_paths = sorted(
                path
                for path in definition.required_artifacts
                if path in assignment or path.rsplit("/", 1)[-1] in assignment
            )
            written = {
                str((call.get("input") or {}).get("path") or "").replace("/workspace/project/", "")
                for call in write_calls
                if call.get("input")
            }
            deliverable_paths = sorted({*deliverable_paths, *[path for path in written if path]})

        completed_writes = {
            str((call.get("input") or {}).get("path") or "").replace("/workspace/project/", "")
            for call in write_calls
            if call.get("input") and call.get("state") == "COMPLETED"
        }
        deliverables = []
        for path in deliverable_paths:
            stub = None
            if definition is not None:
                stub = definition.visible_files.get(path)
            item = deliverable_state(workspace, path, stub)
            item["write_completed_but_absent"] = bool(
                path in completed_writes and item["state"] in {"MISSING", "STUB"}
            )
            deliverables.append(item)

        stop = info.get("stop_reason") or {}
        worker = {
            "worker_key": key,
            "task_id": task_id,
            "profile_id": ((context.workers_json or {}).get("workers") or {})
            .get(key, {})
            .get("profile_id"),
            "assigned_chars": len(assignment),
            "assigned_mentions_contract": _mentions_contract(assignment, task_id, definition),
            "requests": len(requests),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_hit_tokens": cache_hit if cache_known else UNAVAILABLE,
            "cache_miss_tokens": cache_miss if cache_known else UNAVAILABLE,
            "cost_usd": round(
                sum(
                    float(context.provider_calls[row.request_id - 1].get("cost_usd") or 0.0)
                    for row in requests
                    if 0 <= row.request_id - 1 < len(context.provider_calls)
                ),
                6,
            ),
            "first_request_prompt_tokens": requests[0].prompt_tokens if requests else UNAVAILABLE,
            "last_request_prompt_tokens": requests[-1].prompt_tokens if requests else UNAVAILABLE,
            "tool_calls": len(calls),
            "tool_calls_recorded_in_result_json": sum(
                1 for call in calls if call.get("input") is not None
            ),
            "tool_calls_by_name": dict(Counter(call["tool"] for call in calls)),
            "read_calls": len(read_calls),
            "write_calls": len(write_calls),
            "bash_calls": len(bash_calls),
            "public_test_attempts": len(test_calls),
            "adhoc_verification_attempts": len(adhoc_calls),
            "adhoc_verification_failures": len(adhoc_failed),
            "first_activity": interval.start.isoformat() if interval else UNAVAILABLE,
            "last_activity": interval.end.isoformat() if interval and interval.end else UNAVAILABLE,
            "duration_ms": interval.duration_ms if interval else UNAVAILABLE,
            "first_write": write_calls[0]["ts"] if write_calls else NEVER,
            "last_write": write_calls[-1]["ts"] if write_calls else NEVER,
            "tool_calls_after_last_write": (
                sum(
                    1
                    for call in calls
                    if write_calls and call["ts"] and call["ts"] > write_calls[-1]["ts"]
                )
                if write_calls
                else 0
            ),
            "budget": budget_workers.get(f"coding_worker:{key}") or budget_workers.get(key) or {},
            "self_success": bool(info.get("success")),
            "produced_output": bool(str(info.get("output") or "").strip()),
            "stop_code": str(stop.get("code") or UNAVAILABLE),
            "stop_message": str(stop.get("message") or "")[:300],
            "deliverables": deliverables,
            "handoff_consumed": _handoff_consumed(context, task_id),
        }
        workers.append(worker)

    stats = {
        "join": join_quality,
        "actions": action_quality,
        "timeline": timeline_metrics(list(intervals.values())),
    }
    return workers, stats


def _mentions_contract(assignment: str, task_id: str, definition: Any) -> dict[str, Any]:
    if definition is None:
        return {}
    declared = (definition.frozen_contracts or {}).get(task_id) or ()
    return {path: path in assignment for path in declared}


def _handoff_consumed(context: RunContext, task_id: str) -> Any:
    handoff = (context.result.get("metrics") or {}).get("handoff")
    if not handoff:
        return UNAVAILABLE
    text = json.dumps(handoff)
    return task_id in text


# --------------------------------------------------------------------------- budget timeline


def budget_timeline(context: RunContext) -> dict[str, Any]:
    budget = (context.result.get("metrics") or {}).get("budget") or {}
    samples = budget.get("samples") or []
    limit = budget.get("limit_tokens")
    used = 0
    rows: list[dict[str, Any]] = []
    for sample in sorted(samples, key=lambda item: int(item.get("request_id") or 0)):
        tokens = int(sample.get("actual_tokens") or 0)
        used += tokens
        rows.append(
            {
                "request_id": int(sample.get("request_id") or 0),
                "worker": str(sample.get("worker") or ""),
                "tokens": tokens,
                "cumulative_tokens": used,
                "remaining_tokens": (int(limit) - used) if isinstance(limit, int) else None,
            }
        )
    by_worker: dict[str, int] = defaultdict(int)
    for row in rows:
        by_worker[row["worker"]] += row["tokens"]
    total = sum(by_worker.values()) or 1
    last_request_by_worker: dict[str, int] = {}
    for row in rows:
        last_request_by_worker[row["worker"]] = row["request_id"]
    return {
        "limit_tokens": limit,
        "used_tokens": budget.get("used_tokens"),
        "remaining_tokens": budget.get("remaining_tokens"),
        "stop_reason": budget.get("stop_reason"),
        "mode": budget.get("mode"),
        "requests": len(rows),
        "share": {worker: round(value / total, 4) for worker, value in by_worker.items()},
        "tokens_by_worker": dict(by_worker),
        "last_request_id_by_worker": last_request_by_worker,
        "timeline": rows,
    }


# --------------------------------------------------------------------------- redundancy


_PATH_IN_TEXT = re.compile(r"(?:[\w.-]+/)*[\w.-]+\.(?:py|md|json|txt|toml|yaml|yml)")


def paths_in_text(text: str) -> set[str]:
    found: set[str] = set()
    for match in _PATH_IN_TEXT.finditer(text):
        raw = re.sub(r"^/?workspace/project/", "", match.group(0)).lstrip("./")
        if raw and not raw.startswith("/"):
            found.add(raw)
    return found


def duplicate_work(workers: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    reads: dict[str, list[str]] = defaultdict(list)
    commands: dict[str, list[str]] = defaultdict(list)
    touches: dict[str, set[str]] = defaultdict(set)
    for call in calls:
        worker = call["worker"]
        payload = call.get("input") or {}
        if call["tool"] == "file_read":
            path = str(payload.get("path") or "").replace("/workspace/project/", "")
            if path:
                reads[path].append(worker)
        if call["tool"] == "bash":
            command = " ".join(str(payload.get("command") or "").split())
            if command:
                commands[command].append(worker)
                for path in paths_in_text(command):
                    touches[path].add(worker)
        for key in ("path", "old_string", "new_string", "content"):
            value = payload.get(key)
            if isinstance(value, str):
                for path in paths_in_text(value):
                    touches[path].add(worker)
    shared_reads = {
        path: sorted(set(owners)) for path, owners in reads.items() if len(set(owners)) > 1
    }
    shared_commands = {
        command: sorted(set(owners)) for command, owners in commands.items() if len(set(owners)) > 1
    }
    internal_repeats = {
        path: len(owners) for path, owners in reads.items() if len(owners) > len(set(owners))
    }
    shared_touches = {path: sorted(owners) for path, owners in touches.items() if len(owners) > 1}
    return {
        "shared_reads": shared_reads,
        "shared_read_count": len(shared_reads),
        "shared_touches": shared_touches,
        "shared_touch_count": len(shared_touches),
        "distinct_touched_paths": len(touches),
        "shared_bash_commands": shared_commands,
        "shared_bash_count": len(shared_commands),
        "repeated_reads_within_worker": internal_repeats,
        "repeated_reads_within_worker_count": len(internal_repeats),
        "distinct_reads": len(reads),
        "distinct_commands": len(commands),
    }


# --------------------------------------------------------------------------- context audit


def context_audit(
    context: RunContext, workers: list[dict[str, Any]], calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    family = next((item.family for item in DEFINITIONS if item.sample_id == context.definition), "")
    facts = FAMILY_FACTS.get(family, ())
    rows: list[dict[str, Any]] = []
    for fact in facts:
        worker = next((item for item in workers if item["task_id"] == fact.worker_task_id), None)
        if worker is None:
            continue
        assigned = _assignment_text(context, str(worker["worker_key"]))
        basename = fact.source_location.rsplit("/", 1)[-1]
        pointed_at = fact.source_location in assigned or basename in assigned
        read = any(
            call["worker"] == worker["worker_key"]
            and (
                fact.source_location
                == str((call["input"] or {}).get("path") or "").replace("/workspace/project/", "")
                or (
                    call["tool"] == "bash"
                    and basename in str((call["input"] or {}).get("command") or "")
                )
            )
            for call in calls
        )
        rows.append(
            {
                "fact_id": fact.fact_id,
                "worker_task_id": fact.worker_task_id,
                "required_fact": fact.statement,
                "source_location": fact.source_location,
                "worker_received_pointer": pointed_at,
                "worker_read_source": read,
                "worker_failed": not worker["self_success"] or worker["stop_code"] != "success",
                "failure_symptom": fact.symptom,
                "case": (
                    "READ" if read else "AVAILABLE_BUT_NOT_READ" if pointed_at else "NOT_DELIVERED"
                ),
            }
        )
    return rows


# --------------------------------------------------------------------------- classification


def workspace_interference(
    workers: list[dict[str, Any]], calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Find commands where one worker undid a path another worker owns.

    All workers share one sandbox, so ``git checkout -- <sibling file>`` is authorised by the
    action gate and silently destroys a completed deliverable. Only explicit, visible commands are
    reported: the target must match a declared artifact of a *different* worker, or be a whole-tree
    reset.
    """

    owners: dict[str, str] = {}
    states: dict[str, str] = {}
    for worker in workers:
        for item in worker["deliverables"]:
            owners[item["path"]] = worker["task_id"]
            states[item["path"]] = item["state"]

    findings: list[dict[str, Any]] = []
    for call in calls:
        if call["tool"] != "bash" or not call.get("input"):
            continue
        command = str((call["input"] or {}).get("command") or "")
        if not command:
            continue
        targets = [
            match.group("path").strip("'\"")
            for match in REVERT_PATHS.finditer(command)
            if not match.group("path").startswith("-")
        ]
        if REVERT_TREE.search(command):
            targets.append("WHOLE_TREE")
        for raw in targets:
            target = raw.replace("/workspace/project/", "").lstrip("./")
            if target in {"", ".", "*", "WHOLE_TREE"}:
                victims = sorted(
                    path for path, state in states.items() if state in {"STUB", "MISSING"}
                )
                findings.append(
                    {
                        "worker": _worker_key(str(call["worker"])),
                        "command": " ".join(command.split())[:200],
                        "target": "WHOLE_TREE",
                        "victim_paths": victims,
                        "ts": call["ts"],
                    }
                )
                continue
            if target not in owners:
                continue
            owner = owners[target]
            if _worker_key(str(call["worker"])).endswith(f"-{owner}"):
                # A worker reverting its own artifact is not interference.
                continue
            findings.append(
                {
                    "worker": _worker_key(str(call["worker"])),
                    "command": " ".join(command.split())[:200],
                    "target": target,
                    "victim_task_id": owner,
                    "victim_state": states.get(target),
                    "ts": call["ts"],
                }
            )
    return findings


def _stop_codes(context: RunContext) -> list[str]:
    return sorted(
        str((value.get("stop_reason") or {}).get("code") or "unknown")
        for value in (context.result.get("worker_results") or {}).values()
    )


def _evidence_of_call(call: dict[str, Any], limit: int = 160) -> str:
    target = call.get("input") or {}
    text = str(target.get("path") or target.get("command") or "")
    return " ".join(text.split())[:limit]


def classify(
    context: RunContext,
    workers: list[dict[str, Any]],
    duplicate: dict[str, Any],
    facts: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    interference: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = context.result
    oracle = result.get("oracle") or {}
    checks = oracle.get("checks") or {}
    passed = bool(oracle.get("passed"))
    stop_codes = _stop_codes(context)
    reasons: list[dict[str, Any]] = []

    def add(
        code: str,
        symptom: str,
        evidence: list[str],
        confidence: str,
        counter: list[str] | None = None,
    ) -> None:
        reasons.append(
            {
                "code": code,
                "symptom": symptom,
                "evidence": [item for item in evidence if item],
                "confidence": confidence,
                "counterevidence": counter or [],
            }
        )

    budget = (context.result.get("metrics") or {}).get("budget") or {}
    if "budget_exceeded" in stop_codes:
        add(
            BUDGET_EXHAUSTED,
            "the shared budget guard ended at least one worker",
            [
                f"stop codes={stop_codes}",
                f"budget stop_reason={budget.get('stop_reason')}",
                f"remaining_tokens={budget.get('remaining_tokens')}",
            ],
            "HIGH",
        )
    if "max_turns" in stop_codes:
        add(MAX_TURNS, "a worker reached the turn limit", [f"stop codes={stop_codes}"], "HIGH")

    incomplete = [
        f"{worker['task_id']}:{item['path']}={item['state']}"
        for worker in workers
        for item in worker["deliverables"]
        if item["state"] in {"MISSING", "STUB"}
    ]
    vanished = [
        f"{worker['task_id']}:{item['path']}"
        for worker in workers
        for item in worker["deliverables"]
        if item.get("write_completed_but_absent")
    ]
    if incomplete:
        add(
            DELIVERABLE_INCOMPLETE,
            "a declared artifact is missing or still a stub at export time",
            [
                *incomplete,
                *([f"completed write but absent at export: {vanished}"] if vanished else []),
            ],
            "HIGH",
            counter=(
                ["the worker may have reverted its own write before finishing"] if vanished else []
            ),
        )

    definition = next((item for item in DEFINITIONS if item.sample_id == context.definition), None)
    public_tests = sorted(
        path
        for path in (definition.visible_files if definition else {})
        if path.startswith("tests/") or path.rsplit("/", 1)[-1].startswith("test_")
    )
    tests = sum(worker["public_test_attempts"] for worker in workers)
    if public_tests and tests == 0 and not passed:
        add(
            PUBLIC_TEST_NOT_RUN,
            "the fixture ships a public test suite and no worker executed it",
            [f"public tests: {public_tests}", f"pytest/unittest calls: 0 of {len(calls)}"],
            "HIGH",
            counter=["a public test is not always required to produce a correct artifact"],
        )

    integration_attempts = [
        call for call in calls if call["tool"] == "bash" and _looks_like_integration(call)
    ]
    integration_failures = [
        call
        for call in integration_attempts
        if any(marker in str(call.get("output") or "") for marker in ("Traceback", "Error"))
    ]
    if integration_failures and not passed:
        add(
            INTEGRATION_FAILED,
            "a cross-module check was executed and failed",
            [
                f"{len(integration_failures)} failing command(s)",
                _evidence_of_call(integration_failures[-1]),
            ],
            "MEDIUM",
            counter=[
                "the failure may be a probe written by the worker, not the delivered artifact"
            ],
        )
    if not integration_attempts and not passed and len(workers) > 1:
        add(
            INTEGRATION_NOT_VERIFIED,
            "no cross-module or end-to-end check was executed",
            [
                f"workers={len(workers)}",
                "no bash command imported two modules or ran a combined test",
            ],
            "MEDIUM",
            counter=[
                "a frozen-interface split makes local integration impossible until both finish"
            ],
        )

    if duplicate["shared_read_count"] or duplicate["shared_bash_count"]:
        add(
            REDUNDANT_EXPLORATION,
            "two workers repeated the same read or command",
            [
                f"shared reads: {sorted(duplicate['shared_reads'])}",
                f"shared commands: {len(duplicate['shared_bash_commands'])}",
            ],
            "HIGH",
        )

    if not passed and checks.get("hidden_tests_passed") is False:
        for row in facts:
            if not row["worker_failed"]:
                continue
            reference = f"source={row['source_location']} worker={row['worker_task_id']}"
            if row["case"] == "AVAILABLE_BUT_NOT_READ":
                add(
                    CONTEXT_AVAILABLE_BUT_NOT_READ,
                    f"{row['fact_id']}: the source was public and pointed at, but never read",
                    [reference, f"pointer_in_prompt={row['worker_received_pointer']}"],
                    "MEDIUM",
                    counter=["the same rule also appears in the stub docstring the worker edited"],
                )
            elif row["case"] == "READ":
                add(
                    CONTEXT_READ_BUT_MISUNDERSTOOD,
                    f"{row['fact_id']}: the source was read and the implementation contradicts it",
                    [reference, "the source file appears in the worker's own tool calls"],
                    "HIGH",
                    counter=["reading a file is not proof that the relevant lines were used"],
                )
        if not incomplete:
            add(
                IMPLEMENTATION_ERROR,
                "the artifact exists and is implemented, but the hidden test rejects it",
                [f"hidden_tests_passed={checks.get('hidden_tests_passed')}"],
                "HIGH",
            )

    if interference:
        add(
            WORKSPACE_INTERFERENCE,
            "a worker reverted a path owned by another worker in the shared workspace",
            [f"{item['worker'].split('-')[-1]} -> {item['command']}" for item in interference[:4]],
            "HIGH",
            counter=["the revert may have happened after the artifact was already graded"],
        )

    if not reasons and not passed:
        add(UNKNOWN, "no evidence-backed mechanism identified", [], "LOW")

    return {
        "run_id": str((result.get("identity") or {}).get("run_id") or context.run_dir.name),
        "oracle_passed": passed,
        "failure_class": result.get("failure_class"),
        "primary": _primary(reasons, passed),
        "reasons": reasons,
        "verification": {
            "public_tests_in_fixture": public_tests,
            "public_test_attempts": tests,
            "adhoc_attempts": sum(worker["adhoc_verification_attempts"] for worker in workers),
            "adhoc_failures": sum(worker["adhoc_verification_failures"] for worker in workers),
            "integration_attempts": len(integration_attempts),
            "integration_failures": len(integration_failures),
        },
    }


def _looks_like_integration(call: dict[str, Any]) -> bool:
    command = str((call["input"] or {}).get("command") or "")
    if "pytest" in command and ("tests/" in command or "-k" in command):
        return True
    imports = command.count("import")
    return imports >= 2 or "from consumers" in command or "from providers" in command


def _primary(reasons: list[dict[str, Any]], passed: bool) -> str:
    if passed:
        return "NONE"
    priority = [
        WORKSPACE_INTERFERENCE,
        DELIVERABLE_INCOMPLETE,
        BUDGET_EXHAUSTED,
        MAX_TURNS,
        CONTEXT_READ_BUT_MISUNDERSTOOD,
        CONTEXT_AVAILABLE_BUT_NOT_READ,
        IMPLEMENTATION_ERROR,
        INTEGRATION_NOT_VERIFIED,
        PUBLIC_TEST_NOT_RUN,
        UNKNOWN,
    ]
    present = {reason["code"] for reason in reasons}
    for code in priority:
        if code in present:
            return code
    return UNKNOWN


# --------------------------------------------------------------------------- aggregation


def build_run(
    context: RunContext, dictionary: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    workers, stats = build_workers(context, dictionary)
    stamped, _ = action_events(context.run_dir, context.result.get("tool_calls") or [], dictionary)
    duplicate = duplicate_work(workers, stamped)
    facts = context_audit(context, workers, stamped)
    interference = workspace_interference(workers, stamped)
    attribution = classify(context, workers, duplicate, facts, stamped, interference)
    metrics = context.result.get("metrics") or {}
    identity = context.result.get("identity") or {}
    routing = metrics.get("routing") or {}
    decision = routing.get("decision") or {}
    budget = budget_timeline(context)
    return {
        "run_id": identity.get("run_id"),
        "round_id": context.round_id,
        "track": context.track,
        "definition": context.definition,
        "family": next(
            (item.family for item in DEFINITIONS if item.sample_id == context.definition), ""
        ),
        "arm": context.arm,
        "repetition": context.repetition,
        "benchmark_version": context.manifest.get("benchmark_version"),
        "benchmark_digest": context.definition_digest or UNAVAILABLE,
        "definition_current": context.definition_current,
        "policy_version": decision.get("policy_version"),
        "policy_digest": decision.get("policy_digest"),
        "git_commit": identity.get("git_commit"),
        "git_dirty": identity.get("git_dirty"),
        "provider": identity.get("provider"),
        "model": identity.get("model"),
        "config_digest": identity.get("config_digest"),
        "fixture_digest": identity.get("fixture_digest"),
        "execution": {
            "handoff_mode": (context.result.get("environment") or {}).get("handoff_mode"),
            "budget_mode": (context.result.get("environment") or {}).get("budget_mode"),
            "compaction_mode": (context.result.get("environment") or {}).get("compaction_mode"),
            "tool_result_mode": (context.result.get("environment") or {}).get("tool_result_mode"),
            "worker_contract_mode": (context.result.get("environment") or {}).get(
                "worker_contract_mode"
            ),
        },
        "route": decision.get("route"),
        "route_reasons": decision.get("reason_codes"),
        "executed_worker_count": routing.get("executed_worker_count"),
        "oracle": context.result.get("oracle") or {},
        "success": bool(context.result.get("success")),
        "failure_class": context.result.get("failure_class"),
        "stop_codes": _stop_codes(context),
        "tokens": {
            "prompt_tokens": metrics.get("prompt_tokens"),
            "completion_tokens": metrics.get("completion_tokens"),
            "cache_hit_tokens": metrics.get("cache_hit_tokens"),
            "cache_miss_tokens": metrics.get("cache_miss_tokens"),
            "cost_usd": metrics.get("estimated_cost_usd"),
            "model_turns": metrics.get("model_turns"),
            "tool_calls": metrics.get("tool_calls"),
            "join": stats["join"],
            "actions": stats["actions"],
        },
        "timeline": stats["timeline"],
        "budget": budget,
        "workers": workers,
        "duplicate_work": duplicate,
        "context_facts": facts,
        "interference": interference,
        "attribution": attribution,
    }


def collect(root: Path = R05_ROOT) -> dict[str, Any]:
    contexts, excluded = collect_contexts(root)
    dictionary = digest_dictionary(
        [call for context in contexts for call in context.result.get("tool_calls") or []]
    )
    runs = [build_run(context, dictionary) for context in contexts]
    return {
        "root": str(root),
        "preregistration": preregistration(),
        "run_count": len(runs),
        "excluded": excluded,
        "runs": runs,
    }


def write_outputs(payload: dict[str, Any], root: Path = OUT_ROOT) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    rows = [_run_row(run) for run in payload["runs"]]
    if rows:
        with (root / "runs.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    with (root / "workers.jsonl").open("w", encoding="utf-8") as handle:
        for run in payload["runs"]:
            for worker in run["workers"]:
                handle.write(
                    json.dumps(
                        {
                            "run_id": run["run_id"],
                            "definition": run["definition"],
                            "arm": run["arm"],
                            "repetition": run["repetition"],
                            **worker,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
    (root / "attribution.json").write_text(
        json.dumps([run["attribution"] for run in payload["runs"]], indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _run_row(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "definition": run["definition"],
        "arm": run["arm"],
        "repetition": run["repetition"],
        "benchmark_version": run["benchmark_version"],
        "policy_version": run["policy_version"],
        "oracle_passed": run["oracle"]["passed"],
        "success": run["success"],
        "failure_class": run["failure_class"],
        "stop_codes": ";".join(run["stop_codes"]),
        "primary_reason": run["attribution"]["primary"],
        "reasons": ";".join(sorted({item["code"] for item in run["attribution"]["reasons"]})),
        "workers": len(run["workers"]),
        "prompt_tokens": run["tokens"]["prompt_tokens"],
        "completion_tokens": run["tokens"]["completion_tokens"],
        "cost_usd": run["tokens"]["cost_usd"],
        "model_turns": run["tokens"]["model_turns"],
        "tool_calls": run["tokens"]["tool_calls"],
        "cache_join_exact": run["tokens"]["join"]["exact"],
        "cache_join_ambiguous": run["tokens"]["join"]["ambiguous"],
        "actions_observed": run["tokens"]["actions"]["observed_actions"],
        "actions_input_unavailable": run["tokens"]["actions"]["input_unavailable"],
        "actions_undercount": run["tokens"]["actions"]["undercount"],
        "max_observed_concurrency": run["timeline"]["max_observed_concurrency"],
        "overlap_pairs": run["timeline"]["overlap_pair_count"],
        "wall_ms": run["timeline"]["total_run_wall_time_ms"],
        "budget_used": run["budget"]["used_tokens"],
        "budget_limit": run["budget"]["limit_tokens"],
    }


# --------------------------------------------------------------------------- report


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    runs = payload["runs"]
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        by_arm[f"{run['track']}:{run['arm']}"].append(run)

    arms: dict[str, Any] = {}
    for key, group in sorted(by_arm.items()):
        workers = [worker for run in group for worker in run["workers"]]
        arms[key] = {
            "runs": len(group),
            "oracle_pass": sum(1 for run in group if run["oracle"]["passed"]),
            "task_success": sum(1 for run in group if run["success"]),
            "budget_units": sum(run["budget"]["limit_tokens"] or 0 for run in group),
            "prompt_tokens": sum(run["tokens"]["prompt_tokens"] or 0 for run in group),
            "completion_tokens": sum(run["tokens"]["completion_tokens"] or 0 for run in group),
            "cost_usd": round(sum(run["tokens"]["cost_usd"] or 0.0 for run in group), 6),
            "worker_turns": sum(worker["requests"] for worker in workers),
            "worker_tool_calls": sum(worker["tool_calls"] for worker in workers),
            "first_request_prompt_tokens": sum(
                worker["first_request_prompt_tokens"]
                for worker in workers
                if isinstance(worker["first_request_prompt_tokens"], int)
            ),
            "public_test_attempts": sum(worker["public_test_attempts"] for worker in workers),
            "workers": len(workers),
            "stop_codes": dict(Counter(code for run in group for code in run["stop_codes"])),
            "primary_reasons": dict(Counter(run["attribution"]["primary"] for run in group)),
            "wall_ms_mean": _mean(
                [run["timeline"]["total_run_wall_time_ms"] or 0 for run in group]
            ),
            "max_concurrency": max(
                (run["timeline"]["max_observed_concurrency"] or 0 for run in group), default=0
            ),
        }

    families: dict[str, Any] = {}
    for run in runs:
        key = f"{run['track']}:{run['definition']}"
        entry = families.setdefault(
            key,
            {
                "definition": run["definition"],
                "family": run["family"],
                "track": run["track"],
                "arms": defaultdict(
                    lambda: {
                        "runs": 0,
                        "oracle_pass": 0,
                        "prompt_tokens": 0,
                        "cost_usd": 0.0,
                        "worker_tokens": 0,
                        "workers": 0,
                        "primary": Counter(),
                    }
                ),
            },
        )
        arm = entry["arms"][run["arm"]]
        arm["runs"] += 1
        arm["oracle_pass"] += int(bool(run["oracle"]["passed"]))
        arm["prompt_tokens"] += run["tokens"]["prompt_tokens"] or 0
        arm["cost_usd"] = round(arm["cost_usd"] + (run["tokens"]["cost_usd"] or 0.0), 6)
        arm["workers"] += len(run["workers"])
        arm["worker_tokens"] += sum(worker["prompt_tokens"] for worker in run["workers"])
        arm["primary"][run["attribution"]["primary"]] += 1
    for entry in families.values():
        for arm in entry["arms"].values():
            arm["primary"] = dict(arm["primary"])

    outcomes = Counter()
    for run in runs:
        if run["oracle"]["passed"] and run["success"]:
            outcomes["oracle_pass_and_task_success"] += 1
        elif run["oracle"]["passed"]:
            outcomes["oracle_pass_but_harness_failed"] += 1
        else:
            outcomes["oracle_failed"] += 1

    return {
        "benchmark_version": payload["preregistration"]["benchmark_version"],
        "run_count": payload["run_count"],
        "excluded": payload["excluded"],
        "arms": arms,
        "families": {
            key: {**value, "arms": dict(value["arms"])} for key, value in families.items()
        },
        "outcomes": dict(outcomes),
        "stop_codes": dict(Counter(code for run in runs for code in run["stop_codes"])),
        "interference_runs": [
            {
                "run_id": run["run_id"],
                "definition": run["definition"],
                "arm": run["arm"],
                "repetition": run["repetition"],
                "findings": run["interference"],
            }
            for run in runs
            if run["interference"]
        ],
        "reason_counts": dict(
            Counter(reason["code"] for run in runs for reason in run["attribution"]["reasons"])
        ),
        "primary_counts": dict(Counter(run["attribution"]["primary"] for run in runs)),
        "context_fact_counts": dict(
            Counter(row["case"] for run in runs for row in run["context_facts"])
        ),
        "deliverable_states": dict(
            Counter(
                item["state"]
                for run in runs
                for worker in run["workers"]
                for item in worker["deliverables"]
            )
        ),
        "join_quality": {
            "exact": sum(run["tokens"]["join"]["exact"] for run in runs),
            "multiset": sum(run["tokens"]["join"]["multiset"] for run in runs),
            "ambiguous": sum(run["tokens"]["join"]["ambiguous"] for run in runs),
            "unmatched_calls": sum(run["tokens"]["join"]["unmatched_calls"] for run in runs),
            "actions_observed": sum(run["tokens"]["actions"]["observed_actions"] for run in runs),
            "actions_recovered": sum(run["tokens"]["actions"]["recovered_inputs"] for run in runs),
            "actions_input_unavailable": sum(
                run["tokens"]["actions"]["input_unavailable"] for run in runs
            ),
            "actions_undercount": sum(run["tokens"]["actions"]["undercount"] for run in runs),
            "recorded_calls": sum(run["tokens"]["actions"]["recorded_calls"] for run in runs),
        },
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Stage R1-0 attribution summary (generated)",
        "",
        f"Runs analysed: {summary['run_count']}; benchmark version {summary['benchmark_version']}",
        f"Excluded runs: {summary['excluded'] or 'none'}",
        "",
        "| Group | Runs | Oracle | Prompt | Cost USD | Turns | Fixed overhead | Tests |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, value in summary["arms"].items():
        lines.append(
            f"| {key} | {value['runs']} | {value['oracle_pass']} | {value['prompt_tokens']} | "
            f"{value['cost_usd']} | {value['worker_turns']} | "
            f"{value['first_request_prompt_tokens']} | {value['public_test_attempts']} |"
        )
    lines += ["", "## Primary attribution", ""]
    for code, count in sorted(summary["primary_counts"].items(), key=lambda item: -item[1]):
        lines.append(f"* `{code}` — {count} run(s): {CODE_LABELS.get(code, '')}")
    lines += ["", "## Run outcomes", ""]
    for key, value in sorted(summary["outcomes"].items()):
        lines.append(f"* {key}: {value}")
    lines += ["", "## Stop codes", ""]
    for key, value in sorted(summary["stop_codes"].items()):
        lines.append(f"* `{key}`: {value}")
    lines += ["", "## Workspace interference", ""]
    if summary["interference_runs"]:
        for item in summary["interference_runs"]:
            for finding in item["findings"]:
                lines.append(
                    f"* {item['definition']} {item['arm']} rep{item['repetition']}: "
                    f"{finding['worker'].split('-')[-1]} -> {finding.get('target')} "
                    f"({finding.get('victim_state', 'n/a')})"
                )
    else:
        lines.append("* none")
    lines += ["", "## All observed codes", ""]
    for code, count in sorted(summary["reason_counts"].items(), key=lambda item: -item[1]):
        lines.append(f"* `{code}` — {count}")
    lines += ["", "## Context fact cases", ""]
    for case, count in sorted(summary["context_fact_counts"].items()):
        lines.append(f"* {case}: {count}")
    lines += ["", "## Deliverable states", ""]
    for state, count in sorted(summary["deliverable_states"].items()):
        lines.append(f"* {state}: {count}")
    join = summary["join_quality"]
    lines += [
        "",
        "## Evidence join quality",
        "",
        f"* request attribution: exact {join['exact']}, multiset {join['multiset']}, "
        f"ambiguous {join['ambiguous']}, unmatched calls {join['unmatched_calls']}",
        f"* actions observed in the trajectory: {join['actions_observed']}; "
        f"inputs recovered from result.json: {join['actions_recovered']}; "
        f"input UNAVAILABLE: {join['actions_input_unavailable']}; "
        f"actions missing from result.json: {join['actions_undercount']}",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- publishing


def publish_r05_evidence(root: Path = R05_ROOT, destination: Path | None = None) -> dict[str, Any]:
    """Write sanitized, recomputable R0.5 aggregates (no absolute paths, no raw logs)."""

    from experiments.evaluation.stage_r05_analysis import summarize as summarize_r05

    target = destination or (EVIDENCE_ROOT / "stage-r0-5")
    target.mkdir(parents=True, exist_ok=True)
    summary = summarize_r05(root)
    summary.pop("root", None)
    for row in summary["runs"]:
        row.pop("run_dir", None)
    for row in summary["paired"]:
        row.pop("left_route", None)
        row.pop("right_route", None)
    (target / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    rows = summary["runs"]
    if rows:
        with (target / "runs.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (target / "timeline_summary.json").write_text(
        json.dumps(summary["parallel"], indent=2, sort_keys=True), encoding="utf-8"
    )
    (target / "benchmark_digests.json").write_text(
        json.dumps(
            {
                "preregistration": preregistration(),
                "versions": summary["versions"],
                "families": [
                    {
                        "sample_id": item.sample_id,
                        "family": item.family,
                        "digest": benchmark_digest(item),
                    }
                    for item in DEFINITIONS
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "destination": str(target),
        "files": sorted(path.name for path in target.iterdir() if path.is_file()),
    }


def publish_r10_evidence(
    payload: dict[str, Any], destination: Path | None = None
) -> dict[str, Any]:
    """Write the sanitized R1-0 ledger (per-run and per-worker rows, no conversations)."""

    target = destination or (EVIDENCE_ROOT / "stage-r1-0")
    target.mkdir(parents=True, exist_ok=True)
    rows = [_run_row(run) for run in payload["runs"]]
    with (target / "runs.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    worker_rows = []
    for run in payload["runs"]:
        for worker in run["workers"]:
            worker_rows.append(
                {
                    "run_id": run["run_id"],
                    "definition": run["definition"],
                    "arm": run["arm"],
                    "repetition": run["repetition"],
                    "task_id": worker["task_id"],
                    "requests": worker["requests"],
                    "prompt_tokens": worker["prompt_tokens"],
                    "completion_tokens": worker["completion_tokens"],
                    "first_request_prompt_tokens": worker["first_request_prompt_tokens"],
                    "tool_calls": worker["tool_calls"],
                    "read_calls": worker["read_calls"],
                    "write_calls": worker["write_calls"],
                    "public_test_attempts": worker["public_test_attempts"],
                    "stop_code": worker["stop_code"],
                    "self_success": worker["self_success"],
                    "produced_output": worker["produced_output"],
                    "deliverable_states": ";".join(
                        f"{item['path']}={item['state']}" for item in worker["deliverables"]
                    ),
                }
            )
    with (target / "workers.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(worker_rows[0]) if worker_rows else ["run_id"]
        )
        writer.writeheader()
        writer.writerows(worker_rows)
    (target / "attribution.json").write_text(
        json.dumps([run["attribution"] for run in payload["runs"]], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (target / "summary.json").write_text(
        json.dumps(summarize_payload(payload), indent=2, sort_keys=True), encoding="utf-8"
    )
    return {
        "destination": str(target),
        "files": sorted(path.name for path in target.iterdir() if path.is_file()),
    }


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-r1-0")
    parser.add_argument("command", choices=("analyse", "publish"))
    parser.add_argument("--root", default=str(R05_ROOT))
    parser.add_argument("--out", default=str(OUT_ROOT))
    args = parser.parse_args(argv)

    if args.command == "publish":
        payload = collect(Path(args.root))
        print(json.dumps(publish_r05_evidence(Path(args.root)), indent=2))
        print(json.dumps(publish_r10_evidence(payload), indent=2))
        return 0

    payload = collect(Path(args.root))
    write_outputs(payload, Path(args.out))
    summary = summarize_payload(payload)
    (Path(args.out) / "aggregate.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    (Path(args.out) / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    print(render_markdown(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
