"""Immutable domain values used by the world kernel.

The module deliberately depends only on the Python standard library.  Mutable
input containers are recursively frozen at model boundaries so an event cannot
be changed after its hash has been calculated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4


GENESIS_HASH = "0" * 64

WORLD_CREATED = "world.created"
ENTITY_CREATED = "entity.created"
ENTITY_MOVED = "entity.moved"
ACTIVITY_PERFORMED = "activity.performed"
SPEECH_UTTERED = "speech.uttered"
WISH_SUBMITTED = "wish.submitted"
CHILD_GOAL_SELECTED = "child.goal_selected"
CAPABILITY_UNLOCKED = "capability.unlocked"
LATENT_FACT_FROZEN = "latent.fact_frozen"

SUPPORTED_EVENT_TYPES = frozenset(
    {
        WORLD_CREATED,
        ENTITY_CREATED,
        ENTITY_MOVED,
        ACTIVITY_PERFORMED,
        SPEECH_UTTERED,
        WISH_SUBMITTED,
        CHILD_GOAL_SELECTED,
        CAPABILITY_UNLOCKED,
        LATENT_FACT_FROZEN,
    }
)


class ModelValidationError(ValueError):
    """Raised when a domain value is malformed."""


def utc_now() -> str:
    """Return a stable, UTC, RFC3339-ish timestamp suitable for event records."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid4().hex)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ModelValidationError("object keys must be strings")
        frozen = {key: _freeze(item) for key, item in value.items()}
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Convert a recursively frozen model value to JSON-compatible containers."""

    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((thaw(item) for item in value), key=repr)
    return value


def canonical_json(value: Any) -> str:
    """Serialize a value in the one format used for event hashes."""

    try:
        return json.dumps(
            thaw(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ModelValidationError("value is not canonical-JSON serializable") from exc


def _required_text(value: Any, field_name: str, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError("%s must be a non-empty string" % field_name)
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ModelValidationError("%s is longer than %d characters" % (field_name, max_length))
    return cleaned


@dataclass(frozen=True)
class Entity:
    """The sole persistent object primitive.

    Places, people, wishes, goals, capabilities, utterances, and latent facts
    are all represented as entities.  An entity is an agent if and only if its
    ``policy_id`` is non-empty.
    """

    id: str
    name: str
    kind: str
    policy_id: Optional[str] = None
    location_id: Optional[str] = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_text(self.id, "entity.id"))
        object.__setattr__(self, "name", _required_text(self.name, "entity.name", 512))
        object.__setattr__(self, "kind", _required_text(self.kind, "entity.kind"))
        policy_id = self.policy_id.strip() if isinstance(self.policy_id, str) else None
        object.__setattr__(self, "policy_id", policy_id or None)
        if self.location_id is not None:
            object.__setattr__(
                self,
                "location_id",
                _required_text(self.location_id, "entity.location_id"),
            )
        if not isinstance(self.attributes, Mapping):
            raise ModelValidationError("entity.attributes must be an object")
        object.__setattr__(self, "attributes", _freeze(self.attributes))
        canonical_json(self.attributes)

    @property
    def is_agent(self) -> bool:
        return bool(self.policy_id)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "policy_id": self.policy_id,
            "location_id": self.location_id,
            "attributes": thaw(self.attributes),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Entity":
        return cls(
            id=payload["id"],
            name=payload["name"],
            kind=payload["kind"],
            policy_id=payload.get("policy_id"),
            location_id=payload.get("location_id"),
            attributes=payload.get("attributes", {}),
        )


@dataclass(frozen=True)
class ActionProposal:
    """An untrusted request that must pass through :class:`WorldKernel`."""

    action_type: str
    actor_id: Optional[str]
    parameters: Mapping[str, Any]
    proposal_id: str = field(default_factory=lambda: new_id("proposal"))
    observed_seq: Optional[int] = None
    submitted_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "action_type", _required_text(self.action_type, "action_type")
        )
        if self.actor_id is not None:
            object.__setattr__(self, "actor_id", _required_text(self.actor_id, "actor_id"))
        object.__setattr__(
            self, "proposal_id", _required_text(self.proposal_id, "proposal_id")
        )
        if self.observed_seq is not None and (
            not isinstance(self.observed_seq, int) or self.observed_seq < 0
        ):
            raise ModelValidationError("observed_seq must be a non-negative integer")
        if not isinstance(self.parameters, Mapping):
            raise ModelValidationError("parameters must be an object")
        object.__setattr__(self, "parameters", _freeze(self.parameters))
        canonical_json(self.parameters)


@dataclass(frozen=True)
class EventDraft:
    """A validated event waiting for the store to assign sequence and hashes."""

    event_type: str
    payload: Mapping[str, Any]
    actor_id: Optional[str] = None
    proposal_id: Optional[str] = None

    def __post_init__(self) -> None:
        event_type = _required_text(self.event_type, "event_type")
        if event_type not in SUPPORTED_EVENT_TYPES:
            raise ModelValidationError("unsupported event type: %s" % event_type)
        object.__setattr__(self, "event_type", event_type)
        if self.actor_id is not None:
            object.__setattr__(self, "actor_id", _required_text(self.actor_id, "actor_id"))
        if self.proposal_id is not None:
            object.__setattr__(
                self, "proposal_id", _required_text(self.proposal_id, "proposal_id")
            )
        if not isinstance(self.payload, Mapping):
            raise ModelValidationError("event payload must be an object")
        object.__setattr__(self, "payload", _freeze(self.payload))
        canonical_json(self.payload)


def compute_event_hash(
    *,
    world_id: str,
    seq: int,
    event_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    occurred_at: str,
    actor_id: Optional[str],
    proposal_id: Optional[str],
    prev_hash: str,
) -> str:
    material = {
        "world_id": world_id,
        "seq": seq,
        "event_id": event_id,
        "event_type": event_type,
        "payload": payload,
        "occurred_at": occurred_at,
        "actor_id": actor_id,
        "proposal_id": proposal_id,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorldEvent:
    """An immutable event envelope stored in the append-only ledger."""

    world_id: str
    seq: int
    event_id: str
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: str
    prev_hash: str
    event_hash: str
    actor_id: Optional[str] = None
    proposal_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "world_id", _required_text(self.world_id, "world_id"))
        if not isinstance(self.seq, int) or self.seq < 1:
            raise ModelValidationError("event seq must be a positive integer")
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        event_type = _required_text(self.event_type, "event_type")
        if event_type not in SUPPORTED_EVENT_TYPES:
            raise ModelValidationError("unsupported event type: %s" % event_type)
        object.__setattr__(self, "event_type", event_type)
        if not isinstance(self.payload, Mapping):
            raise ModelValidationError("event payload must be an object")
        object.__setattr__(self, "payload", _freeze(self.payload))
        canonical_json(self.payload)
        if len(self.prev_hash) != 64 or len(self.event_hash) != 64:
            raise ModelValidationError("event hashes must be 64-character SHA-256 hex strings")
        try:
            int(self.prev_hash, 16)
            int(self.event_hash, 16)
        except ValueError as exc:
            raise ModelValidationError("event hashes must be hexadecimal") from exc

    def expected_hash(self) -> str:
        return compute_event_hash(
            world_id=self.world_id,
            seq=self.seq,
            event_id=self.event_id,
            event_type=self.event_type,
            payload=self.payload,
            occurred_at=self.occurred_at,
            actor_id=self.actor_id,
            proposal_id=self.proposal_id,
            prev_hash=self.prev_hash,
        )

    def verify_hash(self) -> bool:
        return self.event_hash == self.expected_hash()


@dataclass(frozen=True)
class WorldRecord:
    world_id: str
    seed: str
    name: str
    created_at: str
    parent_world_id: Optional[str] = None
    fork_seq: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "world_id", _required_text(self.world_id, "world_id"))
        object.__setattr__(self, "seed", _required_text(self.seed, "seed", 4096))
        object.__setattr__(self, "name", _required_text(self.name, "world.name", 512))
        if (self.parent_world_id is None) != (self.fork_seq is None):
            raise ModelValidationError(
                "parent_world_id and fork_seq must either both be set or both be absent"
            )
        if self.parent_world_id is not None:
            object.__setattr__(
                self,
                "parent_world_id",
                _required_text(self.parent_world_id, "parent_world_id"),
            )
            if not isinstance(self.fork_seq, int) or self.fork_seq < 1:
                raise ModelValidationError("fork_seq must be a positive integer")
        if not isinstance(self.metadata, Mapping):
            raise ModelValidationError("world.metadata must be an object")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        canonical_json(self.metadata)
