from __future__ import annotations

import pytest
from anycode.types import LLMMessage, TextBlock

from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash


def _messages(text: str) -> list[LLMMessage]:
    return [LLMMessage(role="user", content=[TextBlock(text=text)])]


def test_stable_prefix_hash_is_deterministic() -> None:
    first = stable_prefix_hash("You are terse.")
    second = stable_prefix_hash("You are terse.")

    assert first == second
    assert len(first) == 64
    assert stable_prefix_hash("You are verbose.") != first


def test_full_prompt_hash_tracks_the_dynamic_tail() -> None:
    base = full_prompt_hash("sys", _messages("hello"))
    repeated = full_prompt_hash("sys", _messages("hello"))
    changed = full_prompt_hash("sys", _messages("goodbye"))

    assert base == repeated
    assert base != changed


def test_dynamic_tail_does_not_change_stable_prefix_hash() -> None:
    prefix = stable_prefix_hash("sys")

    full_prompt_hash("sys", _messages("hello"))
    full_prompt_hash("sys", _messages("goodbye"))

    assert stable_prefix_hash("sys") == prefix


def test_tool_definitions_participate_in_the_prefix_hash() -> None:
    from anycode.types import LLMToolDef

    tool = LLMToolDef(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    assert stable_prefix_hash("sys") != stable_prefix_hash("sys", [tool])
    assert stable_prefix_hash("sys", [tool]) == stable_prefix_hash("sys", [tool])


def test_unicode_and_ordering_are_stable() -> None:
    assert stable_prefix_hash("系统提示") == stable_prefix_hash("系统提示")
    assert full_prompt_hash("s", _messages("第一")) == full_prompt_hash("s", _messages("第一"))


def test_hashes_are_hex_digests() -> None:
    value = full_prompt_hash(None, _messages("x"))
    int(value, 16)  # raises when not hexadecimal
    assert len(value) == 64


@pytest.mark.parametrize("prompt", [None, "", "sys"])
def test_none_and_empty_system_prompt_are_normalized(prompt: str | None) -> None:
    assert stable_prefix_hash(prompt) == stable_prefix_hash(prompt or "")
