"""R1-2A: observation-only integration with the evaluation runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from experiments.evaluation.runner import RunnerSettings, _observe_verification
from pytest import MonkeyPatch


class _Task:
    def __init__(self, sample_id: str) -> None:
        self.sample_id = sample_id


def test_off_mode_adds_no_verification_artifacts(tmp_path: Path) -> None:
    settings = RunnerSettings()
    assert settings.verification_mode == "off"
    assert settings.digest() != RunnerSettings(verification_mode="observe").digest()


def test_observe_mode_writes_findings_without_touching_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "reports").mkdir(parents=True)
    (workspace / "reports" / "pricing.md").write_text("# Pricing\n", encoding="utf-8")
    before = {item.name: item.read_bytes() for item in workspace.rglob("*") if item.is_file()}
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = asyncio.run(
        _observe_verification(
            task=_Task("r05-p1-dependency-audit"),
            workspace=workspace,
            baseline_hashes={},
            run_dir=run_dir,
        )
    )
    assert payload is not None
    assert (run_dir / "verification.json").is_file()
    summary = json.loads((run_dir / "verification_summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "observe"
    assert summary["deliverable_presence"] in {"PASS", "FAIL", "UNKNOWN"}
    after = {item.name: item.read_bytes() for item in workspace.rglob("*") if item.is_file()}
    assert before == after


def test_observe_mode_verifies_a_budget_stopped_delivery(tmp_path: Path) -> None:
    """A budget stop must not erase a deliverable that is present and well formed."""

    workspace = tmp_path / "workspace"
    (workspace / "reports").mkdir(parents=True)
    (workspace / "reports" / "pricing.md").write_text(
        'tier_rate: KeyError | example: tier_rate("pro") -> 0.8\n'
        'final_price: KeyError | example: final_price(250, "pro") -> 200\n'
        "bulk_discount: ValueError | example: bulk_discount(120) -> 0.15\n"
        "cheapest: ValueError | example: cheapest([7, 3, 9]) -> 3\n\n"
        '## Constants\nTIERS = {}\nDEFAULT_TIER = "basic"\nEXPEDITE_SURCHARGE = 0.05\n',
        encoding="utf-8",
    )
    (workspace / "reports" / "inventory.md").write_text(
        'available: none | example: available("nut") -> False\n'
        'reserve: ValueError | example: reserve("bolt", 5) -> 7\n'
        'low_stock: none | example: low_stock(4) -> ["gear"]\n'
        "total_units: none | example: total_units() -> 22\n\n"
        "## Constants\nSTOCK = {}\nLOW_STOCK_THRESHOLD = 5\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = asyncio.run(
        _observe_verification(
            task=_Task("r05-p1-dependency-audit"),
            workspace=workspace,
            baseline_hashes={},
            run_dir=run_dir,
        )
    )
    assert payload is not None
    assert payload["deliverable_presence"] == "PASS"
    assert payload["verification_overall"] in {"PASS", "UNKNOWN"}


def test_unknown_sample_is_skipped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = asyncio.run(
        _observe_verification(
            task=_Task("no-such-sample"),
            workspace=workspace,
            baseline_hashes={},
            run_dir=run_dir,
        )
    )
    assert payload is None
    assert not (run_dir / "verification.json").exists()


def test_missing_workspace_is_skipped(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = asyncio.run(
        _observe_verification(
            task=_Task("r05-p2-modules"),
            workspace=None,
            baseline_hashes={},
            run_dir=run_dir,
        )
    )
    assert payload is None


def test_verifier_crash_is_recorded_as_unknown(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    class _Boom:
        def __init__(self, *_: object, **__: object) -> None:
            raise RuntimeError("verifier exploded")

    monkeypatch.setattr(
        "atlasos.verification.engine.VerificationEngine",
        _Boom,
    )
    payload = asyncio.run(
        _observe_verification(
            task=_Task("r05-p2-modules"),
            workspace=workspace,
            baseline_hashes={},
            run_dir=run_dir,
        )
    )
    assert payload is not None
    assert payload["verification_overall"] == "UNKNOWN"
    assert "verifier exploded" in str(payload["error"])
