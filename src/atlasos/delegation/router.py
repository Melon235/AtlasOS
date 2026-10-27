"""Deterministic intent × capability route table. No LLM, embeddings, or risk scalar."""

from __future__ import annotations

from dataclasses import dataclass

from atlasos.delegation.models import (
    GATE_ORDER,
    DelegationIntent,
    Gate,
    RouteDecision,
)

SENSITIVE_DOMAINS = frozenset({"credentials", "secrets", "keys", "tokens"})
PRIVILEGED_DOMAINS = frozenset({"admin", "iam", "billing", "security"})


@dataclass(frozen=True)
class RouteRule:
    rule_id: str
    intent: DelegationIntent
    scopes: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    gates: frozenset[Gate] = frozenset()
    reason_code: str = ""

    def matches(self, scope: str) -> bool:
        if scope in self.scopes:
            return True
        return scope.split(".", 1)[0] in self.domains


ROUTE_RULES: tuple[RouteRule, ...] = (
    RouteRule(
        "research.github.read",
        "research",
        scopes=frozenset({"github.read"}),
        reason_code="research_read",
    ),
    RouteRule(
        "research.rag.read",
        "research",
        scopes=frozenset({"rag.read"}),
        reason_code="research_read",
    ),
    RouteRule(
        "read_data.filesystem.read",
        "read_data",
        scopes=frozenset({"filesystem.read"}),
        reason_code="read_data_read",
    ),
    RouteRule(
        "read_data.database.read",
        "read_data",
        scopes=frozenset({"database.read"}),
        reason_code="read_data_read",
    ),
    RouteRule(
        "modify_data.github.write",
        "modify_data",
        scopes=frozenset({"github.write"}),
        gates=frozenset({"POLICY"}),
        reason_code="write_requires_policy",
    ),
    RouteRule(
        "modify_data.filesystem.write",
        "modify_data",
        scopes=frozenset({"filesystem.write"}),
        gates=frozenset({"POLICY"}),
        reason_code="write_requires_policy",
    ),
    RouteRule(
        "modify_data.database.write",
        "modify_data",
        scopes=frozenset({"database.write"}),
        gates=frozenset({"POLICY"}),
        reason_code="write_requires_policy",
    ),
    RouteRule(
        "execute_code.shell.execute",
        "execute_code",
        scopes=frozenset({"shell.execute"}),
        gates=frozenset({"POLICY"}),
        reason_code="execute_requires_policy",
    ),
    RouteRule(
        "external_communication.email.send",
        "external_communication",
        scopes=frozenset({"email.send"}),
        gates=frozenset({"HITL"}),
        reason_code="external_send_requires_hitl",
    ),
    RouteRule(
        "manage_credentials.sensitive",
        "manage_credentials",
        domains=SENSITIVE_DOMAINS,
        gates=frozenset({"POLICY", "HITL"}),
        reason_code="sensitive_capability_requires_both",
    ),
    RouteRule(
        "administrative.privileged",
        "administrative",
        domains=PRIVILEGED_DOMAINS,
        gates=frozenset({"POLICY", "HITL"}),
        reason_code="privileged_capability_requires_both",
    ),
)


def _ordered_gates(gates: set[Gate]) -> tuple[Gate, ...]:
    return tuple(gate for gate in GATE_ORDER if gate in gates)


class IntentRouter:
    """Deterministic route table with safe HITL fallback for every unmatched scope."""

    def __init__(self, rules: tuple[RouteRule, ...] = ROUTE_RULES) -> None:
        self._rules = rules

    @property
    def rules(self) -> tuple[RouteRule, ...]:
        return self._rules

    def route(self, intent: DelegationIntent, scopes: tuple[str, ...]) -> RouteDecision:
        matched_rules: set[str] = set()
        reasons: set[str] = set()
        gates: set[Gate] = set()
        for scope in scopes:
            scope_matched = False
            for rule in self._rules:
                if rule.intent != intent or not rule.matches(scope):
                    continue
                scope_matched = True
                matched_rules.add(rule.rule_id)
                reasons.add(rule.reason_code)
                gates.update(rule.gates)
            if not scope_matched:
                matched_rules.add("unmatched")
                reasons.add("unmatched_intent_capability")
                gates.add("HITL")
        if not scopes:
            matched_rules.add("unmatched")
            reasons.add("unmatched_intent_capability")
            gates.add("HITL")
        return RouteDecision(
            matched_rules=tuple(sorted(matched_rules)),
            required_gates=_ordered_gates(gates),
            reason_codes=tuple(sorted(reasons)),
        )
