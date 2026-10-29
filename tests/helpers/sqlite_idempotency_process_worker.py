"""Independent-process worker for AnyCode SQLite idempotency verification."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from anycode.tools.idempotency import SQLiteToolIdempotencyStore
from anycode.types import ToolResult


def _fingerprint(payload: str) -> str:
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _side_effect(counter_db: Path, tool: str, key: str, payload: str) -> int:
    connection = sqlite3.connect(counter_db, timeout=10.0)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS test_side_effects (tool TEXT NOT NULL, key TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO test_side_effects (tool, key) VALUES (?, ?)", (tool, key))
        connection.commit()
        row = connection.execute("SELECT COUNT(*) FROM test_side_effects").fetchone()
        return int(row[0])
    finally:
        connection.close()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    store = SQLiteToolIdempotencyStore(args.db)
    await store.setup()
    try:
        if args.mode == "barrier-claim":
            Path(args.ready).write_text("ready", encoding="utf-8")  # noqa: ASYNC240
            deadline = time.monotonic() + 10.0
            while not Path(args.go).exists():  # noqa: ASYNC240
                if time.monotonic() > deadline:
                    raise TimeoutError("barrier go signal was not received")
                time.sleep(0.01)  # noqa: ASYNC251

        outcome = await store.claim(args.tool, args.key, args.fingerprint)
        side_effect_count = 0
        if outcome.outcome == "execute" and args.mode in ("claim-complete", "barrier-claim"):
            side_effect_count = _side_effect(args.counter_db, args.tool, args.key, args.result)
            await store.complete(
                args.tool,
                args.key,
                ToolResult(data=args.result, retry_safe=True),
            )
        return {
            "mode": args.mode,
            "pid": os.getpid(),
            "outcome": outcome.outcome,
            "result": outcome.result.model_dump(mode="json") if outcome.result else None,
            "side_effect_count": side_effect_count,
        }
    finally:
        await store.teardown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--counter-db", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument(
        "--payload",
        default="payload-A",
        help="TEST fingerprint payload; hashed deterministically",
    )
    parser.add_argument("--fingerprint", default=None)
    parser.add_argument("--result", default="result:ok")
    parser.add_argument(
        "--mode",
        choices=("claim-complete", "claim-only", "claim", "barrier-claim"),
        required=True,
    )
    parser.add_argument("--ready", default="")
    parser.add_argument("--go", default="")
    args = parser.parse_args()
    if args.fingerprint is None:
        args.fingerprint = _fingerprint(args.payload)
    try:
        result = asyncio.run(_run(args))
    except BaseException as error:
        print(f"child failure: {error}", file=sys.stderr)
        raise
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
