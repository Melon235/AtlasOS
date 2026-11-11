"""Stage R1-1 workspace ownership: Host-owned write authorization for shared workspaces.

The package separates four concerns:

* :mod:`atlasos.ownership.models` - immutable manifest, modes, reason codes.
* :mod:`atlasos.ownership.manifest` - trusted construction and conflict rejection.
* :mod:`atlasos.ownership.paths` - canonical path normalization (no string prefix matching).
* :mod:`atlasos.ownership.shell` - textual shell classification.
* :mod:`atlasos.ownership.policy` - the M3 external-policy adapter.

It is *not* a workspace isolation subsystem. A worker still shares one workspace and one sandbox;
this package bounds which of those shared files the worker may write.
"""

from atlasos.ownership.manifest import OwnershipManifestError, build_manifest
from atlasos.ownership.models import (
    OWNERSHIP_COVERAGE_PARTIAL,
    OWNERSHIP_DENIED_FOREIGN_PATH,
    OWNERSHIP_DENIED_UNDECLARED_PATH,
    OWNERSHIP_DENIED_WORKSPACE_WIDE,
    OWNERSHIP_INVALID_MANIFEST,
    OWNERSHIP_MODE_AUDIT,
    OWNERSHIP_MODE_ENFORCE,
    OWNERSHIP_MODE_OFF,
    OWNERSHIP_MODES,
    OWNERSHIP_PATH_ESCAPE,
    OWNERSHIP_POLICY_VERSION,
    OWNERSHIP_UNCLASSIFIABLE_SHELL,
    OWNERSHIP_UNKNOWN_WORKER,
    PROVENANCE_HOST,
    PROVENANCE_VERIFIED_TASK,
    OwnedPathDeclaration,
    OwnershipMode,
    WorkspaceOwnershipManifest,
    path_is_within,
)
from atlasos.ownership.paths import (
    OwnershipPathError,
    normalize_declared_path,
    normalize_workspace_path,
)
from atlasos.ownership.policy import (
    OwnershipEvidence,
    OwnershipFinding,
    WorkspaceOwnershipPolicy,
    build_ownership_enforcer,
)
from atlasos.ownership.shell import (
    KNOWN_WRITE_TARGETS,
    PROVEN_READ_ONLY,
    UNCLASSIFIABLE,
    WORKSPACE_WIDE_WRITE,
    ShellAssessment,
    classify_shell_command,
)

__all__ = [
    "KNOWN_WRITE_TARGETS",
    "OWNERSHIP_COVERAGE_PARTIAL",
    "OWNERSHIP_DENIED_FOREIGN_PATH",
    "OWNERSHIP_DENIED_UNDECLARED_PATH",
    "OWNERSHIP_DENIED_WORKSPACE_WIDE",
    "OWNERSHIP_INVALID_MANIFEST",
    "OWNERSHIP_MODE_AUDIT",
    "OWNERSHIP_MODE_ENFORCE",
    "OWNERSHIP_MODE_OFF",
    "OWNERSHIP_MODES",
    "OWNERSHIP_PATH_ESCAPE",
    "OWNERSHIP_POLICY_VERSION",
    "OWNERSHIP_UNCLASSIFIABLE_SHELL",
    "OWNERSHIP_UNKNOWN_WORKER",
    "PROVENANCE_HOST",
    "PROVENANCE_VERIFIED_TASK",
    "PROVEN_READ_ONLY",
    "UNCLASSIFIABLE",
    "WORKSPACE_WIDE_WRITE",
    "OwnedPathDeclaration",
    "OwnershipEvidence",
    "OwnershipFinding",
    "OwnershipManifestError",
    "OwnershipMode",
    "OwnershipPathError",
    "ShellAssessment",
    "WorkspaceOwnershipManifest",
    "WorkspaceOwnershipPolicy",
    "build_manifest",
    "build_ownership_enforcer",
    "classify_shell_command",
    "normalize_declared_path",
    "normalize_workspace_path",
    "path_is_within",
]
