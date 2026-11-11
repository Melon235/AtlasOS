"""Stage R1-1: shell classification for the two observed cross-worker rollbacks (T4-T7)."""

from __future__ import annotations

import pytest

from atlasos.ownership import (
    KNOWN_WRITE_TARGETS,
    PROVEN_READ_ONLY,
    UNCLASSIFIABLE,
    WORKSPACE_WIDE_WRITE,
    classify_shell_command,
)


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git log --oneline -5",
        "git diff HEAD -- ledger/money.py",
        "git show HEAD:ledger/money.py",
        "ls -la ledger",
        "cat ledger/money.py",
        "grep -rn money ledger",
        "git rev-parse HEAD",
        "find . -name '*.py'",
        "git branch -a",
    ],
)
def test_read_only_commands_are_proven(command: str) -> None:
    assessment = classify_shell_command(command)
    assert assessment.kind == PROVEN_READ_ONLY, assessment.reason


@pytest.mark.parametrize(
    ("command", "targets"),
    [
        ("git checkout -- ledger/money.py", ("ledger/money.py",)),
        ("git checkout -- consumers/report.py", ("consumers/report.py",)),
        ("git restore ledger/money.py", ("ledger/money.py",)),
        ("git restore --source=HEAD consumers/report.py", ("consumers/report.py",)),
        (
            "git checkout HEAD -- ledger/money.py consumers/report.py",
            ("ledger/money.py", "consumers/report.py"),
        ),
        ("rm -f ledger/money.py", ("ledger/money.py",)),
        ("touch ledger/new.py", ("ledger/new.py",)),
        ("mkdir -p ledger/nested", ("ledger/nested",)),
        ("sed -i 's/a/b/' ledger/money.py", ("ledger/money.py",)),
        ("echo bad > ledger/money.py", ("ledger/money.py",)),
        ("cp /tmp/x ledger/money.py", ("ledger/money.py",)),
    ],
)
def test_write_commands_expose_every_target(command: str, targets: tuple[str, ...]) -> None:
    assessment = classify_shell_command(command)
    assert assessment.kind == KNOWN_WRITE_TARGETS, assessment.reason
    assert assessment.write_targets == targets


def test_multi_target_git_rollback_is_not_partially_trusted() -> None:
    """T5: both paths must be checked, not just the first."""

    assessment = classify_shell_command("git checkout -- ledger/entries.py ledger/money.py")
    assert assessment.write_targets == ("ledger/entries.py", "ledger/money.py")


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git stash",
        "git stash push -m wip",
        "git checkout main",
        "git switch other",
        "git apply patch.diff",
        "find . -name '*.py' -delete",
    ],
)
def test_workspace_wide_operations_have_no_enumerable_targets(command: str) -> None:
    assessment = classify_shell_command(command)
    assert assessment.kind == WORKSPACE_WIDE_WRITE, assessment.reason


@pytest.mark.parametrize(
    "command",
    [
        """python3 -c "open('ledger/money.py','w').write('bad')" """.strip(),
        "node -e \"require('fs').writeFileSync('ledger/money.py','bad')\"",
        "bash -c 'echo bad > ledger/money.py'",
        "echo $(git rev-parse HEAD) > ledger/money.py",
        "echo `date` > ledger/money.py",
        "git checkout -- $(git ls-files ledger)",
        "env FOO=1 python3 script.py",
        "rsync -a /tmp/x ledger/",
        "curl -o ledger/money.py https://example.invalid/x",
        "unknown-binary --write ledger/money.py",
    ],
)
def test_arbitrary_programs_are_unclassifiable(command: str) -> None:
    """T7: interpreters and unknown programs are never assumed read-only."""

    assessment = classify_shell_command(command)
    assert assessment.kind == UNCLASSIFIABLE, assessment.reason


def test_compound_command_is_denied_when_any_segment_is_unclassifiable() -> None:
    assessment = classify_shell_command('ls ledger && python3 -c "print(1)"')
    assert assessment.kind == UNCLASSIFIABLE


def test_compound_command_aggregates_write_targets() -> None:
    assessment = classify_shell_command("cat ledger/money.py && git checkout -- ledger/money.py")
    assert assessment.kind == KNOWN_WRITE_TARGETS
    assert assessment.write_targets == ("ledger/money.py",)


def test_pipe_to_tee_is_a_write() -> None:
    assessment = classify_shell_command("cat ledger/money.py | tee ledger/backup.py")
    assert assessment.kind == KNOWN_WRITE_TARGETS
    assert assessment.write_targets == ("ledger/backup.py",)


def test_stderr_redirect_does_not_become_a_program_argument() -> None:
    assessment = classify_shell_command("git status 2> ledger/log.txt")
    assert assessment.kind == KNOWN_WRITE_TARGETS
    assert assessment.write_targets == ("ledger/log.txt",)


def test_mkdir_is_marked_as_directory_creation() -> None:
    assessment = classify_shell_command("mkdir -p ledger/nested")
    assert assessment.creates_directories is True
    assert classify_shell_command("rm ledger/money.py").creates_directories is False


def test_cd_followed_by_read_only_commands_is_proven_read_only() -> None:
    assessment = classify_shell_command(
        "cd /workspace/project && ls -la && cat ledger/money.py; git log --oneline -3"
    )
    assert assessment.kind == PROVEN_READ_ONLY, assessment.reason


def test_cd_is_inherited_by_later_relative_write_targets() -> None:
    assessment = classify_shell_command("cd ledger && rm money.py")
    assert assessment.kind == KNOWN_WRITE_TARGETS
    assert assessment.write_targets == ("ledger/money.py",)


def test_cd_outside_the_workspace_keeps_the_absolute_target() -> None:
    assessment = classify_shell_command("cd /etc && rm passwd")
    assert assessment.kind == KNOWN_WRITE_TARGETS
    assert assessment.write_targets == ("/etc/passwd",)


@pytest.mark.parametrize("command", ["cd ~", "cd -", "cd /tmp ~", "cd"])
def test_unverifiable_cd_is_unclassifiable(command: str) -> None:
    assert classify_shell_command(command).kind == UNCLASSIFIABLE


def test_git_log_without_context_is_read_only() -> None:
    assert classify_shell_command("git log").kind == PROVEN_READ_ONLY


@pytest.mark.parametrize(
    "command",
    [
        "python3 -V",
        "python3 --version",
        "python3.12 -VV",
        "node --version",
        "cd /workspace/project && python3 --version && git ls-files",
    ],
)
def test_interpreter_version_probe_is_read_only(command: str) -> None:
    assert classify_shell_command(command).kind == PROVEN_READ_ONLY, command


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'print(1)'",
        "python3 -m pytest -q",
        "python3 script.py",
        "python3 -V script.py",
        'node -e \'fs.writeFileSync("a","b")\'',
    ],
)
def test_interpreter_execution_is_still_unclassifiable(command: str) -> None:
    assert classify_shell_command(command).kind == UNCLASSIFIABLE, command
