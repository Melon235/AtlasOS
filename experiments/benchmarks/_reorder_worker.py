"""Subprocess worker for the bounded reorder benchmark (isolated + killable)."""

from __future__ import annotations

import io
import json
import sys
import time
from contextlib import redirect_stdout

from contextpilot import ContextPilot


def build_documents(requests: int, blocks_per_request: int, target_tokens: int) -> list[list[str]]:
    chars_per_token = 4
    target_chars = target_tokens * chars_per_token
    contexts: list[list[str]] = []
    for request_index in range(requests):
        context = []
        for block_index in range(blocks_per_request):
            doc_id = (request_index + block_index) % max(requests, 2)
            header = f"SHARED DOC {doc_id} policy: cite sources and stay concise."
            body_line = (
                f"Document {doc_id} paragraph for request {request_index}; "
                "stable prefix guidance is repeated to make reuse meaningful."
            )
            lines = []
            length = len(header)
            while length < target_chars:
                lines.append(body_line)
                length += len(body_line) + 1
            context.append(header + "\n" + "\n".join(lines))
        contexts.append(context)
    return contexts


def main() -> None:
    payload = json.loads(sys.argv[1])
    contexts = build_documents(
        payload["requests"], payload["blocks_per_request"], payload["target_tokens"]
    )
    started = time.perf_counter()
    with redirect_stdout(io.StringIO()):
        pilot = ContextPilot(use_gpu=False)
        pilot.reorder(contexts)
    latency_ms = (time.perf_counter() - started) * 1000.0
    print(
        json.dumps(
            {
                "requests": payload["requests"],
                "blocks_per_request": payload["blocks_per_request"],
                "target_tokens": payload["target_tokens"],
                "context_chars": sum(len(block) for context in contexts for block in context),
                "reorder_latency_ms": latency_ms,
            }
        )
    )


if __name__ == "__main__":
    main()
