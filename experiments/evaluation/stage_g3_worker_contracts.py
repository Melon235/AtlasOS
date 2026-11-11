"""Stage G3-3: structured Worker Task Contract, derived only from public task text.

What the model already receives (audited in ``docs/stage-g3-3-report.md`` §1):

* ``ScopedWorkerFactory.create`` renders a byte-stable system prompt whose
  ``ASSIGNED TASK — UNTRUSTED DATA`` block contains ``objective = assignment.task``;
* ``AgentConfig`` for the worker is built from that profile + assignment;
* ``runner.py`` builds one ``TaskSpec`` per worker step with
  ``description = step.prompt`` — the user turn of the tool loop.

This module only rewrites **the step description** (the task turn). The assignment, the profile, the
system prompt, the tool list, the DAG and the budget are untouched, so ``legacy`` reproduces the
previous bytes exactly and ``structured`` adds a bounded contract block.

Everything here is a pure function of public benchmark metadata: the step's own prompt, sibling
steps' task ids/titles, and ``depends_on``. ``StageEDefinition.hidden_files`` and the oracle are not
reachable from this module's signatures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from experiments.evaluation.models import WorkerStep

WORKER_CONTRACT_LEGACY = "legacy"
WORKER_CONTRACT_STRUCTURED = "structured"
WORKER_CONTRACT_MODES = (WORKER_CONTRACT_LEGACY, WORKER_CONTRACT_STRUCTURED)

#: The shared environment sentence of the Stage E1 module tasks. It is re-expressed by SCOPE and
#: OWNERSHIP, so the structured arm does not repeat it verbatim twice inside one message.
ENV_TAIL = (
    "Work in /workspace/project. Do not modify tests/ or files outside the allowed module paths."
)
UNSPECIFIED = "not specified in the task text"

_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|json|md|txt|toml|yaml|yml|cfg|ini))(?![\w/])"
)
_WRITE_VERB_RE = re.compile(r"\b(write|create|implement|modify|edit|add|update|produce)\b", re.I)
_READ_VERB_RE = re.compile(r"\b(read|inspect|open|look at|check|examine|review|reuse)\b", re.I)
_NEGATION_RE = re.compile(r"\b(do not|don't|never)\b", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+")

MAX_REFERENCED_SIBLINGS = 4


@dataclass(frozen=True)
class WorkerContract:
    """Structured view of one worker step, rendered deterministically."""

    sample_id: str
    step_id: str
    goal: str
    owned_files: tuple[str, ...]
    excluded_files: tuple[str, ...]
    upstream: tuple[str, ...]
    referenced_siblings: tuple[str, ...]

    def render(self) -> str:
        return "\n".join(self._lines())

    # -- sections ----------------------------------------------------------

    def _lines(self) -> list[str]:
        lines: list[str] = [
            "[WORKER TASK CONTRACT]",
            f"SAMPLE: {self.sample_id} | STEP: {self.step_id}",
            "",
            "TASK GOAL",
            self.goal,
            "Do not broaden this into unrelated repository work.",
            "",
            "WORKSPACE SCOPE",
            "Work in /workspace/project. Read any project source file you need.",
            "Do not modify tests/ or files outside your owned paths.",
            "Do not search evaluation harness artifacts, hidden tests, scoring outputs or Git "
            "history for answers; they are not part of this task.",
            "",
            "FILE OWNERSHIP",
            f"- Owned (create or modify): {_join(self.owned_files)}",
        ]
        if self.excluded_files:
            lines.append(
                "- Explicitly not yours (named in your task): " + ", ".join(self.excluded_files)
            )
        lines.extend(
            [
                "- Reading files owned by other workers is expected; editing them is not.",
                "",
                "DEPENDENCIES",
            ]
        )
        if self.upstream:
            lines.append("- Declared upstream steps: " + ", ".join(self.upstream[:8]) + ".")
        else:
            lines.append("- This step declares no upstream dependency in the task graph.")
        if self.referenced_siblings:
            lines.append(
                "- Your task text references sibling work: "
                + ", ".join(self.referenced_siblings[:MAX_REFERENCED_SIBLINGS])
                + ". Those modules may still be in progress; use their published interface."
            )
        lines.extend(
            [
                "- An upstream success message is not proof of correctness; check the interface "
                "you depend on.",
                "",
                "DELIVERABLES",
                f"- {self._deliverable_line()}",
                "- Do not report completion while a required artifact is missing or still "
                "unimplemented.",
                "",
                "VERIFICATION",
                "- Run a task-relevant public check you can execute yourself: an import check, a "
                "local smoke test, or a call with the examples in your task text.",
                "- If a check fails, read the output and repair the implementation when feasible.",
                "",
                "COMPLETION REPORT",
                "- State what you changed, which checks you ran, their observed result, and any "
                "remaining limitation.",
                "- Do not claim a check passed unless you ran it.",
            ]
        )
        return lines

    def _deliverable_line(self) -> str:
        if self.owned_files:
            return (
                "Files that must exist with the required functions implemented (no "
                "NotImplementedError left): " + ", ".join(self.owned_files) + "."
            )
        return (
            "Produce the artifacts named in your task description. Owned files are "
            f"{UNSPECIFIED}; keep changes inside /workspace/project."
        )


def _join(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else UNSPECIFIED


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in _SENTENCE_SPLIT.split(text) if item.strip()]


def _normalize_path(token: str) -> str:
    cleaned = token.strip().lstrip("./")
    for prefix in ("/workspace/project/", "workspace/project/"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned


def classify_paths(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split the paths named by public task text into (write targets, explicit exclusions).

    A path is a write target only when the nearest verb before it is a write verb ("read X then
    implement Y" owns Y only). A path inside a negated clause ("do not create final.json") is an
    exclusion. Nothing is inferred from metadata the text does not mention.
    """

    normalized = text.replace("/workspace/project/", "").replace("workspace/project/", "")
    owned: list[str] = []
    excluded: list[str] = []
    for sentence in _sentences(normalized):
        mentions = [(match.group(1), match.start()) for match in _PATH_RE.finditer(sentence)]
        if not mentions:
            continue
        if _NEGATION_RE.search(sentence):
            for raw, _ in mentions:
                path = _normalize_path(raw)
                if path not in excluded and path not in owned:
                    excluded.append(path)
            continue
        for raw, start in mentions:
            prefix = sentence[:start]
            last_write = max(
                (match.start() for match in _WRITE_VERB_RE.finditer(prefix)), default=-1
            )
            last_read = max((match.start() for match in _READ_VERB_RE.finditer(prefix)), default=-1)
            if last_write <= last_read:
                continue
            path = _normalize_path(raw)
            if path not in owned:
                owned.append(path)
            if path in excluded:
                excluded.remove(path)
    return tuple(sorted(owned)), tuple(sorted(excluded))


MIN_REFERENCE_NAME = 3


def referenced_siblings(step: WorkerStep, steps: tuple[WorkerStep, ...]) -> tuple[str, ...]:
    """Sibling steps or modules named by this step's public text (never ``depends_on``).

    Only names of at least three characters are matched, on word boundaries, so single-letter step
    ids (``a``/``b``) cannot be inferred from unrelated prose.
    """

    text = step.prompt.lower()
    names: list[str] = []
    for other in steps:
        if other.task_id == step.task_id:
            continue
        candidates = {other.task_id, _module_stem(other.task_id)}
        for candidate in sorted(candidates):
            if len(candidate) < MIN_REFERENCE_NAME:
                continue
            if re.search(rf"\b{re.escape(candidate.lower())}\b", text) and candidate not in names:
                names.append(candidate)
                break
    return tuple(sorted(names))


def _module_stem(task_id: str) -> str:
    return task_id.rsplit("-", 1)[-1] if "-" in task_id else task_id


def _strip_env(goal: str) -> str:
    text = goal.strip()
    if text.endswith(ENV_TAIL):
        text = text[: -len(ENV_TAIL)].strip()
    return text


def build_contract(
    step: WorkerStep, *, steps: tuple[WorkerStep, ...], sample_id: str
) -> WorkerContract:
    owned, excluded = classify_paths(step.prompt)
    return WorkerContract(
        sample_id=sample_id,
        step_id=step.task_id,
        goal=_strip_env(step.prompt),
        owned_files=owned,
        excluded_files=excluded,
        upstream=tuple(step.depends_on),
        referenced_siblings=referenced_siblings(step, steps),
    )


def worker_task_text(
    step: WorkerStep, *, steps: tuple[WorkerStep, ...], sample_id: str, mode: str
) -> str:
    """The worker's task turn for ``mode``.

    ``legacy`` returns the step prompt object-identical (no copy, no normalisation); ``structured``
    appends the contract block after it, so the original public task text is preserved verbatim.
    """

    if mode not in WORKER_CONTRACT_MODES:
        raise ValueError(f"unknown worker contract mode {mode!r}")
    if mode == WORKER_CONTRACT_LEGACY:
        return step.prompt
    contract = build_contract(step, steps=steps, sample_id=sample_id)
    return f"{step.prompt}\n\n{contract.render()}\n"


def contract_overhead(step_prompt: str, task_text: str) -> int:
    """Characters the contract adds to one worker task turn (0 in legacy mode)."""

    return max(0, len(task_text) - len(step_prompt))
