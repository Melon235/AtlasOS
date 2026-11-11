"""Textual shell-command classification for the ownership boundary.

The classifier answers one question only: *which workspace paths can this command provably
write?* It never runs the command, never executes a subprocess, and never consults the sandbox.

Three outcomes exist:

``PROVEN_READ_ONLY``
    Every segment is a program from a closed read-only allowlist (or a Git listing subcommand).
``KNOWN_WRITE_TARGETS``
    Every segment is either read-only or a recognised write with statically extractable targets.
``WORKSPACE_WIDE_WRITE``
    A recognised destructive operation whose file set cannot be enumerated (``git clean``,
    ``git stash``, ``git reset --hard``, ``find -delete``). Refused in ``enforce``.
``UNCLASSIFIABLE``
    Anything else - including every interpreter (``python3 -c``, ``node -e``, ``bash -c``),
    unknown programs, command substitution, and unsupported option combinations. Refused in
    ``enforce`` because its side effects cannot be bounded.

The classifier is deliberately conservative: a false ``UNCLASSIFIABLE`` only costs a denied
command, while a false ``PROVEN_READ_ONLY`` would break the ownership invariant.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass

from atlasos.trajectory.models import canonical_digest

PROVEN_READ_ONLY = "PROVEN_READ_ONLY"
KNOWN_WRITE_TARGETS = "KNOWN_WRITE_TARGETS"
WORKSPACE_WIDE_WRITE = "WORKSPACE_WIDE_WRITE"
UNCLASSIFIABLE = "UNCLASSIFIABLE"

_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n"})
_REDIRECTS = frozenset({">", ">>", "<", "<<", "<<<", ">&", "<&"})

_READ_ONLY_PROGRAMS = frozenset(
    {
        "basename",
        "cat",
        "cut",
        "date",
        "diff",
        "dirname",
        "egrep",
        "echo",
        "export",
        "false",
        "fgrep",
        "file",
        "grep",
        "head",
        "ls",
        "nl",
        "popd",
        "printf",
        "pushd",
        "pwd",
        "rg",
        "set",
        "sleep",
        "stat",
        "tail",
        "test",
        "tree",
        "true",
        "type",
        "unset",
        "wc",
        "which",
        "whoami",
        "[",
    }
)

#: Programs whose write targets are the trailing non-option arguments.
_TRAILING_TARGET_PROGRAMS = frozenset({"rm", "rmdir", "touch", "mkdir", "chmod", "chown"})
#: Programs that take a source and a destination; the destination is the write.
_DESTINATION_PROGRAMS = frozenset({"cp", "mv", "install", "ln"})

_GIT_READ_ONLY = frozenset(
    {
        "blame",
        "cat-file",
        "describe",
        "diff",
        "log",
        "ls-files",
        "ls-tree",
        "rev-parse",
        "shortlog",
        "show",
        "status",
    }
)
_GIT_INDEX_ONLY = frozenset({"add", "rm", "mv"})
_GIT_WORKSPACE_WIDE = frozenset(
    {"clean", "stash", "switch", "checkout-index", "rebase", "merge", "apply", "am", "cherry-pick"}
)

_SHELL_METACHARACTERS = ("$(", "`", "${", "<(", ">(")

#: A version probe cannot execute repository code, so it is the one interpreter form that is
#: provably read-only. Everything else that runs an interpreter stays ``UNCLASSIFIABLE``.
_VERSION_PROBE_ARGUMENTS = frozenset({"-V", "-VV", "--version"})
_VERSION_PROBE_PROGRAMS = frozenset(
    {
        "python",
        "python3",
        "node",
        "npm",
        "npx",
        "pip",
        "pip3",
        "uv",
        "java",
        "go",
        "cargo",
        "pytest",
    }
)


@dataclass(frozen=True)
class ShellAssessment:
    """One classification result."""

    kind: str
    write_targets: tuple[str, ...]
    reason: str
    command_digest: str
    creates_directories: bool = False

    @property
    def provably_safe(self) -> bool:
        return self.kind == PROVEN_READ_ONLY

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "write_targets": list(self.write_targets),
            "reason": self.reason,
            "command_digest": self.command_digest,
            "creates_directories": self.creates_directories,
        }


def classify_shell_command(command: str) -> ShellAssessment:
    """Classify one shell command without executing it."""

    digest = canonical_digest({"command": command})
    text = command.strip()
    if not text:
        return ShellAssessment(UNCLASSIFIABLE, (), "empty command", digest)
    for marker in _SHELL_METACHARACTERS:
        if marker in text:
            return ShellAssessment(
                UNCLASSIFIABLE,
                (),
                f"shell metacharacter {marker!r} cannot be analysed statically",
                digest,
            )
    # ``&>`` is a redirect, not a background separator.
    if "&>" in text:
        text = text.replace("&>", ">")

    try:
        tokens = _tokenize(text)
    except ValueError as error:
        return ShellAssessment(UNCLASSIFIABLE, (), f"unparseable shell: {error}", digest)
    if not tokens:
        return ShellAssessment(UNCLASSIFIABLE, (), "empty command", digest)

    segments = _split_segments(tokens)
    targets: list[str] = []
    kinds: list[str] = []
    reasons: list[str] = []
    all_mkdir = True
    cwd = ""
    for segment in segments:
        # ``cd`` is a read-only shell builtin, but every later relative write inherits it. The
        # working directory is tracked so ``cd sub && rm x`` authorizes ``sub/x``, not ``x``.
        if _program_of(segment) == "cd":
            moved, reason = _resolve_cd(segment, cwd)
            if moved is None:
                kinds.append(UNCLASSIFIABLE)
                reasons.append(reason)
            else:
                cwd = moved
                kinds.append(PROVEN_READ_ONLY)
                reasons.append("cd")
            continue
        assessment = _classify_segment(segment)
        if assessment is None:
            continue
        kind, segment_targets, reason, is_mkdir = assessment
        kinds.append(kind)
        reasons.append(reason)
        targets.extend(_resolve_target(item, cwd) for item in segment_targets)
        all_mkdir = all_mkdir and is_mkdir
    if not kinds:
        return ShellAssessment(UNCLASSIFIABLE, (), "no analyzable command segment", digest)
    if UNCLASSIFIABLE in kinds:
        return ShellAssessment(UNCLASSIFIABLE, (), "; ".join(reasons), digest)
    if WORKSPACE_WIDE_WRITE in kinds:
        return ShellAssessment(WORKSPACE_WIDE_WRITE, (), "; ".join(reasons), digest)
    if KNOWN_WRITE_TARGETS in kinds:
        return ShellAssessment(
            KNOWN_WRITE_TARGETS,
            tuple(dict.fromkeys(targets)),
            "; ".join(reasons),
            digest,
            creates_directories=all_mkdir and bool(targets),
        )
    return ShellAssessment(PROVEN_READ_ONLY, (), "read-only command", digest)


def _tokenize(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _classify_segment(
    tokens: list[str],
) -> tuple[str, tuple[str, ...], str, bool] | None:
    """Return ``(kind, targets, reason, is_mkdir)`` for one segment, or None if empty."""

    words: list[str] = []
    redirect_targets: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _REDIRECTS:
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            if token in (">", ">>", ">|") and target:
                redirect_targets.append(target)
            index += 2
            continue
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in _REDIRECTS:
            # ``2> file`` / ``2>> file``: the descriptor is not a program argument.
            index += 1
            continue
        words.append(token)
        index += 1

    if not words:
        if redirect_targets:
            return (KNOWN_WRITE_TARGETS, tuple(redirect_targets), "output redirection", False)
        return None

    program = words[0].rsplit("/", 1)[-1]
    arguments = words[1:]

    if program == "git":
        kind, targets, reason = _classify_git(arguments)
        kind = _promote_for_redirect(kind, redirect_targets)
        return (kind, tuple(targets) + tuple(redirect_targets), reason, False)

    if program == "find":
        kind, reason = _classify_find(arguments)
        kind = _promote_for_redirect(kind, redirect_targets)
        return (kind, tuple(redirect_targets), reason, False)

    if program == "sed":
        kind, targets, reason = _classify_sed(arguments)
        kind = _promote_for_redirect(kind, redirect_targets)
        return (kind, tuple(targets) + tuple(redirect_targets), reason, False)

    if program in _READ_ONLY_PROGRAMS:
        kind = KNOWN_WRITE_TARGETS if redirect_targets else PROVEN_READ_ONLY
        reason = "read-only program" if not redirect_targets else "read-only program with redirect"
        return (kind, tuple(redirect_targets), reason, False)

    if program == "tee":
        return (KNOWN_WRITE_TARGETS, tuple(arguments) + tuple(redirect_targets), "tee", False)

    if program in _TRAILING_TARGET_PROGRAMS:
        positional = [item for item in arguments if not item.startswith("-")]
        kind = KNOWN_WRITE_TARGETS
        reason = f"{program} writes its arguments"
        return (kind, tuple(positional) + tuple(redirect_targets), reason, program == "mkdir")

    if program in _DESTINATION_PROGRAMS:
        positional = [item for item in arguments if not item.startswith("-")]
        if len(positional) < 2:
            return (
                UNCLASSIFIABLE,
                (),
                f"{program} without a resolvable destination",
                False,
            )
        destination = positional[-1]
        if program == "mv":
            return (
                KNOWN_WRITE_TARGETS,
                tuple(positional) + tuple(redirect_targets),
                "mv rewrites source and destination",
                False,
            )
        return (
            KNOWN_WRITE_TARGETS,
            (destination, *redirect_targets),
            f"{program} destination",
            False,
        )

    if program == "truncate":
        positional = [item for item in arguments if not item.startswith("-")]
        return (KNOWN_WRITE_TARGETS, tuple(positional), "truncate", False)

    if _is_version_probe(program, arguments):
        kind = _promote_for_redirect(PROVEN_READ_ONLY, redirect_targets)
        return (kind, tuple(redirect_targets), f"{program} version probe", False)

    return (
        UNCLASSIFIABLE,
        (),
        f'program "{program}" has no analyzable write set',
        False,
    )


_PYTHON_PROGRAM = re.compile(r"^python[0-9.]*$")


def _is_version_probe(program: str, arguments: list[str]) -> bool:
    """True only for ``<interpreter> --version`` with a single version flag."""

    if len(arguments) != 1 or arguments[0] not in _VERSION_PROBE_ARGUMENTS:
        return False
    return program in _VERSION_PROBE_PROGRAMS or bool(_PYTHON_PROGRAM.fullmatch(program))


def _program_of(segment: list[str]) -> str:
    for index, token in enumerate(segment):
        if token in _REDIRECTS:
            continue
        if token.isdigit() and index + 1 < len(segment) and segment[index + 1] in _REDIRECTS:
            continue
        return token.rsplit("/", 1)[-1]
    return ""


def _resolve_cd(segment: list[str], cwd: str) -> tuple[str | None, str]:
    words = [token for token in segment[1:] if token not in _REDIRECTS]
    if not words:
        return None, "cd without a target cannot be verified"
    if len(words) > 1:
        return None, "cd with several targets cannot be verified"
    target = words[0]
    if target in ("-", "~") or target.startswith("~/"):
        return None, f"cd {target!r} leaves the workspace"
    if target.startswith("/"):
        return target, "cd"
    if not cwd:
        return target, "cd"
    if cwd.startswith("/"):
        return posixpath.normpath(posixpath.join(cwd, target)), "cd"
    return posixpath.normpath(posixpath.join(cwd, target)), "cd"


def _resolve_target(raw: str, cwd: str) -> str:
    if not cwd or raw.startswith("/"):
        return raw
    return posixpath.normpath(posixpath.join(cwd, raw))


def _promote_for_redirect(kind: str, redirect_targets: list[str]) -> str:
    """A read-only program that redirects output still writes a file."""

    if redirect_targets and kind == PROVEN_READ_ONLY:
        return KNOWN_WRITE_TARGETS
    return kind


def _classify_git(arguments: list[str]) -> tuple[str, list[str], str]:
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        option = arguments[index]
        index += 1
        if option in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            index += 1
    if index >= len(arguments):
        return (PROVEN_READ_ONLY, [], "git without a subcommand")
    subcommand = arguments[index]
    rest = arguments[index + 1 :]

    if subcommand in _GIT_READ_ONLY:
        return (PROVEN_READ_ONLY, [], f"git {subcommand} is read-only")
    if subcommand == "branch":
        listing = rest and all(item.startswith("-") for item in rest)
        if not rest or listing:
            return (PROVEN_READ_ONLY, [], "git branch listing")
        return (UNCLASSIFIABLE, [], "git branch modifies refs")
    if subcommand in ("tag", "remote", "config"):
        mutable = any(
            item in ("-d", "--delete", "-a", "--add", "--unset", "--replace-all", "-e", "--edit")
            for item in rest
        )
        if mutable:
            return (UNCLASSIFIABLE, [], f"git {subcommand} modifies repository state")
        return (PROVEN_READ_ONLY, [], f"git {subcommand} is read-only")
    if subcommand == "checkout":
        if "--" in rest:
            targets = [item for item in rest[rest.index("--") + 1 :] if not item.startswith("-")]
            if not targets:
                return (WORKSPACE_WIDE_WRITE, [], "git checkout -- without a path")
            return (
                KNOWN_WRITE_TARGETS,
                targets,
                "git checkout -- rewrites the named paths",
            )
        return (
            WORKSPACE_WIDE_WRITE,
            [],
            "git checkout without an explicit path can restore the whole tree",
        )
    if subcommand == "restore":
        if "--staged" in rest and "--worktree" not in rest:
            return (KNOWN_WRITE_TARGETS, [], "git restore --staged only touches the index")
        targets = [item for item in rest if not item.startswith("-") and "=" not in item]
        if not targets:
            return (WORKSPACE_WIDE_WRITE, [], "git restore without a path")
        return (KNOWN_WRITE_TARGETS, targets, "git restore rewrites the named paths")
    if subcommand == "reset":
        if any(item in ("--hard", "--keep", "--merge") for item in rest):
            return (WORKSPACE_WIDE_WRITE, [], "git reset --hard rewrites the whole worktree")
        return (KNOWN_WRITE_TARGETS, [], "git reset without --hard only touches the index")
    if subcommand in _GIT_WORKSPACE_WIDE:
        return (WORKSPACE_WIDE_WRITE, [], f"git {subcommand} has no enumerable file set")
    if subcommand in _GIT_INDEX_ONLY:
        return (KNOWN_WRITE_TARGETS, [], f"git {subcommand} does not rewrite worktree files")
    return (UNCLASSIFIABLE, [], f"git {subcommand} is not classified")


def _classify_find(arguments: list[str]) -> tuple[str, str]:
    for item in arguments:
        if item in ("-delete",):
            return (WORKSPACE_WIDE_WRITE, "find -delete removes an unbounded file set")
        if item in ("-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fls"):
            return (UNCLASSIFIABLE, f"find {item} runs arbitrary side effects")
    return (PROVEN_READ_ONLY, "find without destructive actions")


def _classify_sed(arguments: list[str]) -> tuple[str, list[str], str]:
    in_place = any(
        item == "--in-place" or (item.startswith("-i") and not item.startswith("--"))
        for item in arguments
    )
    positional = [item for item in arguments if not item.startswith("-")]
    if not in_place:
        return (PROVEN_READ_ONLY, [], "sed without -i")
    files = positional[1:]
    if not files:
        return (UNCLASSIFIABLE, [], "sed -i without a target file")
    return (KNOWN_WRITE_TARGETS, files, "sed -i rewrites the named files")


#: R1-1 does not attempt static side-effect analysis of arbitrary programs.
_INTERPRETER_HINT = re.compile(r"\b(python|node|perl|ruby|bash|sh|zsh|pwsh|powershell)\b")


def looks_like_interpreter(command: str) -> bool:
    """Diagnostic helper used by the report; it never widens authorization."""

    return bool(_INTERPRETER_HINT.search(command))


__all__ = [
    "KNOWN_WRITE_TARGETS",
    "PROVEN_READ_ONLY",
    "ShellAssessment",
    "UNCLASSIFIABLE",
    "WORKSPACE_WIDE_WRITE",
    "classify_shell_command",
    "looks_like_interpreter",
]
