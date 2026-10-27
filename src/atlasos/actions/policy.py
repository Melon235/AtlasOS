"""Agent-Guard-inspired deterministic policy: explicit default, ordered rules, first match."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atlasos.actions.models import (
    ActionDecisionValue,
    ActionRequest,
    PolicyResult,
)


class PolicyLoadError(ValueError):
    pass


class ActionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    decision: ActionDecisionValue
    action: str | None = None
    resource_prefix: str | None = None
    agent: str | None = None
    operating_scope: str | None = None

    def matches(self, request: ActionRequest) -> bool:
        if self.action is not None and self.action != request.action:
            return False
        if self.resource_prefix is not None and not request.resource.startswith(
            self.resource_prefix
        ):
            return False
        if self.agent is not None and self.agent != request.agent:
            return False
        return not (
            self.operating_scope is not None
            and self.operating_scope != request.context.operating_scope
        )


class ActionPolicy(BaseModel):
    """Immutable policy. Default is mandatory and may not be ALLOW."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    default: ActionDecisionValue
    rules: tuple[ActionRule, ...] = ()

    @model_validator(mode="after")
    def _validate_default(self) -> ActionPolicy:
        if self.default == "ALLOW":
            raise ValueError("action policy default must be DENY or REQUIRE_HUMAN")
        return self

    def decide(self, request: ActionRequest) -> PolicyResult:
        for rule in self.rules:
            if rule.matches(request):
                return PolicyResult(
                    decision=rule.decision,
                    matched_rule=rule.id,
                    reason_codes=(f"rule:{rule.id}",),
                    policy_name=self.name,
                )
        reason = "default_deny" if self.default == "DENY" else "default_require_human"
        return PolicyResult(
            decision=self.default,
            matched_rule=None,
            reason_codes=(reason,),
            policy_name=self.name,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActionPolicy:
        if "default" not in payload:
            raise PolicyLoadError("action policy must declare an explicit default")
        try:
            return cls.model_validate(payload)
        except Exception as error:
            raise PolicyLoadError(f"invalid action policy: {error}") from error

    @classmethod
    def from_file(cls, path: str | Path) -> ActionPolicy:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PolicyLoadError(f"cannot load action policy {path}: {error}") from error
        if not isinstance(payload, dict):
            raise PolicyLoadError("action policy root must be a JSON object")
        return cls.from_dict(payload)
