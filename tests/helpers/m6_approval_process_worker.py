"""Independent-process worker for M6 SQLite approval durability verification."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from atlasos.approvals.models import (
    ApprovalChallenge,
    ApprovalReceipt,
    ReviewerIdentity,
    derive_approval_id,
)
from atlasos.approvals.store import SQLiteApprovalStore


def _challenge(args: argparse.Namespace) -> ApprovalChallenge:
    approval_id = args.approval_id or derive_approval_id(
        suspension_id=args.suspension_id,
        call_id=args.call_id,
        action_digest=args.action_digest,
        decision_basis_digest=args.decision_basis_digest,
    )
    return ApprovalChallenge(
        approval_id=approval_id,
        suspension_id=args.suspension_id,
        run_id=args.run_id,
        task_id=args.task_id,
        worker_id=args.worker_id,
        profile_id=args.profile_id,
        call_id=args.call_id,
        ordinal=args.ordinal,
        action_digest=args.action_digest,
        decision_basis_digest=args.decision_basis_digest,
        tool_name=args.tool_name,
        trusted_review_projection={"recipient": "ops@example.test"},
        created_at=datetime.now(UTC),
    )


def _barrier(args: argparse.Namespace) -> None:
    if not args.ready:
        return
    Path(args.ready).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10.0
    while not Path(args.go).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("barrier go signal not received")
        time.sleep(0.01)


def _run(args: argparse.Namespace) -> dict[str, object]:
    store = SQLiteApprovalStore(args.db)
    if args.mode == "create":
        result = store.create_if_absent(_challenge(args))
        return {
            "pid": os.getpid(),
            "mode": args.mode,
            "created": result.created,
            "approval_id": result.challenge.approval_id,
            "state": result.challenge.state,
            "version": result.challenge.version,
        }
    if args.mode in ("approve", "reject"):
        _barrier(args)
        challenge = store.get(args.approval_id)
        if challenge is None:
            return {"pid": os.getpid(), "mode": args.mode, "status": "not_found"}
        status = store.resolve(
            args.approval_id,
            expected_version=challenge.version,
            decision="APPROVED" if args.mode == "approve" else "REJECTED",
            reviewer=ReviewerIdentity(actor=args.actor),
            reason=args.reason,
        )
        return {
            "pid": os.getpid(),
            "mode": args.mode,
            "status": status.status,
            "state": status.challenge.state if status.challenge else None,
            "version": status.challenge.version if status.challenge else None,
        }
    if args.mode == "read":
        challenge = store.get(args.approval_id)
        return {
            "pid": os.getpid(),
            "mode": args.mode,
            "state": challenge.state if challenge else None,
            "version": challenge.version if challenge else None,
            "actor": challenge.actor if challenge else None,
        }
    if args.mode == "receipt":
        challenge = store.get(args.approval_id)
        receipt = ApprovalReceipt.from_challenge(challenge) if challenge else None
        return {
            "pid": os.getpid(),
            "mode": args.mode,
            "receipt": receipt.model_dump(mode="json") if receipt else None,
            "state": challenge.state if challenge else None,
            "version": challenge.version if challenge else None,
        }
    raise ValueError(f"unknown mode {args.mode!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--mode", required=True, choices=("create", "approve", "reject", "read", "receipt")
    )
    parser.add_argument("--approval-id", default="")
    parser.add_argument("--suspension-id", default="suspension-1")
    parser.add_argument("--run-id", default="run-1")
    parser.add_argument("--task-id", default="task-1")
    parser.add_argument("--worker-id", default="worker-1")
    parser.add_argument("--profile-id", default="research_worker")
    parser.add_argument("--call-id", default="call-1")
    parser.add_argument("--ordinal", type=int, default=0)
    parser.add_argument("--tool-name", default="email.send")
    parser.add_argument("--action-digest", default="a" * 64)
    parser.add_argument("--decision-basis-digest", default="b" * 64)
    parser.add_argument("--actor", default="alice")
    parser.add_argument("--reason", default="process test")
    parser.add_argument("--ready", default="")
    parser.add_argument("--go", default="")
    args = parser.parse_args()
    result = _run(args)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:  # pragma: no cover - child failure surface
        print(f"child failure: {error}", file=sys.stderr)
        raise
