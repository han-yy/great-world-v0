"""Epistemic boundary between world events and an agent's private cognition.

The objects in this module are *derived records*.  They are deliberately not
world events and must never be used as authoritative world state.  A caller
may persist them in an agent-owned store, but the source event ledger remains
the only truth layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence


class EventLike(Protocol):
    """Small structural interface accepted from ``world.models`` events."""

    event_id: str
    event_type: str
    payload: Mapping[str, Any]


def _get(value: object, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _clamp_confidence(value: float) -> float:
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return numeric


def _freeze(value: Any) -> Any:
    """Make cognitive payloads immutable without importing kernel internals."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{sha256(material.encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True)
class ProvenanceRef:
    """A traceable link between adjacent epistemic layers."""

    source_kind: str
    source_id: str
    relation: str


@dataclass(frozen=True)
class PerceptionRecord:
    """What one observer experienced from one truth-layer event."""

    perception_id: str
    observer_id: str
    source_event_id: str
    perceived_type: str
    details: Mapping[str, Any]
    confidence: float
    observed_at: str | int | float | None = None
    source_seq: int | None = None
    epistemic_layer: str = field(default="perception", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _clamp_confidence(self.confidence))
        object.__setattr__(self, "details", _freeze(dict(self.details)))


@dataclass(frozen=True)
class BeliefRecord:
    """A revisable proposition held by one entity, never a truth assertion."""

    belief_id: str
    holder_id: str
    subject_id: str | None
    predicate: str
    object_value: Any
    confidence: float
    provenance: tuple[ProvenanceRef, ...]
    formed_at: str | int | float | None = None
    epistemic_layer: str = field(default="belief", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _clamp_confidence(self.confidence))
        object.__setattr__(self, "object_value", _freeze(self.object_value))
        object.__setattr__(self, "provenance", tuple(self.provenance))


@dataclass(frozen=True)
class MemoryRecord:
    """An agent-owned encoding of a belief or experience."""

    memory_id: str
    owner_id: str
    memory_type: str
    content: Mapping[str, Any]
    confidence: float
    provenance: tuple[ProvenanceRef, ...]
    encoded_at: str | int | float | None = None
    salience: float = 0.5
    epistemic_layer: str = field(default="memory", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _clamp_confidence(self.confidence))
        object.__setattr__(self, "salience", _clamp_confidence(self.salience))
        object.__setattr__(self, "content", _freeze(dict(self.content)))
        object.__setattr__(self, "provenance", tuple(self.provenance))


Projection = Callable[[object, str], Mapping[str, Any]]


_SPEECH_EVENT_TYPES = frozenset(
    {
        "speech",
        "spoke",
        "say",
        "said",
        "message",
        "utterance",
        "agent.spoke",
        "speech.uttered",
    }
)
_NON_PERCEPTUAL_KEYS = frozenset(
    {
        "hidden",
        "internal",
        "private",
        "secret",
        "truth_only",
        "latent",
        "observable_by",
        "observers",
        "private_to",
    }
)


def _is_speech(event_type: str) -> bool:
    lowered = event_type.casefold()
    return lowered in _SPEECH_EVENT_TYPES or lowered.endswith((".speech", ".spoke"))


def _observer_allowed(event: object, observer_id: str) -> bool:
    payload = _get(event, "payload", default={}) or {}
    if not isinstance(payload, Mapping):
        return True
    allowed = payload.get("observable_by", payload.get("observers"))
    if allowed is not None:
        if isinstance(allowed, str):
            return allowed == "*" or allowed == observer_id
        return observer_id in allowed
    private_to = payload.get("private_to")
    if private_to is not None:
        if isinstance(private_to, str):
            return private_to == observer_id
        return observer_id in private_to
    return True


def _default_projection(event: object, observer_id: str) -> Mapping[str, Any]:
    event_type = str(_get(event, "event_type", "type", default="unknown"))
    payload = _get(event, "payload", default={}) or {}
    if not isinstance(payload, Mapping):
        payload = {"value": payload}

    if _is_speech(event_type):
        speaker_id = _get(event, "actor_id", default=None) or payload.get(
            "speaker_id", payload.get("actor_id")
        )
        utterance = payload.get(
            "utterance", payload.get("text", payload.get("content", payload.get("message", "")))
        )
        # Crucial epistemic rule: the observer perceived a speech act.  The
        # utterance is not unpacked into alleged facts here.
        return {
            "speaker_id": speaker_id,
            "utterance": str(utterance),
            "channel": payload.get("channel", "local"),
        }

    explicitly_projected = payload.get("percept", payload.get("public"))
    if isinstance(explicitly_projected, Mapping):
        return dict(explicitly_projected)
    return {key: value for key, value in payload.items() if key not in _NON_PERCEPTUAL_KEYS}


def perceive_event(
    event: EventLike | Mapping[str, Any] | object,
    observer_id: str,
    *,
    observable: bool = True,
    confidence: float = 1.0,
    observed_at: str | int | float | None = None,
    projection: Projection | None = None,
) -> PerceptionRecord | None:
    """Project one ledger event into one observer's experience.

    ``observable`` should be decided by the kernel's spatial/sensory rules.
    The payload-level audience check is a second guard, not a replacement for
    those rules.  Returning ``None`` means the observer experienced nothing.
    """

    if not observable or not _observer_allowed(event, observer_id):
        return None

    event_id = str(_get(event, "event_id", "id", default=""))
    if not event_id:
        raise ValueError("a source event_id is required for perception provenance")
    event_type = str(_get(event, "event_type", "type", default="unknown"))
    details = (projection or _default_projection)(event, observer_id)
    if not isinstance(details, Mapping):
        raise TypeError("a perception projection must return a mapping")
    perceived_type = "heard_speech" if _is_speech(event_type) else event_type
    timestamp = observed_at
    if timestamp is None:
        timestamp = _get(event, "occurred_at", "timestamp", "tick", default=None)
    seq = _get(event, "seq", default=None)
    return PerceptionRecord(
        perception_id=_stable_id("perception", event_id, observer_id),
        observer_id=observer_id,
        source_event_id=event_id,
        perceived_type=perceived_type,
        details=details,
        confidence=confidence,
        observed_at=timestamp,
        source_seq=int(seq) if seq is not None else None,
    )


def beliefs_from_perception(perception: PerceptionRecord) -> tuple[BeliefRecord, ...]:
    """Create conservative beliefs from an experience.

    Speech yields only ``speaker said utterance``.  In particular, an
    utterance such as "the door is open" does *not* create an ``open(door)``
    belief.  A later reasoning system may consider the report, with this
    provenance and an explicitly adjusted confidence.
    """

    provenance = (
        ProvenanceRef("perception", perception.perception_id, "derived_from"),
        ProvenanceRef("world_event", perception.source_event_id, "perceived_from"),
    )
    if perception.perceived_type == "heard_speech":
        speaker_id = perception.details.get("speaker_id")
        utterance = str(perception.details.get("utterance", ""))
        return (
            BeliefRecord(
                belief_id=_stable_id("belief", perception.perception_id, "said"),
                holder_id=perception.observer_id,
                subject_id=str(speaker_id) if speaker_id is not None else None,
                predicate="said",
                object_value=utterance,
                confidence=perception.confidence,
                provenance=provenance,
                formed_at=perception.observed_at,
            ),
        )

    actor_id = perception.details.get("actor_id")
    return (
        BeliefRecord(
            belief_id=_stable_id("belief", perception.perception_id, "observed"),
            holder_id=perception.observer_id,
            subject_id=str(actor_id) if actor_id is not None else None,
            predicate=f"observed:{perception.perceived_type}",
            object_value=dict(perception.details),
            confidence=perception.confidence,
            provenance=provenance,
            formed_at=perception.observed_at,
        ),
    )


def memory_from_belief(
    belief: BeliefRecord,
    *,
    encoded_at: str | int | float | None = None,
    salience: float = 0.5,
) -> MemoryRecord:
    """Encode a belief as memory while preserving its uncertainty trail."""

    provenance = (
        ProvenanceRef("belief", belief.belief_id, "encoded_from"),
        *belief.provenance,
    )
    return MemoryRecord(
        memory_id=_stable_id("memory", belief.belief_id),
        owner_id=belief.holder_id,
        memory_type="episodic",
        content={
            "subject_id": belief.subject_id,
            "predicate": belief.predicate,
            "object": belief.object_value,
        },
        confidence=belief.confidence,
        provenance=provenance,
        encoded_at=encoded_at if encoded_at is not None else belief.formed_at,
        salience=salience,
    )


@dataclass(frozen=True)
class CognitiveUpdate:
    perception: PerceptionRecord | None
    beliefs: tuple[BeliefRecord, ...] = ()
    memories: tuple[MemoryRecord, ...] = ()


class PerceptionBeliefMemoryPipeline:
    """Stateless convenience pipeline; it never writes kernel state."""

    def process(
        self,
        event: EventLike | Mapping[str, Any] | object,
        observer_id: str,
        *,
        observable: bool = True,
        confidence: float = 1.0,
        salience: float = 0.5,
        projection: Projection | None = None,
    ) -> CognitiveUpdate:
        perception = perceive_event(
            event,
            observer_id,
            observable=observable,
            confidence=confidence,
            projection=projection,
        )
        if perception is None:
            return CognitiveUpdate(None)
        beliefs = beliefs_from_perception(perception)
        memories = tuple(memory_from_belief(item, salience=salience) for item in beliefs)
        return CognitiveUpdate(perception, beliefs, memories)


def merge_belief_evidence(
    beliefs: Sequence[BeliefRecord],
    *,
    confidence: float | None = None,
) -> BeliefRecord:
    """Combine evidence for exactly the same proposition, without truth promotion."""

    if not beliefs:
        raise ValueError("at least one belief is required")
    first = beliefs[0]
    proposition = (first.holder_id, first.subject_id, first.predicate, first.object_value)
    for belief in beliefs[1:]:
        if (belief.holder_id, belief.subject_id, belief.predicate, belief.object_value) != proposition:
            raise ValueError("only identical propositions can merge")
    deduplicated: dict[tuple[str, str, str], ProvenanceRef] = {}
    for belief in beliefs:
        for source in belief.provenance:
            deduplicated[(source.source_kind, source.source_id, source.relation)] = source
    selected_confidence = (
        max(item.confidence for item in beliefs) if confidence is None else _clamp_confidence(confidence)
    )
    return BeliefRecord(
        belief_id=_stable_id("belief", *(item.belief_id for item in beliefs)),
        holder_id=first.holder_id,
        subject_id=first.subject_id,
        predicate=first.predicate,
        object_value=first.object_value,
        confidence=selected_confidence,
        provenance=tuple(deduplicated.values()),
        formed_at=first.formed_at,
    )
