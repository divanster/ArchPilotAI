"""Model routing and escalation of an agent run: pure policy, no I/O.

A policy maps each kind of agent action to a light or a heavy deployment. Escalation moves a run
to the heavy deployment after repeated trouble and never moves it back within the run (one run is
one repair attempt). Budgets end a run with a distinct stop reason.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

LIGHT, HEAVY = "light", "heavy"
POLICY, ESCALATION, USER_SELECTED = "policy", "escalation", "user_selected"

# Action types of a model call and the tier each one uses.
FILE_SELECTION = "file_selection"
SHORT_EDIT = "short_edit"
FORMATTING = "formatting"
SIMPLE_TOOL_CALL = "simple_tool_call"
CHECKLIST_PROPOSAL = "checklist_proposal"
CI_DIAGNOSIS = "ci_diagnosis"
STATIC_DIAGNOSIS = "static_diagnosis"
REPAIR = "repair"
SELF_REVIEW = "self_review"

ACTION_TIERS: dict[str, str] = {
    FILE_SELECTION: LIGHT,
    SHORT_EDIT: LIGHT,
    FORMATTING: LIGHT,
    SIMPLE_TOOL_CALL: LIGHT,
    CHECKLIST_PROPOSAL: HEAVY,
    CI_DIAGNOSIS: HEAVY,
    STATIC_DIAGNOSIS: HEAVY,
    REPAIR: HEAVY,
    SELF_REVIEW: HEAVY,
}

# Escalation triggers (the routing detail of an escalated call).
INVALID_ACTIONS = "invalid_actions"
REPEATED_FAILURE = "repeated_failure"
REASONING_EXHAUSTED = "reasoning_budget_exhausted"

INVALID_CODES = frozenset({"invalid_shape", "invalid_args"})
INVALID_ACTIONS_LIMIT = 2
REPEAT_SIGNATURE_LIMIT = 2

# Budget stop reasons.
TOKEN_LIMIT, HEAVY_CALL_LIMIT, COST_LIMIT = "token_limit", "heavy_call_limit", "cost_limit"

_FORMAT_ERRORS = frozenset(
    {"invalid_json", "unknown_action", "output_truncated", "output_too_large"}
)
_NEUTRAL_ERRORS = frozenset({"tests_unavailable", "unknown_command", "checks_unavailable"})
_EDITS = frozenset({"edit_file", "write_file", "delete_file"})
_CHECKS = frozenset({"run_static_checks", "run_tests"})
_FORMATTING_CODES = frozenset({"E501", "W291", "W293", "W391", "I001", "Q000", "COM812", "E303"})


class RoutingError(Exception):
    pass


class UnverifiedDeploymentError(RoutingError):
    def __init__(self, deployment: str) -> None:
        self.deployment = deployment
        super().__init__(f"The deployment {deployment[:100]!r} is not marked as verified")


@dataclass(frozen=True)
class Route:
    """The deployment chosen for one model call and why."""

    deployment: str
    tier: str | None  # None when routing is off
    reason: str  # policy | escalation | user_selected
    detail: str  # the action type, or the escalation trigger

    @property
    def heavy(self) -> bool:
        return self.tier == HEAVY


@dataclass(frozen=True)
class RoutingPolicy:
    enabled: bool = False
    light: str = ""  # blank: the run's chosen deployment
    heavy: str = ""
    verified: frozenset[str] = frozenset()
    # deployment -> (input, output) price per 1,000 tokens
    prices: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    token_limit: int = 0  # 0 = no limit
    heavy_call_limit: int = 0
    cost_limit: float = 0.0

    def problems(self) -> list[str]:
        """Configured deployments that may not be routed to (empty when valid or off)."""
        if not self.enabled:
            return []
        names = {self.light.strip(), self.heavy.strip()} - {""}
        return [f"{n} is not marked as verified" for n in sorted(names - self.verified)]

    def deployment_for(self, tier: str, default: str) -> str:
        configured = (self.light if tier == LIGHT else self.heavy).strip()
        if not configured:
            return default
        if configured not in self.verified:
            raise UnverifiedDeploymentError(configured)
        return configured

    def cost(self, deployment: str, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self.prices.get(deployment, (0.0, 0.0))
        return (input_tokens * price_in + output_tokens * price_out) / 1000

    def public(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "light": self.light.strip() or None,
            "heavy": self.heavy.strip() or None,
            "token_limit": self.token_limit or None,
            "heavy_call_limit": self.heavy_call_limit or None,
            "cost_limit": self.cost_limit or None,
            "light_actions": sorted(a for a, t in ACTION_TIERS.items() if t == LIGHT),
            "heavy_actions": sorted(a for a, t in ACTION_TIERS.items() if t == HEAVY),
            "problems": self.problems(),
        }


def tier_of(action_type: str) -> str:
    try:
        return ACTION_TIERS[action_type]
    except KeyError:
        raise RoutingError(f"Unknown action type: {action_type[:60]}") from None


def route_for(policy: RoutingPolicy, action_type: str, default: str) -> Route:
    """The route of a single model call outside an agent run (for example a checklist proposal)."""
    tier = tier_of(action_type)
    if not policy.enabled:
        return Route(default, None, USER_SELECTED, "")
    return Route(policy.deployment_for(tier, default), tier, POLICY, action_type)


def signatures(result: Mapping[str, Any]) -> set[str]:
    """Normalised signatures of the actionable findings and CI failures in a step result.

    Line numbers are left out so the same finding moving in a file is still the same finding;
    findings outside the changed files ("elsewhere") are not actionable and are skipped.
    """
    found: set[str] = set()

    def walk(node: Mapping[str, Any], depth: int) -> None:
        for key, value in node.items():
            if key == "findings" and isinstance(value, list):
                for f in value:
                    if isinstance(f, Mapping) and f.get("code") and f.get("scope") != "elsewhere":
                        found.add(f"static:{f.get('tool', '')}:{f.get('file', '')}:{f['code']}")
            elif key in ("actionable_failures", "new") and isinstance(value, list):
                for f in value:
                    if isinstance(f, Mapping):
                        name = f.get("signature") or f.get("test") or f.get("name")
                        if name:
                            found.add(f"ci:{name}:{f.get('file', '')}:{f.get('category', '')}")
            elif isinstance(value, Mapping) and depth < 4:
                walk(value, depth + 1)

    walk(result, 0)
    return found


def next_action_type(
    action: str,
    error_code: str | None,
    sigs: set[str],
    *,
    changed: bool,
) -> str:
    """The kind of the next model call, from what the last step did."""
    if error_code is not None:
        if error_code in INVALID_CODES or error_code in _FORMAT_ERRORS | _NEUTRAL_ERRORS:
            return SIMPLE_TOOL_CALL
        return SELF_REVIEW if action == "finish" else REPAIR
    if any(s.startswith("ci:") for s in sigs):
        return CI_DIAGNOSIS
    if sigs:
        codes = {s.rsplit(":", 1)[-1] for s in sigs}
        return FORMATTING if codes <= _FORMATTING_CODES else STATIC_DIAGNOSIS
    if action in _CHECKS:
        return SELF_REVIEW if changed else FILE_SELECTION
    if action in _EDITS:
        return SHORT_EDIT
    if action in ("list_files", "search_code", "search_documents", "search_repository"):
        return FILE_SELECTION
    return SIMPLE_TOOL_CALL


class RoutingState:
    """The routing of one run: escalation, recorded usage and budgets. One run is one attempt,
    so an escalation lasts until the run ends."""

    def __init__(self, policy: RoutingPolicy, default: str) -> None:
        self.policy = policy
        self.default = default
        self.escalation: str | None = None
        self.invalid_streak = 0
        self.seen: Counter[str] = Counter()
        self.next_type = FILE_SELECTION
        self.heavy_calls = 0
        self.tokens = 0
        self.cost = 0.0

    def route(self) -> Route:
        if not self.policy.enabled:
            return Route(self.default, None, USER_SELECTED, "")
        if self.escalation is not None:
            deployment = self.policy.deployment_for(HEAVY, self.default)
            return Route(deployment, HEAVY, ESCALATION, self.escalation)
        tier = tier_of(self.next_type)
        return Route(self.policy.deployment_for(tier, self.default), tier, POLICY, self.next_type)

    def escalate(self, trigger: str) -> bool:
        """True when this call moved the run to the heavy deployment (false: already there/off)."""
        if not self.policy.enabled or self.escalation is not None:
            return False
        self.escalation = trigger
        return True

    def can_escalate(self, route: Route) -> bool:
        return self.policy.enabled and self.escalation is None and route.tier == LIGHT

    def budget_stop(self, route: Route) -> str | None:
        p = self.policy
        if p.token_limit and self.tokens >= p.token_limit:
            return TOKEN_LIMIT
        if p.cost_limit and self.cost >= p.cost_limit:
            return COST_LIMIT
        if p.heavy_call_limit and route.heavy and self.heavy_calls >= p.heavy_call_limit:
            return HEAVY_CALL_LIMIT
        return None

    def record(self, route: Route, input_tokens: int, output_tokens: int) -> float:
        cost = self.policy.cost(route.deployment, input_tokens, output_tokens)
        self.tokens += input_tokens + output_tokens
        self.cost += cost
        if route.heavy:
            self.heavy_calls += 1
        return cost

    def observe(
        self, action: str, error_code: str | None, result: Mapping[str, Any], *, changed: bool
    ) -> None:
        """What a step showed: updates the escalation triggers and the next call's type."""
        self.invalid_streak = self.invalid_streak + 1 if error_code in INVALID_CODES else 0
        if self.invalid_streak >= INVALID_ACTIONS_LIMIT:
            self.escalate(INVALID_ACTIONS)
        sigs = signatures(result)
        for sig in sigs:
            self.seen[sig] += 1
            if self.seen[sig] >= REPEAT_SIGNATURE_LIMIT:
                self.escalate(REPEATED_FAILURE)
        self.next_type = next_action_type(action, error_code, sigs, changed=changed)
