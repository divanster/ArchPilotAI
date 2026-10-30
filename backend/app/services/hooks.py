"""Hooks of the agent loop: small checks that run before or after an action.

A hook has a point, an optional tool filter, an order and returns allow / block / annotate. A
blocking hook gives the model a short reason as an error step; an annotation is attached to the
step. Hooks are plain functions of the run's state: they never call the model.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

Point = Literal["before_action", "after_action", "before_finish"]
Verdict = Literal["allow", "block", "annotate"]

MAX_REASON_CHARS = 300


@dataclass(frozen=True)
class HookResult:
    verdict: Verdict = "allow"
    reason: str = ""  # short and safe to show to the model; never file content
    code: str | None = None  # error code of a block
    data: dict[str, Any] = field(default_factory=dict)  # extra fields for the step result


ALLOW = HookResult()


def block(code: str, reason: str, **data: Any) -> HookResult:
    return HookResult("block", reason, code, data)


def annotate(reason: str, **data: Any) -> HookResult:
    return HookResult("annotate", reason, None, data)


@dataclass(frozen=True)
class Hook:
    name: str
    point: Point
    run: Callable[[Any], HookResult]  # receives the HookInput of the registry caller
    description: str = ""
    tools: frozenset[str] | None = None  # None: every tool
    order: int = 100
    required: bool = False  # cannot be disabled for a project

    def applies(self, point: str, tool: str) -> bool:
        return self.point == point and (self.tools is None or tool in self.tools)


@dataclass
class HookOutcome:
    blocked: HookResult | None = None
    log: list[dict[str, str]] = field(default_factory=list)
    attach: dict[str, Any] = field(default_factory=dict)


class HookRegistry:
    def __init__(self, hooks: Iterable[Hook] = ()) -> None:
        self._hooks: dict[str, Hook] = {}
        for hook in hooks:
            self.register(hook)

    def register(self, hook: Hook) -> None:
        if hook.name in self._hooks:
            raise ValueError(f"Duplicate hook {hook.name!r}")
        self._hooks[hook.name] = hook

    def get(self, name: str) -> Hook | None:
        return self._hooks.get(name)

    def all(self) -> list[Hook]:
        return sorted(self._hooks.values(), key=lambda h: (h.point, h.order, h.name))

    def active(self, point: str, tool: str, disabled: frozenset[str] = frozenset()) -> list[Hook]:
        """The hooks to run, in order. A required hook is active whatever `disabled` says."""
        found = [
            h
            for h in self._hooks.values()
            if h.applies(point, tool) and (h.required or h.name not in disabled)
        ]
        return sorted(found, key=lambda h: (h.order, h.name))

    def run(
        self, point: Point, tool: str, ctx: Any, disabled: frozenset[str] = frozenset()
    ) -> HookOutcome:
        """Run the hooks of a point in order; the first block ends the chain."""
        outcome = HookOutcome()
        for hook in self.active(point, tool, disabled):
            result = hook.run(ctx)
            reason = result.reason[:MAX_REASON_CHARS]
            outcome.log.append(
                {"hook": hook.name, "point": point, "result": result.verdict, "reason": reason}
            )
            if result.verdict == "block":
                outcome.blocked = result
                break
            if result.verdict == "annotate":
                outcome.attach.update(result.data)
        return outcome
