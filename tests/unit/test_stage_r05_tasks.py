"""Stage R0.5: benchmark-definition invariants for the positive/negative families.

These tests are the repository-level freeze. If any prompt, fixture, contract, oracle or expected
route changes, the digests below change and this file fails, which forces a new benchmark version
and a new round instead of a silent edit of an existing one.
"""

from __future__ import annotations

from experiments.evaluation.stage_r05_tasks import (
    BENCHMARK_VERSION,
    DEFINITIONS,
    EXPERIMENT_MATRIX,
    NEGATIVE_IDS,
    POSITIVE_IDS,
    benchmark_digest,
    definition_by_id,
    definition_payload,
    preregistration,
    variants,
)

from atlasos.workers.routing import extract_declared_paths

#: Frozen 2026-11-11 as ``r0.5.2``. ``r0.5.0`` was invalidated by an over-specified P1 oracle and
#: ``r0.5.1`` by a P2 contract that contradicted its own oracle; both defects are documented, and
#: only the affected family's digest moved.
BENCHMARK_DIGESTS = {
    "r05-p1-analysis": "aa1594aaf7b78b5d6ed619d04ca22c3c7c9c09a9a067c02f243f8b85260adbc2",
    "r05-p2-modules": "27433621c2f4997edef0f0d0ed89147f95ae5002d6db4a006f3d01c225748bec",
    "r05-p3-frozen-interface": "442e0c37001fffdc12288562cdecb96bed42eb600ee048ff9964eed2e7562bc4",
    "r05-n1-shared-write": "67ded57a873490d469ffb0cc3e3968dfb864b7d546f721b023d08b1311b44b23",
    "r05-n2-unfrozen-interface": "a182180e9af0c7f8f6886565ad715acb426429a35d0bb746cf92fcf0f1ee72d1",
    "r05-n3-linear-chain": "216bee1f8719e9ae6bd8355c24356ea6c2d0f7843800ac19f4f6c9a24cd5345a",
}

PREREGISTRATION_DIGEST = "57d8ff2b2d72d3e72ac24c5896ec6b3c9f3671a9369cba033bbd9b4b208fb8d4"
ORACLE_DEFINITION_DIGEST = "4d69d20c2f057a25cecad0a27dd686563ee026d4219997e6d406db50fa102356"


def test_benchmark_definitions_are_frozen() -> None:
    assert {item.sample_id for item in DEFINITIONS} == set(BENCHMARK_DIGESTS)
    actual = {item.sample_id: benchmark_digest(item) for item in DEFINITIONS}
    assert actual == BENCHMARK_DIGESTS


def test_preregistration_is_frozen() -> None:
    payload = preregistration()
    assert BENCHMARK_VERSION == "r0.5.2"
    assert payload["benchmark_version"] == BENCHMARK_VERSION
    assert payload["preregistration_digest"] == PREREGISTRATION_DIGEST
    assert payload["oracle_definition_digest"] == ORACLE_DEFINITION_DIGEST
    assert payload["routing_policy_version"] == "r0.1.0"
    assert payload["width_policy_version"] == "r0.2.0"
    assert payload["positive_task_definitions"]["ids"] == list(POSITIVE_IDS)
    assert payload["negative_task_definitions"]["ids"] == list(NEGATIVE_IDS)


def test_experiment_matrix_matches_the_frozen_plan() -> None:
    assert EXPERIMENT_MATRIX["track_a"]["runs"] == 18
    assert EXPERIMENT_MATRIX["track_b"]["runs"] == 12
    assert len(POSITIVE_IDS) == 3
    assert len(NEGATIVE_IDS) == 3
    assert EXPERIMENT_MATRIX["track_b"]["tasks"] == ["e1-01-config", "e1-03-rbac"]
    assert EXPERIMENT_MATRIX["fixed_conditions"]["max_turns"] == 14


def test_positive_families_have_no_dependency_edges_and_disjoint_artifacts() -> None:
    for sample_id in ("r05-p1-analysis", "r05-p2-modules", "r05-p3-frozen-interface"):
        definition = definition_by_id(sample_id)
        assert [worker.depends_on for worker in definition.workers] == [(), ()]
        written = [set(extract_declared_paths(worker.prompt)[0]) for worker in definition.workers]
        assert written[0] and written[1]
        assert written[0].isdisjoint(written[1])
        assert set(definition.required_artifacts) <= set(definition.allowed_paths)
        union: set[str] = set()
        for paths in written:
            assert paths <= set(definition.required_artifacts)
            union |= paths
        assert union == set(definition.required_artifacts)


def test_each_worker_prompt_names_only_its_own_artifact() -> None:
    for definition in DEFINITIONS:
        if not definition.family.startswith("P"):
            continue
        ids = {worker.task_id for worker in definition.workers}
        for worker in definition.workers:
            own = set(extract_declared_paths(worker.prompt)[0])
            assert own, worker.task_id
            for sibling in ids - {worker.task_id}:
                assert sibling not in worker.prompt.split(), (
                    f"{definition.sample_id}:{worker.task_id} names sibling {sibling}"
                )


def test_negative_controls_encode_the_failure_they_must_expose() -> None:
    shared = definition_by_id("r05-n1-shared-write")
    writes = [set(extract_declared_paths(worker.prompt)[0]) for worker in shared.workers]
    assert writes[0] & writes[1] == {"state.json"}

    unfrozen = definition_by_id("r05-n2-unfrozen-interface")
    declared = unfrozen.frozen_contracts or {}
    assert declared, "the failing family must declare the contract it does not have"
    for task_id, paths in declared.items():
        for path in paths:
            assert path not in unfrozen.visible_files, (
                f"{unfrozen.sample_id}:{task_id} contract {path} must be absent"
            )

    chain = definition_by_id("r05-n3-linear-chain")
    assert [worker.depends_on for worker in chain.workers] == [(), ("first",)]


def test_frozen_interface_family_ships_every_contract_it_declares() -> None:
    definition = definition_by_id("r05-p3-frozen-interface")
    declared = definition.frozen_contracts or {}
    assert set(declared) == {worker.task_id for worker in definition.workers}
    for paths in declared.values():
        for path in paths:
            assert path in definition.visible_files


def test_oracle_files_are_never_visible_to_a_worker() -> None:
    for definition in DEFINITIONS:
        visible = set(definition.visible_files)
        hidden = set(definition.hidden_files)
        assert hidden
        assert visible.isdisjoint(hidden)
        for path in hidden:
            assert not any(path == item or item.endswith(path) for item in visible)
        for worker in definition.workers:
            for path in hidden:
                assert path not in worker.prompt
                assert "test_hidden" not in worker.prompt
            assert "hidden" not in worker.prompt.lower()


def test_variants_share_fixture_and_oracle() -> None:
    for definition in DEFINITIONS:
        a0, a1 = variants(definition)
        assert a0.visible_files == a1.visible_files
        assert a0.hidden_files == a1.hidden_files
        assert a0.required_artifacts == a1.required_artifacts
        assert a0.allowed_paths == a1.allowed_paths
        assert a0.frozen_contracts == a1.frozen_contracts
        assert len(a1.workers) == 2
        assert a1.tool_expectations["distinct_workers"] == 2
        assert a1.mode == "multi_worker"
        assert a0.mode == "coding"


def test_definition_payload_is_json_serialisable() -> None:
    import json

    for definition in DEFINITIONS:
        payload = definition_payload(definition)
        json.dumps(payload, sort_keys=True)
        assert payload["expected_route"] in {"direct", "delegate_parallel"}
