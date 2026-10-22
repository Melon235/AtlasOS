"""Bounded ContextPilot reorder microbenchmark.

Each case runs in a subprocess with a hard timeout. No LLM call, no AtlasOS runtime.

    uv run python experiments/benchmarks/reorder_cpu.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_SECONDS = 10
WORKER = Path(__file__).with_name("_reorder_worker.py")


@dataclass(frozen=True)
class Case:
    name: str
    requests: int
    blocks_per_request: int
    target_tokens: int


CASES = (
    Case("c1", 2, 2, 500),
    Case("c2", 2, 3, 500),
    Case("c3", 3, 3, 500),
    Case("c4", 4, 4, 500),
    Case("c5", 2, 2, 1000),
    Case("c6", 3, 3, 1000),
)


def run_case(case: Case) -> dict[str, object]:
    payload = json.dumps(
        {
            "requests": case.requests,
            "blocks_per_request": case.blocks_per_request,
            "target_tokens": case.target_tokens,
        }
    )
    try:
        completed = subprocess.run(
            [sys.executable, str(WORKER), payload],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"case": case.name, "timed_out": True, "reorder_latency_ms": None}
    if completed.returncode != 0:
        return {
            "case": case.name,
            "timed_out": False,
            "error": completed.stderr.strip()[-300:],
            "reorder_latency_ms": None,
        }
    result = json.loads(completed.stdout)
    result.update({"case": case.name, "timed_out": False})
    return result


def main() -> None:
    rows = []
    for case in CASES:
        row = run_case(case)
        rows.append(row)
        if row.get("timed_out"):
            print(f"{case.name}: TIMEOUT >{TIMEOUT_SECONDS}s")
        elif row.get("error"):
            print(f"{case.name}: ERROR {row['error']}")
        else:
            print(
                f"{case.name}: requests={row['requests']} "
                f"blocks/request={row['blocks_per_request']} "
                f"chars={row['context_chars']} "
                f"reorder_ms={row['reorder_latency_ms']:.1f}"
            )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
