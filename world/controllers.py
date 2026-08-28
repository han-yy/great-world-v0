"""Controller boundary: cognition may propose actions, never mutate the world."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, TYPE_CHECKING, runtime_checkable

from .perception import BeliefRecord, MemoryRecord, PerceptionRecord

if TYPE_CHECKING:  # Avoid a hard import cycle with the kernel's model module.
    from .models import ActionProposal


class ControllerUnavailable(RuntimeError):
    """A transient controller failure that must not be mistaken for silence."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


_PRIVATE_REASONING_KEYS = frozenset(
    {
        "analysis",
        "chain_of_thought",
        "chain-of-thought",
        "cot",
        "hidden_reasoning",
        "reasoning",
        "scratchpad",
        "thoughts",
    }
)

_SERIALIZED_PERCEPTION_FIELDS = frozenset(
    {"perception_id", "source_event_id", "perceived_type", "details", "confidence"}
)
_TRUTH_EVENT_ENVELOPE_FIELDS = frozenset(
    {"event_type", "payload", "event_hash", "prev_hash", "world_id"}
)


def _strip_private_reasoning(value: Any) -> Any:
    """Prevent transient model scratch work from entering an action/event payload."""

    if isinstance(value, Mapping):
        return {
            str(key): _strip_private_reasoning(item)
            for key, item in value.items()
            if str(key).casefold() not in _PRIVATE_REASONING_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_strip_private_reasoning(item) for item in value]
    return value


@dataclass(frozen=True)
class DecisionContext:
    """Read-only, agent-relative input passed to every controller type.

    ``world_view`` must already be filtered for this agent.  Truth-layer state
    is intentionally absent.  ``trigger_events`` contains observer-specific
    perceptions, their owner-scoped serialized form, or opaque event ids;
    never raw truth-layer event objects.
    """

    agent_id: str
    batch_id: str
    observed_seq: int
    perceptions: tuple[PerceptionRecord, ...] = ()
    beliefs: tuple[BeliefRecord, ...] = ()
    memories: tuple[MemoryRecord, ...] = ()
    available_actions: tuple[str, ...] = ()
    world_view: Mapping[str, Any] = field(default_factory=dict)
    trigger_events: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not self.agent_id:
            raise ValueError("agent_id is required")
        if not self.batch_id:
            raise ValueError("batch_id is required")
        if self.observed_seq < 0:
            raise ValueError("observed_seq cannot be negative")
        if any(item.observer_id != self.agent_id for item in self.perceptions):
            raise ValueError("all perceptions must belong to the deciding agent")
        if any(item.holder_id != self.agent_id for item in self.beliefs):
            raise ValueError("all beliefs must belong to the deciding agent")
        if any(item.owner_id != self.agent_id for item in self.memories):
            raise ValueError("all memories must belong to the deciding agent")
        object.__setattr__(self, "perceptions", tuple(self.perceptions))
        object.__setattr__(self, "beliefs", tuple(self.beliefs))
        object.__setattr__(self, "memories", tuple(self.memories))
        object.__setattr__(self, "available_actions", tuple(self.available_actions))
        object.__setattr__(self, "world_view", _freeze(dict(self.world_view)))
        triggers: list[Any] = []
        for item in self.trigger_events:
            if isinstance(item, PerceptionRecord):
                if item.observer_id != self.agent_id:
                    raise ValueError("trigger perception belongs to a different observer")
                triggers.append(item)
            elif isinstance(item, Mapping):
                fields = set(item)
                if fields & _TRUTH_EVENT_ENVELOPE_FIELDS:
                    raise TypeError("raw truth-layer events cannot be controller triggers")
                missing = _SERIALIZED_PERCEPTION_FIELDS - fields
                if missing:
                    raise TypeError(
                        f"serialized trigger perception is missing fields: {sorted(missing)}"
                    )
                if "observer_id" in item and item["observer_id"] != self.agent_id:
                    raise ValueError("trigger perception belongs to a different observer")
                if not isinstance(item["details"], Mapping):
                    raise TypeError("serialized perception details must be a mapping")
                triggers.append(_freeze(dict(item)))
            elif isinstance(item, str) and item:
                triggers.append(item)
            else:
                raise TypeError(
                    "trigger_events may contain only perceptions or opaque ids"
                )
        object.__setattr__(self, "trigger_events", tuple(triggers))


@dataclass(frozen=True)
class ActionIntent:
    """Controller-local action shape, normalized into a kernel proposal."""

    action_type: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action_type:
            raise ValueError("action_type is required")
        clean = _strip_private_reasoning(dict(self.parameters))
        object.__setattr__(self, "parameters", _freeze(clean))


Policy = Callable[[DecisionContext], Any]


@runtime_checkable
class Controller(Protocol):
    """Common interface for human, AI, and offline-delegate control."""

    def propose(self, context: DecisionContext) -> "ActionProposal | None":
        """Return at most one proposal and do not mutate world state."""


def _make_proposal(
    context: DecisionContext,
    action_type: str,
    parameters: Mapping[str, Any],
) -> "ActionProposal":
    from .models import ActionProposal

    if context.available_actions and action_type not in context.available_actions:
        raise ValueError(f"action type is not available to {context.agent_id}: {action_type}")
    clean = _strip_private_reasoning(dict(parameters))
    # One observer may emit at most one committed proposal for one bounded
    # batch, including after a process retry.
    proposal_id = "proposal_" + sha256(
        f"{context.agent_id}\0{context.batch_id}".encode("utf-8")
    ).hexdigest()[:32]
    return ActionProposal(
        action_type=action_type,
        actor_id=context.agent_id,
        parameters=clean,
        proposal_id=proposal_id,
        observed_seq=context.observed_seq,
    )


def _normalize_result(context: DecisionContext, result: Any) -> "ActionProposal | None":
    if result is None:
        return None

    from .models import ActionProposal

    if isinstance(result, ActionProposal):
        if result.actor_id != context.agent_id:
            raise ValueError("a controller cannot propose an action for another agent")
        if context.available_actions and result.action_type not in context.available_actions:
            raise ValueError(
                f"action type is not available to {context.agent_id}: {result.action_type}"
            )
        # Rebuild it so private scratch fields cannot pass through in parameters.
        clean = _strip_private_reasoning(dict(result.parameters))
        if clean == dict(result.parameters):
            return result
        return ActionProposal(
            action_type=result.action_type,
            actor_id=result.actor_id,
            parameters=clean,
            proposal_id=result.proposal_id,
            observed_seq=result.observed_seq,
            submitted_at=result.submitted_at,
        )
    if isinstance(result, ActionIntent):
        return _make_proposal(context, result.action_type, result.parameters)
    if isinstance(result, Mapping):
        action_type = result.get("action_type", result.get("type"))
        if not action_type:
            raise ValueError("controller result requires action_type")
        parameters = result.get("parameters", result.get("payload", {}))
        if not isinstance(parameters, Mapping):
            raise TypeError("action parameters must be a mapping")
        return _make_proposal(context, str(action_type), parameters)
    raise TypeError("controller policy must return ActionProposal, ActionIntent, mapping, or None")


class HumanController:
    """Turns explicitly queued player input into proposals."""

    def __init__(self) -> None:
        self._pending: deque[ActionIntent] = deque()
        self._lock = Lock()

    def submit(self, action_type: str, parameters: Mapping[str, Any] | None = None) -> None:
        intent = ActionIntent(action_type, parameters or {})
        with self._lock:
            self._pending.append(intent)

    def propose(self, context: DecisionContext) -> "ActionProposal | None":
        with self._lock:
            if not self._pending:
                return None
            intent = self._pending.popleft()
        return _normalize_result(context, intent)


class ScriptedAIController:
    """Adapter for a deterministic policy or an external LLM client callable."""

    def __init__(self, policy: Policy) -> None:
        if not callable(policy):
            raise TypeError("policy must be callable")
        self._policy = policy

    def propose(self, context: DecisionContext) -> "ActionProposal | None":
        return _normalize_result(context, self._policy(context))


TriggerPredicate = Callable[[DecisionContext], bool]


class DelegateController:
    """Event-driven, lazy offline control with one decision per event batch.

    The delegate stores only bounded batch de-duplication metadata.  It never
    stores prompts, scratchpads, hidden reasoning, or a continuous thought
    stream.  Its policy is not invoked at all for an empty trigger batch.
    """

    def __init__(
        self,
        policy: Policy,
        *,
        should_wake: TriggerPredicate | None = None,
    ) -> None:
        if not callable(policy):
            raise TypeError("policy must be callable")
        self._policy = policy
        self._should_wake = should_wake or (lambda context: bool(context.trigger_events))
        self._processed: set[str] = set()
        self._lock = Lock()

    def _claim_batch(self, batch_id: str) -> bool:
        with self._lock:
            if batch_id in self._processed:
                return False
            self._processed.add(batch_id)
            return True

    def propose(self, context: DecisionContext) -> "ActionProposal | None":
        if not context.trigger_events or not self._should_wake(context):
            return None
        if not self._claim_batch(context.batch_id):
            return None
        return _normalize_result(context, self._policy(context))
