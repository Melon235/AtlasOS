"""Context optimizer implementations (identity and official ContextPilot)."""

from atlasos.context.optimizers.base import (
    ContextOptimizer,
    OptimizedContext,
    context_chars,
)
from atlasos.context.optimizers.contextpilot import (
    ContextPilotIntegrationError,
    ContextPilotOptimizer,
    ContextPilotUnavailable,
)
from atlasos.context.optimizers.identity import IdentityContextOptimizer

__all__ = [
    "ContextOptimizer",
    "ContextPilotIntegrationError",
    "ContextPilotOptimizer",
    "ContextPilotUnavailable",
    "IdentityContextOptimizer",
    "OptimizedContext",
    "context_chars",
]
