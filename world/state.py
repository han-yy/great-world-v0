"""Pure event reducers and replayable world-state projections."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple

from .models import (
    ACTIVITY_PERFORMED,
    CAPABILITY_UNLOCKED,
    CHILD_GOAL_SELECTED,
    ENTITY_CREATED,
    ENTITY_MOVED,
    GENESIS_HASH,
    LATENT_FACT_FROZEN,
    SPEECH_UTTERED,
    WISH_SUBMITTED,
    WORLD_CREATED,
    Entity,
    WorldEvent,
    thaw,
)


class StateTransitionError(RuntimeError):
    pass


def _mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


def _capability_mapping(
    values: Mapping[str, Iterable[str]]
) -> Mapping[str, FrozenSet[str]]:
    return MappingProxyType(
        {entity_id: frozenset(capabilities) for entity_id, capabilities in values.items()}
    )


@dataclass(frozen=True)
class WorldState:
    """A derived snapshot; the event ledger remains the source of truth."""

    world_id: str
    seq: int = 0
    seed: Optional[str] = None
    name: Optional[str] = None
    metadata: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    entities: Mapping[str, Entity] = field(
        default_factory=lambda: MappingProxyType({})
    )
    utterance_ids: Tuple[str, ...] = ()
    activity_ids: Tuple[str, ...] = ()
    wish_ids: Tuple[str, ...] = ()
    child_goals: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    capabilities: Mapping[str, FrozenSet[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    latent_facts: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    applied_event_ids: FrozenSet[str] = frozenset()

    def entity(self, entity_id: str) -> Entity:
        try:
            return self.entities[entity_id]
        except KeyError as exc:
            raise StateTransitionError("entity not found: %s" % entity_id) from exc

    def is_agent(self, entity_id: str) -> bool:
        entity = self.entities.get(entity_id)
        return bool(entity and entity.is_agent)

    def capabilities_for(self, entity_id: str) -> FrozenSet[str]:
        return self.capabilities.get(entity_id, frozenset())

    def active_goal_for(self, child_id: str) -> Optional[Entity]:
        goal_id = self.child_goals.get(child_id)
        return self.entities.get(goal_id) if goal_id else None

    def latent_fact_for(self, semantic_key: str) -> Optional[Entity]:
        """Return the once-frozen fact for a stable seed/scope/key identity."""

        fact_id = self.latent_facts.get(semantic_key)
        return self.entities.get(fact_id) if fact_id else None


def _check_payload(
    event: WorldEvent,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    actual = set(event.payload.keys())
    missing = required_set - actual
    extra = actual - allowed
    if missing or extra:
        parts = []
        if missing:
            parts.append("missing %s" % sorted(missing))
        if extra:
            parts.append("unexpected %s" % sorted(extra))
        raise StateTransitionError(
            "%s payload invalid: %s" % (event.event_type, "; ".join(parts))
        )


def reduce_event(state: WorldState, event: WorldEvent) -> WorldState:
    """Apply exactly one event and return a new immutable projection."""

    if event.seq != state.seq + 1:
        raise StateTransitionError(
            "expected seq %d, got %d" % (state.seq + 1, event.seq)
        )
    if event.event_id in state.applied_event_ids:
        raise StateTransitionError("event applied twice: %s" % event.event_id)

    entities: Dict[str, Entity] = dict(state.entities)
    utterance_ids = state.utterance_ids
    activity_ids = state.activity_ids
    wish_ids = state.wish_ids
    child_goals = dict(state.child_goals)
    capabilities = {
        entity_id: set(values) for entity_id, values in state.capabilities.items()
    }
    latent_facts = dict(state.latent_facts)
    seed = state.seed
    name = state.name
    metadata = dict(state.metadata)

    if event.event_type == WORLD_CREATED:
        _check_payload(event, {"world_id", "name", "seed", "metadata"})
        if state.seq != 0 or entities:
            raise StateTransitionError("world.created must be the first event")
        seed = str(event.payload["seed"])
        name = str(event.payload["name"])
        metadata = thaw(event.payload["metadata"])
        # A fork is a distinct logical world even though its first event belongs
        # to the parent stream, so project the world entity using state.world_id.
        entities[state.world_id] = Entity(
            id=state.world_id,
            name=name,
            kind="world",
            attributes={"seed": seed, "metadata": metadata},
        )

    elif event.event_type == ENTITY_CREATED:
        _check_payload(event, {"entity"})
        raw_entity = event.payload["entity"]
        if not isinstance(raw_entity, Mapping):
            raise StateTransitionError("entity.created entity must be an object")
        entity = Entity.from_payload(raw_entity)
        if entity.id in entities:
            raise StateTransitionError("entity already exists: %s" % entity.id)
        if entity.location_id is not None and entity.location_id not in entities:
            raise StateTransitionError(
                "entity location does not exist: %s" % entity.location_id
            )
        entities[entity.id] = entity

    elif event.event_type == ENTITY_MOVED:
        _check_payload(
            event, {"entity_id", "from_location_id", "to_location_id"}
        )
        entity_id = str(event.payload["entity_id"])
        entity = entities.get(entity_id)
        if entity is None:
            raise StateTransitionError("cannot move missing entity: %s" % entity_id)
        from_location_id = event.payload["from_location_id"]
        to_location_id = event.payload["to_location_id"]
        if entity.location_id != from_location_id:
            raise StateTransitionError(
                "move origin mismatch for %s: state=%r event=%r"
                % (entity_id, entity.location_id, from_location_id)
            )
        if to_location_id not in entities:
            raise StateTransitionError(
                "move destination does not exist: %s" % to_location_id
            )
        entities[entity_id] = replace(entity, location_id=str(to_location_id))

    elif event.event_type == SPEECH_UTTERED:
        _check_payload(
            event,
            {"utterance_id", "speaker_id", "text", "target_ids", "location_id"},
        )
        utterance_id = str(event.payload["utterance_id"])
        speaker_id = str(event.payload["speaker_id"])
        speaker = entities.get(speaker_id)
        if speaker is None or not speaker.is_agent:
            raise StateTransitionError("speaker is not an agent: %s" % speaker_id)
        if utterance_id in entities:
            raise StateTransitionError("utterance already exists: %s" % utterance_id)
        target_ids = tuple(str(value) for value in event.payload["target_ids"])
        missing_targets = [value for value in target_ids if value not in entities]
        if missing_targets:
            raise StateTransitionError(
                "speech targets do not exist: %s" % missing_targets
            )
        location_id = event.payload["location_id"]
        if location_id is not None and location_id not in entities:
            raise StateTransitionError(
                "speech location does not exist: %s" % location_id
            )
        entities[utterance_id] = Entity(
            id=utterance_id,
            name="Utterance by %s" % speaker.name,
            kind="utterance",
            location_id=location_id,
            attributes={
                "speaker_id": speaker_id,
                "text": event.payload["text"],
                "target_ids": target_ids,
            },
        )
        utterance_ids = utterance_ids + (utterance_id,)

    elif event.event_type == ACTIVITY_PERFORMED:
        _check_payload(
            event,
            {"activity_id", "actor_id", "description", "target_ids", "location_id"},
        )
        activity_id = str(event.payload["activity_id"])
        actor_id = str(event.payload["actor_id"])
        actor = entities.get(actor_id)
        if actor is None or not actor.is_agent:
            raise StateTransitionError("activity actor is not an agent: %s" % actor_id)
        if activity_id in entities:
            raise StateTransitionError("activity already exists: %s" % activity_id)
        target_ids = tuple(str(value) for value in event.payload["target_ids"])
        missing_targets = [value for value in target_ids if value not in entities]
        if missing_targets:
            raise StateTransitionError(
                "activity targets do not exist: %s" % missing_targets
            )
        location_id = event.payload["location_id"]
        if location_id is not None and location_id not in entities:
            raise StateTransitionError(
                "activity location does not exist: %s" % location_id
            )
        entities[activity_id] = Entity(
            id=activity_id,
            name=str(event.payload["description"])[:80],
            kind="activity",
            location_id=location_id,
            attributes={
                "actor_id": actor_id,
                "description": event.payload["description"],
                "target_ids": target_ids,
            },
        )
        activity_ids = activity_ids + (activity_id,)

    elif event.event_type == WISH_SUBMITTED:
        _check_payload(event, {"wish_id", "submitted_by", "text"})
        wish_id = str(event.payload["wish_id"])
        submitted_by = str(event.payload["submitted_by"])
        submitter = entities.get(submitted_by)
        if submitter is None or not submitter.is_agent:
            raise StateTransitionError("wish submitter is not an agent: %s" % submitted_by)
        if wish_id in entities:
            raise StateTransitionError("wish already exists: %s" % wish_id)
        text = str(event.payload["text"])
        entities[wish_id] = Entity(
            id=wish_id,
            name=text[:80],
            kind="wish",
            attributes={"text": text, "submitted_by": submitted_by},
        )
        wish_ids = wish_ids + (wish_id,)

    elif event.event_type == CHILD_GOAL_SELECTED:
        _check_payload(
            event,
            {"goal_id", "child_id", "description", "source_wish_ids", "rationale"},
        )
        goal_id = str(event.payload["goal_id"])
        child_id = str(event.payload["child_id"])
        child = entities.get(child_id)
        if child is None or child.kind != "child" or not child.is_agent:
            raise StateTransitionError("goal owner is not a child agent: %s" % child_id)
        if goal_id in entities:
            raise StateTransitionError("goal already exists: %s" % goal_id)
        source_wish_ids = tuple(
            str(value) for value in event.payload["source_wish_ids"]
        )
        for wish_id in source_wish_ids:
            wish = entities.get(wish_id)
            if wish is None or wish.kind != "wish":
                raise StateTransitionError("goal source is not a wish: %s" % wish_id)
        description = str(event.payload["description"])
        entities[goal_id] = Entity(
            id=goal_id,
            name=description[:80],
            kind="goal",
            attributes={
                "description": description,
                "child_id": child_id,
                "source_wish_ids": source_wish_ids,
                "rationale": event.payload["rationale"],
            },
        )
        child_goals[child_id] = goal_id

    elif event.event_type == CAPABILITY_UNLOCKED:
        _check_payload(
            event,
            {
                "entity_id",
                "capability_id",
                "name",
                "description",
                "prerequisite_ids",
                "evidence_event_ids",
            },
        )
        entity_id = str(event.payload["entity_id"])
        owner = entities.get(entity_id)
        if owner is None or not owner.is_agent:
            raise StateTransitionError(
                "capability owner is not an agent: %s" % entity_id
            )
        capability_id = str(event.payload["capability_id"])
        existing = entities.get(capability_id)
        if existing is not None and existing.kind != "capability":
            raise StateTransitionError(
                "capability id belongs to another entity kind: %s" % capability_id
            )
        prerequisites = frozenset(
            str(value) for value in event.payload["prerequisite_ids"]
        )
        if not prerequisites.issubset(state.capabilities_for(entity_id)):
            raise StateTransitionError(
                "capability prerequisites are not unlocked for %s" % entity_id
            )
        evidence_ids = frozenset(
            str(value) for value in event.payload["evidence_event_ids"]
        )
        if not evidence_ids.issubset(state.applied_event_ids):
            raise StateTransitionError("capability evidence references future events")
        if existing is None:
            entities[capability_id] = Entity(
                id=capability_id,
                name=str(event.payload["name"]),
                kind="capability",
                attributes={
                    "description": event.payload["description"],
                    "prerequisite_ids": tuple(sorted(prerequisites)),
                },
            )
        capabilities.setdefault(entity_id, set()).add(capability_id)

    elif event.event_type == LATENT_FACT_FROZEN:
        _check_payload(
            event,
            {
                "fact_id",
                "key",
                "value",
                "scope",
                "exploration_context_hash",
                "determinism_key",
            },
        )
        fact_id = str(event.payload["fact_id"])
        determinism_key = str(event.payload["determinism_key"])
        if fact_id in entities:
            raise StateTransitionError("latent fact already exists: %s" % fact_id)
        if state.seed is None:
            raise StateTransitionError("cannot freeze latent fact without world seed")
        scope = str(event.payload["scope"])
        key = str(event.payload["key"])
        semantic_key = hashlib.sha256(
            ("%s\0%s\0%s" % (state.seed, scope, key)).encode("utf-8")
        ).hexdigest()
        if semantic_key in latent_facts:
            raise StateTransitionError(
                "latent fact was already frozen for semantic key: %s" % semantic_key
            )
        entities[fact_id] = Entity(
            id=fact_id,
            name=key,
            kind="latent_fact",
            attributes={
                "key": key,
                "value": thaw(event.payload["value"]),
                "scope": scope,
                "exploration_context_hash": event.payload[
                    "exploration_context_hash"
                ],
                "determinism_key": determinism_key,
            },
        )
        latent_facts[semantic_key] = fact_id

    else:
        raise StateTransitionError("no reducer for event type: %s" % event.event_type)

    return WorldState(
        world_id=state.world_id,
        seq=event.seq,
        seed=seed,
        name=name,
        metadata=_mapping(metadata),
        entities=_mapping(entities),
        utterance_ids=utterance_ids,
        activity_ids=activity_ids,
        wish_ids=wish_ids,
        child_goals=_mapping(child_goals),
        capabilities=_capability_mapping(capabilities),
        latent_facts=_mapping(latent_facts),
        applied_event_ids=state.applied_event_ids | {event.event_id},
    )


def replay(world_id: str, events: Iterable[WorldEvent]) -> WorldState:
    """Rebuild a logical world only from its ordered event history."""

    state = WorldState(world_id=world_id)
    previous_hash = GENESIS_HASH
    for event in events:
        if event.prev_hash != previous_hash:
            raise StateTransitionError(
                "hash-chain break at logical seq %d" % (state.seq + 1)
            )
        if not event.verify_hash():
            raise StateTransitionError("invalid event hash: %s" % event.event_id)
        state = reduce_event(state, event)
        previous_hash = event.event_hash
    return state
