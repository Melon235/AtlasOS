"""STRUCTURAL checks for Python deliverables.

Parsing is static. Nothing here imports or executes the artifact; a parsed AST is the only input, so
a hostile module body cannot run on the host.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from atlasos.verification import models
from atlasos.verification.files import UnsafeArtifactPath, read_text


def _finding(
    check: models.CheckSpec,
    status: models.Status,
    reason: str,
    detail: str = "",
    evidence: str | None = None,
) -> models.VerificationFinding:
    return models.VerificationFinding(
        check_id=check.check_id,
        category=check.category,
        status=status,
        reason_code=reason,
        artifact=check.artifact,
        detail=detail,
        evidence_digest=evidence,
    )


def _parse(
    check: models.CheckSpec, snapshot: Path
) -> tuple[ast.Module | None, models.VerificationFinding | None]:
    artifact = check.artifact or ""
    try:
        source = read_text(snapshot, artifact)
    except FileNotFoundError:
        return None, _finding(check, "FAIL", models.REASON_MISSING)
    except (UnsafeArtifactPath, UnicodeDecodeError) as error:
        return None, _finding(check, "FAIL", models.REASON_UNREADABLE, str(error))
    if not source.strip():
        return None, _finding(check, "FAIL", models.REASON_EMPTY, "artifact is empty")
    try:
        return ast.parse(source, filename=artifact), None
    except SyntaxError as error:
        return None, _finding(
            check,
            "FAIL",
            models.REASON_PARSE_ERROR,
            f"{error.msg} at line {error.lineno}",
            evidence=models.canonical_digest({"artifact": artifact, "error": error.msg}),
        )


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def check_python_parses(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    tree, failure = _parse(check, snapshot)
    if failure is not None:
        return failure
    assert tree is not None
    return _finding(
        check,
        "PASS",
        models.REASON_OK,
        evidence=models.canonical_digest(
            {"artifact": check.artifact, "functions": sorted(_functions(tree))}
        ),
    )


def check_function_present(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    tree, failure = _parse(check, snapshot)
    if failure is not None:
        return failure
    assert tree is not None
    expected = list(check.params.get("functions") or [])
    defined = _functions(tree)
    missing = [name for name in expected if name not in defined]
    if missing:
        return _finding(check, "FAIL", models.REASON_MISSING_FUNCTION, f"missing: {missing}")
    return _finding(check, "PASS", models.REASON_OK, detail=f"present: {expected}")


def check_signature(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    tree, failure = _parse(check, snapshot)
    if failure is not None:
        return failure
    assert tree is not None
    defined = _functions(tree)
    problems: list[str] = []
    for name, expected in (check.params.get("signatures") or {}).items():
        node = defined.get(name)
        if node is None:
            problems.append(f"{name}: missing")
            continue
        actual = _signature(node)
        if actual != expected:
            problems.append(f"{name}: expected {expected}, found {actual}")
    if problems:
        return _finding(check, "FAIL", models.REASON_SIGNATURE_MISMATCH, "; ".join(problems))
    return _finding(check, "PASS", models.REASON_OK)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render ``name(pos, /, arg=default, *, kw=default)`` without annotations.

    Annotations are deliberately ignored: the public contract in the fixtures is expressed by
    parameter name, position and default, not by type hints a worker may omit.
    """

    parts: list[str] = []
    positional = [*node.args.posonlyargs, *node.args.args]
    defaults = [None] * (len(positional) - len(node.args.defaults)) + [
        ast.unparse(item) for item in node.args.defaults
    ]
    for index, argument in enumerate(positional):
        default = defaults[index]
        parts.append(argument.arg if default is None else f"{argument.arg}={default}")
        if node.args.posonlyargs and index + 1 == len(node.args.posonlyargs):
            parts.append("/")
    if node.args.vararg is not None:
        parts.append(f"*{node.args.vararg.arg}")
    elif node.args.kwonlyargs:
        parts.append("*")
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
        parts.append(argument.arg if default is None else f"{argument.arg}={ast.unparse(default)}")
    if node.args.kwarg is not None:
        parts.append(f"**{node.args.kwarg.arg}")
    return f"{node.name}({', '.join(parts)})"


def check_no_not_implemented(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    tree, failure = _parse(check, snapshot)
    if failure is not None:
        return failure
    assert tree is not None
    defined = _functions(tree)
    targets = list(check.params.get("functions") or sorted(defined))
    stubs: list[str] = []
    for name in targets:
        node = defined.get(name)
        if node is None:
            stubs.append(f"{name}: missing")
            continue
        if _raises_not_implemented(node):
            stubs.append(name)
    if stubs:
        return _finding(check, "FAIL", models.REASON_NOT_IMPLEMENTED, f"still stubs: {stubs}")
    return _finding(check, "PASS", models.REASON_OK)


def _raises_not_implemented(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for statement in node.body:
        if not isinstance(statement, ast.Raise) or statement.exc is None:
            continue
        exc = statement.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return True
        if isinstance(exc, ast.Attribute) and exc.attr == "NotImplementedError":
            return True
    # A body of ``...``/``pass``/docstring-only is an unimplemented function too.
    meaningful = [
        statement
        for statement in node.body
        if not (
            isinstance(statement, ast.Pass)
            or (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        )
    ]
    return not meaningful


def check_no_forbidden_import(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    tree, failure = _parse(check, snapshot)
    if failure is not None:
        return failure
    assert tree is not None
    forbidden = [str(item) for item in check.params.get("modules") or []]
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits.extend(alias.name for alias in node.names if _matches(alias.name, forbidden))
        elif isinstance(node, ast.ImportFrom) and node.module and _matches(node.module, forbidden):
            hits.append(node.module)
    if hits:
        return _finding(
            check, "FAIL", models.REASON_FORBIDDEN_IMPORT, f"forbidden imports: {sorted(set(hits))}"
        )
    return _finding(check, "PASS", models.REASON_OK)


def _matches(module: str, forbidden: list[str]) -> bool:
    return any(module == item or module.startswith(item + ".") for item in forbidden)


def check_import_binding(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    """The consumer must import exactly the names the provider is contracted to export."""

    tree, failure = _parse(check, snapshot)
    if failure is not None:
        return failure
    assert tree is not None
    module = str(check.params.get("module") or "")
    names = set(check.params.get("names") or [])
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            imported.update(alias.name for alias in node.names)
    missing = sorted(names - imported)
    if missing:
        return _finding(
            check,
            "FAIL",
            models.REASON_BINDING_MISMATCH,
            f"consumer never imports {missing} from {module}",
        )
    return _finding(check, "PASS", models.REASON_OK, detail=f"imports {sorted(names)}")


CHECKS = {
    models.CheckKind.PYTHON_PARSES: check_python_parses,
    models.CheckKind.FUNCTION_PRESENT: check_function_present,
    models.CheckKind.SIGNATURE_OK: check_signature,
    models.CheckKind.NO_NOT_IMPLEMENTED: check_no_not_implemented,
    models.CheckKind.NO_FORBIDDEN_IMPORT: check_no_forbidden_import,
    models.CheckKind.IMPORT_BINDING: check_import_binding,
}

__all__ = ["CHECKS", "_signature"]
