"""Stage R1-1: manifest construction and path normalization (T8)."""

from __future__ import annotations

import pytest

from atlasos.ownership import (
    OWNERSHIP_PATH_ESCAPE,
    OwnedPathDeclaration,
    OwnershipManifestError,
    OwnershipPathError,
    build_manifest,
    normalize_declared_path,
    normalize_workspace_path,
)

ROOT = "/workspace/project"


def _declaration(
    worker: str,
    *paths: str,
    task: str | None = None,
    excluded: tuple[str, ...] = (),
    provenance: str = "verified_task_metadata",
) -> OwnedPathDeclaration:
    return OwnedPathDeclaration(
        worker_id=worker,
        task_id=task or f"task-{worker}",
        paths=tuple(paths),
        excluded=excluded,
        provenance=provenance,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ledger/money.py", "ledger/money.py"),
        ("./ledger/money.py", "ledger/money.py"),
        ("ledger/../ledger/money.py", "ledger/money.py"),
        ("/workspace/project/ledger/money.py", "ledger/money.py"),
        ("ledger//money.py", "ledger/money.py"),
        ("consumers/report.py/", "consumers/report.py"),
    ],
)
def test_normalize_accepts_equivalent_spellings(raw: str, expected: str) -> None:
    assert normalize_workspace_path(raw, workspace_root=ROOT) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "../etc/passwd",
        "ledger/../../etc/passwd",
        "/etc/passwd",
        "/workspace/project-evil/file.py",
        "/workspace/project",
        "ledger\\money.py",
        "C:/temp/file.py",
        "ledger/money.py\x00",
        None,
        123,
    ],
)
def test_normalize_rejects_escapes_and_aliases(raw: object) -> None:
    with pytest.raises(OwnershipPathError) as error:
        normalize_workspace_path(raw, workspace_root=ROOT)
    assert error.value.reason_code == OWNERSHIP_PATH_ESCAPE


def test_normalize_declared_path_preserves_directory_semantics() -> None:
    assert normalize_declared_path("ledger/money.py", workspace_root=ROOT) == "ledger/money.py"
    assert normalize_declared_path("ledger/**", workspace_root=ROOT) == "ledger/"
    assert normalize_declared_path("ledger/", workspace_root=ROOT) == "ledger/"
    assert normalize_declared_path("ledger/*", workspace_root=ROOT) == "ledger/"


def test_manifest_digest_is_stable_and_detects_tampering() -> None:
    declaration = _declaration("worker-a", "ledger/entries.py")
    first = build_manifest(run_id="run", workspace_root=ROOT, declarations=(declaration,))
    second = build_manifest(run_id="run", workspace_root=ROOT, declarations=(declaration,))
    assert first.digest == second.digest
    assert first.is_valid()
    tampered = type(first)(
        run_id=first.run_id,
        workspace_root=first.workspace_root,
        policy_version=first.policy_version,
        worker_ownership={"worker-a": ("ledger/entries.py",), "worker-b": ("ledger/money.py",)},
        task_by_worker=dict(first.task_by_worker),
        excluded_by_worker=dict(first.excluded_by_worker),
        global_paths=first.global_paths,
        provenance=dict(first.provenance),
        digest=first.digest,
    )
    assert not tampered.is_valid()


def test_manifest_records_task_binding_and_ownership() -> None:
    manifest = build_manifest(
        run_id="run",
        workspace_root=ROOT,
        declarations=(
            _declaration("worker-a", "ledger/entries.py", task="task-a"),
            _declaration("worker-b", "ledger/money.py", task="task-b"),
        ),
    )
    assert manifest.owner_of("ledger/entries.py") == "worker-a"
    assert manifest.owner_of("ledger/money.py") == "worker-b"
    assert manifest.owner_of("ledger/unknown.py") is None
    assert manifest.task_of("worker-b") == "task-b"


def test_directory_declaration_covers_descendants() -> None:
    manifest = build_manifest(
        run_id="run",
        workspace_root=ROOT,
        declarations=(_declaration("worker-a", "ledger/**"),),
    )
    assert manifest.owner_of("ledger/money.py") == "worker-a"
    assert manifest.owner_of("ledger/nested/deep.py") == "worker-a"


def test_conflicting_declarations_are_rejected() -> None:
    with pytest.raises(OwnershipManifestError):
        build_manifest(
            run_id="run",
            workspace_root=ROOT,
            declarations=(
                _declaration("worker-a", "ledger/money.py"),
                _declaration("worker-b", "ledger/money.py"),
            ),
        )


def test_planner_claim_cannot_grant_write_ownership() -> None:
    with pytest.raises(OwnershipManifestError):
        build_manifest(
            run_id="run",
            workspace_root=ROOT,
            declarations=(_declaration("worker-a", "ledger/money.py", provenance="planner_claim"),),
        )


def test_manifest_rejects_escaping_declaration() -> None:
    with pytest.raises(OwnershipManifestError):
        build_manifest(
            run_id="run",
            workspace_root=ROOT,
            declarations=(_declaration("worker-a", "../outside.py"),),
        )
