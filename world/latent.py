"""Deterministic, wish-independent resolution of latent world facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


class LatentContextError(ValueError):
    """Raised when non-exploration intent leaks into latent resolution."""


def _contains_forbidden_key(key: object) -> bool:
    normalized = "".join(character for character in str(key).casefold() if character.isalnum())
    return "wish" in normalized or "goal" in normalized


def _validate_context(value: Any, path: str = "context") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _contains_forbidden_key(key):
                raise LatentContextError(
                    f"latent exploration context cannot contain wish/goal field: {path}.{key}"
                )
            _validate_context(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _validate_context(item, f"{path}[{index}]")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(item) for item in value), key=repr)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"latent context/value is not JSON-compatible: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(*parts: object) -> str:
    return sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExplorationContext:
    """Curated context constructed from kernel-observed exploration only."""

    exploration_id: str
    observed_seq: int
    actor_id: str | None = None
    location_id: str | None = None
    method: str = "observe"
    parameters: Mapping[str, Any] = field(default_factory=dict)
    source: str = field(default="kernel", init=False)

    def __post_init__(self) -> None:
        if not self.exploration_id:
            raise LatentContextError("exploration_id is required")
        if self.observed_seq < 0:
            raise LatentContextError("observed_seq cannot be negative")
        if not self.method:
            raise LatentContextError("exploration method is required")
        _validate_context(self.parameters)
        object.__setattr__(self, "parameters", _freeze(dict(self.parameters)))

    @classmethod
    def from_kernel(
        cls,
        *,
        exploration_id: str,
        observed_seq: int,
        actor_id: str | None = None,
        location_id: str | None = None,
        method: str = "observe",
        parameters: Mapping[str, Any] | None = None,
    ) -> "ExplorationContext":
        return cls(
            exploration_id=exploration_id,
            observed_seq=observed_seq,
            actor_id=actor_id,
            location_id=location_id,
            method=method,
            parameters=parameters or {},
        )

    def canonical_fields(self) -> Mapping[str, Any]:
        # Request identity and ledger position are provenance/concurrency data,
        # not causes of the hidden fact. Including either would let an
        # unrelated wish or utterance change a later discovery merely by
        # advancing the event sequence.
        fields = {
            "source": self.source,
            "actor_id": self.actor_id,
            "location_id": self.location_id,
            "method": self.method,
            "parameters": self.parameters,
        }
        _validate_context(fields)
        return _freeze(fields)


@dataclass(frozen=True)
class LatentFact:
    """Payload-compatible frozen latent fact for ``latent.fact_frozen``."""

    fact_id: str
    key: str
    value: Any
    scope: str
    exploration_context_hash: str
    determinism_key: str

    def __post_init__(self) -> None:
        if not self.fact_id or not self.key or not self.scope:
            raise ValueError("fact_id, key, and scope are required")
        _canonical_json(self.value)  # Validate before freezing.
        object.__setattr__(self, "value", _freeze(self.value))

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "key": self.key,
            "value": _plain(self.value),
            "scope": self.scope,
            "exploration_context_hash": self.exploration_context_hash,
            "determinism_key": self.determinism_key,
        }

    def to_action_parameters(self) -> dict[str, Any]:
        """Parameters accepted by the kernel's ``latent.freeze_fact`` action.

        The kernel independently computes ``determinism_key`` from its own
        authoritative seed, so a controller must not supply that field.
        """

        return {
            "fact_id": self.fact_id,
            "key": self.key,
            "value": _plain(self.value),
            "scope": self.scope,
            "exploration_context_hash": self.exploration_context_hash,
        }


ValueFactory = Callable[[str, Mapping[str, Any]], Any]


def _default_value_factory(determinism_key: str, _: Mapping[str, Any]) -> Mapping[str, Any]:
    """Domain-neutral value; game-specific kernels may inject a typed factory."""

    return {
        "variant": int(determinism_key[:12], 16) % 1024,
        "signature": determinism_key[:16],
    }


class LatentRealityResolver:
    """Resolve hidden facts from a fixed seed, then freeze the first result.

    ``resolve`` is suitable for a small in-process v0.  For strict event-store
    integration, call ``preview_resolution``, commit its payload through the
    kernel, then call ``record_frozen`` while replaying that event.
    """

    def __init__(
        self,
        world_seed: str | int,
        *,
        value_factory: ValueFactory | None = None,
        frozen_facts: Iterable[LatentFact] = (),
    ) -> None:
        seed = str(world_seed)
        if not seed:
            raise ValueError("world_seed cannot be empty")
        self._world_seed = seed
        self._seed_commitment = _digest("world-seed-v1", seed)
        self._value_factory = value_factory or _default_value_factory
        self._frozen: dict[tuple[str, str], LatentFact] = {}
        self._lock = RLock()
        for fact in frozen_facts:
            self.record_frozen(fact)

    @property
    def seed_commitment(self) -> str:
        """Public proof that the resolver's seed is fixed, without exposing it."""

        return self._seed_commitment

    @staticmethod
    def _normalize_context(
        context: ExplorationContext | Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if isinstance(context, ExplorationContext):
            return context.canonical_fields()
        if not isinstance(context, Mapping):
            raise TypeError("exploration context must be ExplorationContext or mapping")
        if context.get("source") != "kernel":
            raise LatentContextError("mapping exploration context must declare source='kernel'")
        _validate_context(context)
        return _freeze(dict(context))

    def preview_resolution(
        self,
        key: str,
        context: ExplorationContext | Mapping[str, Any],
        *,
        scope: str = "world",
    ) -> LatentFact:
        """Pure deterministic candidate; this method does not freeze anything."""

        if not key:
            raise ValueError("latent key is required")
        if not scope:
            raise ValueError("latent scope is required")
        normalized = self._normalize_context(context)
        context_hash = _digest("exploration-context-v1", _canonical_json(normalized))
        # This byte-for-byte formula is shared with WorldKernel, which
        # independently derives the value rather than trusting a proposal.
        determination_material = "%s\0%s\0%s\0%s" % (
            self._world_seed,
            scope,
            key,
            context_hash,
        )
        determinism_key = sha256(determination_material.encode("utf-8")).hexdigest()
        value = self._value_factory(determinism_key, normalized)
        _canonical_json(value)
        return LatentFact(
            fact_id=f"latent_{determinism_key[:24]}",
            key=key,
            value=value,
            scope=scope,
            exploration_context_hash=context_hash,
            determinism_key=determinism_key,
        )

    def propose_resolution(
        self,
        key: str,
        context: ExplorationContext | Mapping[str, Any],
        *,
        scope: str = "world",
    ) -> LatentFact:
        """Return a committed fact if known, otherwise a non-mutating candidate."""

        with self._lock:
            existing = self._frozen.get((scope, key))
            if existing is not None:
                return existing
        return self.preview_resolution(key, context, scope=scope)

    def resolve(
        self,
        key: str,
        context: ExplorationContext | Mapping[str, Any],
        *,
        scope: str = "world",
    ) -> LatentFact:
        """Return the already frozen fact or atomically freeze the first candidate."""

        identity = (scope, key)
        with self._lock:
            existing = self._frozen.get(identity)
            if existing is not None:
                return existing
            candidate = self.propose_resolution(key, context, scope=scope)
            self._frozen[identity] = candidate
            return candidate

    def record_frozen(self, fact: LatentFact) -> LatentFact:
        """Hydrate/replay one committed ``latent.fact_frozen`` event."""

        identity = (fact.scope, fact.key)
        with self._lock:
            existing = self._frozen.get(identity)
            if existing is not None and existing != fact:
                raise ValueError(f"conflicting frozen latent fact for {fact.scope}:{fact.key}")
            self._frozen[identity] = fact
            return fact

    def get_frozen(self, key: str, *, scope: str = "world") -> LatentFact | None:
        with self._lock:
            return self._frozen.get((scope, key))

    def frozen_facts(self) -> tuple[LatentFact, ...]:
        with self._lock:
            return tuple(self._frozen[identity] for identity in sorted(self._frozen))
