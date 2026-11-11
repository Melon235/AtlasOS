"""Experimental batch request policies (Stage 3 closure).

ContextPilot 0.5.0 ``reorder()`` is a cross-request primitive: it returns a request execution
order plus reordered documents per request. AtlasOS V1 does not put batch reordering on the
online path; this module exists to keep the semantics explicit and to support the bounded CPU
experiment.

A policy may change request order only. It must not rewrite Atlas-rendered prompt contents.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from atlasos.context.models import ContextBlock


class BatchPolicyUnavailable(RuntimeError):
    """ContextPilot is not installed or could not be used."""


@dataclass(frozen=True)
class RenderedRequest:
    request_id: str
    blocks: tuple[ContextBlock, ...]
    messages: tuple[dict[str, str], ...]
    prompt_hash: str


@dataclass(frozen=True)
class BatchOrderResult:
    requests: tuple[RenderedRequest, ...]
    policy_name: str
    order: tuple[str, ...]
    reordered: bool
    latency_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


class BatchRequestPolicy(Protocol):
    name: str

    def order(self, requests: Sequence[RenderedRequest]) -> BatchOrderResult: ...


class IdentityBatchRequestPolicy:
    name = "identity"

    def order(self, requests: Sequence[RenderedRequest]) -> BatchOrderResult:
        started = time.perf_counter()
        ordered = tuple(requests)
        return BatchOrderResult(
            requests=ordered,
            policy_name=self.name,
            order=tuple(request.request_id for request in ordered),
            reordered=False,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            metadata={},
        )


class ContextPilotReorderPolicy:
    """Official batch reorder used as an experiment-only request ordering policy.

    Only the returned request execution order is applied. Per-document ordering returned by the
    vendor is recorded as diagnostic metadata and is not written back into Atlas prompts.
    """

    name = "contextpilot_reorder"

    def order(self, requests: Sequence[RenderedRequest]) -> BatchOrderResult:
        started = time.perf_counter()
        try:
            from contextpilot import ContextPilot  # pyright: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise BatchPolicyUnavailable(
                "contextpilot==0.5.0 is not installed; install the 'contextpilot' extra"
            ) from exc

        contexts = [[block.content for block in request.blocks] for request in requests]
        backend = ContextPilot(use_gpu=False)
        _, original_indices = backend.reorder(contexts)
        order = list(original_indices)
        if sorted(order) != list(range(len(requests))):
            raise RuntimeError(f"reorder returned an invalid request permutation: {order}")
        ordered = tuple(requests[index] for index in order)
        return BatchOrderResult(
            requests=ordered,
            policy_name=self.name,
            order=tuple(request.request_id for request in ordered),
            reordered=order != sorted(order),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            metadata={"request_indices": order},
        )
