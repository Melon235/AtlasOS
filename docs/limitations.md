# Known Limitations

Real constraints of the current candidate. Nothing here is a backlog promise.

## Runtime scope

1. **Single host.** Ledger, approvals, suspensions, idempotency and sandbox bindings are local
   SQLite files. There is no cross-host recovery, no distributed lock, no shared queue.
2. **Single process per run directory.** `RunBudgetState` uses an in-process lock. Concurrent
   processes writing the same run directory are unsupported.
3. **AnyCode `_next_seq` is per process.** Upstream's filesystem run store caches the transcript
   sequence in memory, so multi-process appends to one run could collide. AtlasRuntime runs one
   writer per run and does not add a lock.
4. **Recovery requires the operator harness.** `atlas resume` refuses to reattach a remote sandbox
   without M5/M6 authority. The trusted recovery composition exists and is tested, but it is not a
   one-command CLI operation.
5. **Approve/reject requires a configured suspension store.** Without M6 wiring, an approval
   command has nothing to resolve.

## Sandbox

6. **CPU and memory quotas are not pinned** in the sandbox or in the oracle sandbox. Isolation is
   network and filesystem level, not resource-scheduling level.
7. **One container per grading** in the isolated oracle (~3.1 s of a ~6 s grading budget).
8. **Only `opensandbox` and `daytona` are wired**; the other AnyCode backends are reachable only
   through upstream code paths and are not exercised here.
9. **Local ownership is an OS file lock**, not a lease protocol. It detects a concurrent local
   owner, not a stale remote one.

## Authorization

10. **Shell classification is text-level.** The ownership gate cannot attribute an arbitrary shell
    command to a declared path; it denies what it cannot classify, which is why enforcement is off
    by default.
11. **No OS-level per-worker isolation.** Worker identity is carried in `ExecutionContext`
    attributes and enforced by the gate, not by the kernel.
12. **The action gate sees a tool call, not intent.** A `bash` call that writes outside the
    workspace is caught by path/shell policy at the tool boundary, not by semantic analysis.

## Evidence

13. **Sample sizes are small.** Most mechanism comparisons are n=4 to n=6 per arm; only Stage E1
    (36 paired runs) and the 30-task benchmark (90 runs) are larger.
14. **Single model family.** Every live run used `deepseek-chat` at temperature 0. One benchmark
    report resolved to `deepseek-flash` despite requesting `deepseek-chat`.
15. **Some mechanisms have deterministic evidence only.** Suspension resume, `history.read`
    recovery and the ownership rollback block were never exercised by a real model run — the
    recovery tool was called 0 times in 12 real runs.
16. **Provider-level concurrency overlap is inferred**, not observed: `provider_calls.jsonl` has no
    wall-clock or worker attribution.
17. **`result.json` is an incomplete record.** Stage R1-0 found 365 recorded actions against 591 in
    the trajectory (38 % missing, concentrated in failed workers). Analysis must use the
    trajectory.
18. **Cache behaviour is provider-controlled.** Prompt-cache hit rates depend on DeepSeek's
    best-effort cache; no cold-start control was available, so cache experiments are correlational.

## Verified-but-narrow

19. **The application coding mode requires a UAT-shaped workspace** containing `csv_parser.py`.
    It is a demonstration path, not a general repository driver.
20. **Deliverable verification covers a fixed check vocabulary.** Public-contract recall is weak
    (23 true positives / 23 false negatives on historical exports); a mid-month date family is not
    detected.
21. **CI runs the offline suite only.** Live DeepSeek tests are opt-in and never required; the
    sandbox-live integration test is environment-gated and skipped in CI.

## Repository

22. **The package, CLI and repository names are frozen** as `atlasos`, `atlas` and
    `Melon235/AtlasOS` for compatibility. The public project name is AtlasRuntime; the rename is a
    separate migration.
23. **Historical stage branches are kept as-is.** They contain their own raw run artifacts; the
    final candidate branch does not.
