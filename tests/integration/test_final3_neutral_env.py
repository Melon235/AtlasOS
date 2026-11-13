"""Final-3 R-10: the native baseline's tool capability must map 1:1 onto Atlas's sandbox tools.

This test proves the *capability* half of environment parity and asserts that the *environment*
half is still blocked. It must not be relaxed to make a cost comparison look fair.
"""

from __future__ import annotations

from experiments.evaluation.baseline import BASELINE_TOOLS
from experiments.evaluation.neutral_env import (
    ATLAS_SANDBOX_TOOLS,
    CAPABILITY_MAP,
    NATIVE_TOOLS,
    capability_report,
)

from atlasos.sandbox.tools import sandbox_tool_names


def test_native_tool_list_is_the_one_the_baseline_compiles_with() -> None:
    assert tuple(BASELINE_TOOLS) == NATIVE_TOOLS


def test_every_native_tool_has_an_atlas_counterpart() -> None:
    assert set(NATIVE_TOOLS) == set(ATLAS_SANDBOX_TOOLS)
    assert set(ATLAS_SANDBOX_TOOLS) <= set(sandbox_tool_names())


def test_capability_map_covers_every_tool_and_states_its_limitation() -> None:
    mapped = {row[1] for row in CAPABILITY_MAP}

    assert mapped == set(NATIVE_TOOLS)
    for operation, native, atlas, equivalence, limitation in CAPABILITY_MAP:
        assert operation and native and atlas
        assert equivalence in {"equivalent", "equivalent-with-limitation"}
        assert limitation


def test_environment_parity_is_reported_as_blocked_not_complete() -> None:
    report = capability_report()

    assert report.status == "BASELINE_PARITY_BLOCKED"
    assert report.missing
    assert "neutral sandbox" in " ".join(report.missing)
