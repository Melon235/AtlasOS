"""Stage G3-4a: read-only provenance audit for evaluation artifact visibility.

Walks recorded run artifacts (never a live sandbox) and answers, per suspicious path:

* which worker command touched it,
* what the command saw (marker counts only, no verbatim hidden content),
* whether the touched content is a copy of the worker's own output, public fixture data, or a
  grading artifact.

Usage::

    python -m experiments.evaluation.stage_g3_4a_integrity audit \
        --runs experiments/results/stage-g3-3/g33-20261111T051039Z \
        --output experiments/results/stage-g3-4a/audit

It writes ``provenance.csv`` and ``provenance.json`` and prints a summary. It never writes into a
run directory and never prints full tool output.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.sandbox import SandboxCommand

REPO_ROOT = Path(__file__).resolve().parents[2]

#: AtlasOS M5 uploads the fixture bundle here to clone the workspace inside the sandbox. It is a
#: staging artifact, never part of the task, and is removed once the clone is verified.
STAGING_BUNDLE = "/tmp/project.bundle"

#: Cheap per-run sweep: names that must never exist inside a worker sandbox.
PRIVATE_NAME_PATTERNS = ("test_hidden*", "*.oracle", "oracle.log", "*.hidden_test*")

CANARY_PREFIX = "ATLAS-ORACLE-CANARY"

#: Neutral fixture commit message. The worker can read ``git log``, so the message must not name
#: the benchmark sample or announce an evaluation fixture.
FIXTURE_COMMIT_LABEL = "initial project import"


@dataclass
class IsolationReport:
    """Result of the per-run evaluation isolation step."""

    staging_bundle_present_before: bool = False
    staging_bundle_removed: bool = False
    private_name_hits: list[str] = field(default_factory=list)
    probe_commands: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_preparation_mode": "public_fixture_clone",
            "staging_bundle": STAGING_BUNDLE,
            "staging_bundle_present_before": self.staging_bundle_present_before,
            "staging_bundle_removed": self.staging_bundle_removed,
            "private_name_hits": list(self.private_name_hits),
            "probe_commands": list(self.probe_commands),
            "isolation_checks_passed": self.passed,
            "failures": list(self.failures),
        }


async def enforce_evaluation_isolation(
    provider: Any,
    handle: Any,
    *,
    timeout_seconds: int = 60,
) -> IsolationReport:
    """Remove the staging bundle and prove no private artifact name is reachable.

    Evaluation-only cleanup: it runs after provisioning, before the first model turn, and only
    touches harness staging artifacts. It never changes sandbox permissions or worker commands.
    """

    report = IsolationReport()

    check = await provider.execute(
        handle,
        SandboxCommand(
            command=f"test -e {STAGING_BUNDLE} && echo present || echo absent",
            cwd="/",
            timeout_seconds=timeout_seconds,
        ),
    )
    report.probe_commands.append("test -e /tmp/project.bundle")
    if not check.ok:
        report.failures.append(f"staging probe failed: {check.error}")
        return report
    report.staging_bundle_present_before = check.stdout.strip().endswith("present")

    if report.staging_bundle_present_before:
        removal = await provider.execute(
            handle,
            SandboxCommand(
                command=f"rm -f {STAGING_BUNDLE}",
                cwd="/",
                timeout_seconds=timeout_seconds,
            ),
        )
        report.probe_commands.append("rm -f /tmp/project.bundle")
        if not removal.ok or removal.exit_code != 0:
            report.failures.append(f"staging bundle removal failed: {removal.error}")
            return report
        verify = await provider.execute(
            handle,
            SandboxCommand(
                command=f"test -e {STAGING_BUNDLE} && echo present || echo absent",
                cwd="/",
                timeout_seconds=timeout_seconds,
            ),
        )
        report.probe_commands.append("test -e /tmp/project.bundle (post-removal)")
        if not verify.ok or verify.stdout.strip().endswith("present"):
            report.failures.append("staging bundle still present after removal")
            return report
        report.staging_bundle_removed = True

    names = " -o ".join(f"-name '{pattern}'" for pattern in PRIVATE_NAME_PATTERNS)
    sweep = await provider.execute(
        handle,
        SandboxCommand(
            command=(
                "find / -xdev -maxdepth 4 \\( " + names + " \\) "
                "-not -path '/proc/*' -not -path '/sys/*' 2>/dev/null | head -20"
            ),
            cwd="/",
            timeout_seconds=timeout_seconds,
        ),
    )
    report.probe_commands.append("find / -maxdepth 4 (private name patterns)")
    if not sweep.ok:
        report.failures.append(f"private name sweep failed: {sweep.error}")
        return report
    report.private_name_hits = [line for line in sweep.stdout.splitlines() if line.strip()]
    if report.private_name_hits:
        report.failures.append(f"private artifact names reachable: {report.private_name_hits}")
    return report


def canary_for(seed: str) -> str:
    """Deterministic, non-secret marker used to prove a private file never reaches a sandbox."""

    import hashlib

    return f"{CANARY_PREFIX}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


#: Paths that must not be an information source for a worker.
SUSPICIOUS = {
    "sandbox_tmp_stdout": re.compile(r"/tmp/[\w.-]*\.stdout|^[\w]{8,}\.stdout"),
    "sandbox_tmp_stderr": re.compile(r"/tmp/[\w.-]*\.stderr|^[\w]{8,}\.stderr"),
    "project_bundle": re.compile(r"project\.bundle"),
    "hidden_tests": re.compile(r"test_hidden|tests/test_hidden|hidden"),
    "oracle_workspace": re.compile(r"oracle-workspace|oracle\.log"),
    "harness_results": re.compile(r"experiments/results|stage-e1|stage-g3"),
    "git_history": re.compile(r"git (log|reflog|bundle|cat-file|rev-list|show)"),
    "grading_scan": re.compile(r"find / |find /tmp|\.stdout|\.stderr"),
}

#: Markers that suggest grading feedback rather than the worker's own output.
GRADING_MARKERS = (
    "test_hidden",
    "FAILED",
    "assert",
    "=== FAILURES ===",
    "pytest",
    "PASSED",
    "short test summary",
)

PUBLIC_MARKERS = (
    "BEGIN-MARKER",  # unused sentinel kept for symmetry with other audits
    "OpenSandbox",
)

MAX_PREVIEW = 240


def load_conversations(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    path = run_dir / "tool_conversations.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tasks = payload.get("tasks") or {}
    return {
        str(name): list(entry.get("messages") or [])
        for name, entry in tasks.items()
        if isinstance(entry, dict)
    }


def tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names: dict[str, str] = {}
    inputs: dict[str, dict[str, Any]] = {}
    for message in messages:
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                names[str(block.get("id"))] = str(block.get("name"))
                inputs[str(block.get("id"))] = dict(block.get("input") or {})
    calls: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        for block in message.get("content") or []:
            if block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id"))
            content = block.get("content")
            text = content if isinstance(content, str) else json.dumps(content, default=str)
            calls.append(
                {
                    "tool": names.get(call_id, "?"),
                    "input": inputs.get(call_id, {}),
                    "is_error": bool(block.get("is_error")),
                    "text": text,
                }
            )
    return calls


def classify_text(text: str) -> str:
    """Content category of a tool result, from markers only."""

    markers = {marker: text.count(marker) for marker in GRADING_MARKERS}
    if "test_hidden" in text:
        return "ORACLE_PRIVATE"
    if markers["FAILED"] or markers["=== FAILURES ==="] or markers["short test summary"]:
        return "GRADING_FEEDBACK"
    if markers["pytest"] and markers["assert"]:
        return "TEST_OUTPUT"
    if "OpenSandbox" in text or "/opt" in text:
        return "PLATFORM_INTERNAL"
    return "WORKER_OWN_OUTPUT"


def audit_run(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task, messages in load_conversations(run_dir).items():
        turn = 0
        for message in messages:
            if message.get("role") == "assistant":
                turn += 1
                continue
        for call in tool_calls(messages):
            command = " ".join(str(call["input"].get("command") or call["input"]).split())
            hits = [name for name, pattern in SUSPICIOUS.items() if pattern.search(command)]
            if not hits:
                continue
            text = call["text"]
            rows.append(
                {
                    "run_id": run_dir.name,
                    "task": task,
                    "tool": call["tool"],
                    "categories": ";".join(sorted(hits)),
                    "command": command[:MAX_PREVIEW],
                    "result_chars": len(text),
                    "result_category": classify_text(text),
                    "grading_markers": sum(text.count(marker) for marker in GRADING_MARKERS),
                    "result_preview": " ".join(text.split())[:MAX_PREVIEW],
                }
            )
    return rows


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


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "touches": len(rows),
        "by_category": dict(Counter(row["result_category"] for row in rows)),
        "by_suspicious_path": dict(
            Counter(category for row in rows for category in str(row["categories"]).split(";"))
        ),
        "runs": len({row["run_id"] for row in rows}),
        "tasks": len({(row["run_id"], row["task"]) for row in rows}),
        "grading_marker_hits": sum(int(row["grading_markers"]) for row in rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-g3-4a-integrity")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--runs", nargs="+", required=True, type=Path)
    audit.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    rows: list[dict[str, Any]] = []
    for root in args.runs:
        for run_dir in sorted({path.parent for path in root.glob("runs/**/result.json")}):
            rows.extend(audit_run(run_dir))

    write_csv(args.output / "provenance.csv", rows)
    summary = summarise(rows)
    (args.output / "provenance.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
