"""Local application layer for AtlasOS CLI and Web interaction."""

from atlasos.app.service import (
    ApplicationError,
    ApprovalAuthorityUnavailable,
    AtlasApplicationService,
    RecoveryAuthorityUnavailable,
    WorkspaceNotSupported,
)

__all__ = [
    "ApplicationError",
    "ApprovalAuthorityUnavailable",
    "AtlasApplicationService",
    "RecoveryAuthorityUnavailable",
    "WorkspaceNotSupported",
]
