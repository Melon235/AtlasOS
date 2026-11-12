"""Write the B0 upstream/environment evidence record.

The record is what the report cites: pinned commit and image, per-task digests, per-task contract
summary, and the observed runtime blockers. It contains no private task content and no secret.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from experiments.business_benchmarks.commerce_agent_bench.upstream import (
    TASK_PATHS,
    load_task,
    manifest_payload,
    task_digests,
    text_only_collection,
    upstream_root,
    verify_commit,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_ROOT = REPO_ROOT / "experiments" / "evidence" / "stage-b0"


def build_audit(root: Path | None = None) -> dict[str, Any]:
    base = root or upstream_root()
    commit = verify_commit(base)
    collection = set(text_only_collection(base))
    tasks: dict[str, Any] = {}
    for task_id in TASK_PATHS:
        task = load_task(task_id, root=base)
        environment = task.environment
        tasks[task_id] = {
            "upstream_dir": TASK_PATHS[task_id],
            "in_text_only_collection": task_id in collection,
            "language": task.definition["task"].get("language"),
            "difficulty": task.definition["task"].get("difficulty"),
            "agent_timeout_sec": task.agent_timeout_seconds,
            "max_actions": task.max_actions,
            "verifier": task.definition.get("verifier"),
            "requires_network": environment.get("requires_network"),
            "requires_browser": environment.get("requires_browser"),
            "requires_vision": environment.get("requires_vision"),
            "mock_services": environment.get("mock_services"),
            "host_published_port": task.host_published_port,
            "runtime_mocks": sorted(task.runtime_mocks),
            "task_local_service_script": (task.root / "services" / "start_services.sh").is_file(),
            "rubric_checks": len(task.rubric.get("checks") or []),
            "digests": task_digests(task).as_dict(),
            "prompt_chars": len(task.prompt),
            "workspace_files": sum(1 for path in task.workspace.rglob("*") if path.is_file()),
        }
    return {
        "stage": "b0",
        "generated_at": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "upstream": {**manifest_payload(), "verified_commit": commit},
        "collection_size": len(collection),
        "tasks": tasks,
    }


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Write the B0 upstream audit evidence")
    parser.add_argument("--upstream", default=None)
    args = parser.parse_args(argv)
    payload = build_audit(Path(args.upstream) if args.upstream else None)
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_ROOT / "upstream_audit.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"audit written to {path}")
    return payload


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
