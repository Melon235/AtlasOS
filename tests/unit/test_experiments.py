from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from atlasos.config import AtlasConfig
from atlasos.experiments import ExperimentStore, configuration_hash, git_commit


class _Row(BaseModel):
    run_id: str


def test_configuration_hash_is_stable_and_sensitive() -> None:
    baseline = AtlasConfig()
    same = AtlasConfig()
    changed = AtlasConfig.model_validate({"provider": {"model": "deepseek-reasoner"}})

    digest = configuration_hash(baseline)

    assert digest == configuration_hash(same)
    assert digest != configuration_hash(changed)
    assert len(digest) == 64
    int(digest, 16)


def test_store_appends_one_json_object_per_line(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "runs.jsonl")

    store.append(_Row(run_id="r1"))
    store.append(_Row(run_id="r2"))

    rows = store.read_all()
    assert [row["run_id"] for row in rows] == ["r1", "r2"]
    raw = (tmp_path / "runs.jsonl").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert len(raw.splitlines()) == 2


def test_store_creates_parent_directories(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "nested" / "dir" / "runs.jsonl")

    store.append(_Row(run_id="r1"))

    assert store.read_all() == [{"run_id": "r1"}]


def test_git_commit_is_none_outside_a_repository(tmp_path: Path) -> None:
    assert git_commit(tmp_path) is None
