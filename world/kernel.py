"""Authoritative validation and translation boundary for world mutations."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Optional, Sequence, Set, Tuple

from .event_store import SQLiteEventStore
from .models import (
    CAPABILITY_UNLOCKED,
    CHILD_GOAL_SELECTED,
    ENTITY_CREATED,
    ENTITY_MOVED,
    LATENT_FACT_FROZEN,
    SPEECH_UTTERED,
    WISH_SUBMITTED,
    ActionProposal,
    Entity,
    EventDraft,
    WorldEvent,
    WorldRecord,
    canonical_json,
    new_id,
    thaw,
)
from .state import WorldState, replay


ACTION_CREATE_ENTITY = "entity.create"
ACTION_MOVE_ENTITY = "entity.move"
ACTION_UTTER_SPEECH = "speech.utter"
ACTION_SUBMIT_WISH = "wish.submit"
ACTION_SELECT_CHILD_GOAL = "child.select_goal"
ACTION_UNLOCK_CAPABILITY = "capability.unlock"
ACTION_FREEZE_LATENT_FACT = "latent.freeze_fact"

SUPPORTED_ACTION_TYPES = frozenset(
    {
        ACTION_CREATE_ENTITY,
        ACTION_MOVE_ENTITY,
        ACTION_UTTER_SPEECH,
        ACTION_SUBMIT_WISH,
        ACTION_SELECT_CHILD_GOAL,
        ACTION_UNLOCK_CAPABILITY,
        ACTION_FREEZE_LATENT_FACT,
    }
)

ACTION_EVENT_TYPES = {
    ACTION_CREATE_ENTITY: ENTITY_CREATED,
    ACTION_MOVE_ENTITY: ENTITY_MOVED,
    ACTION_UTTER_SPEECH: SPEECH_UTTERED,
    ACTION_SUBMIT_WISH: WISH_SUBMITTED,
    ACTION_SELECT_CHILD_GOAL: CHILD_GOAL_SELECTED,
    ACTION_UNLOCK_CAPABILITY: CAPABILITY_UNLOCKED,
    ACTION_FREEZE_LATENT_FACT: LATENT_FACT_FROZEN,
}


class KernelError(RuntimeError):
    pass


class ProposalRejected(KernelError):
    pass


class WorldKernel:
    """The only component controllers should use to change world truth.

    Controllers receive ``submit``.  Trusted bootstrap and latent-resolution
    code may deliberately use ``submit_system``; untrusted actor proposals
    cannot omit their actor identity to obtain system authority.
    """

    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    def create_world(
        self,
        world_id: str,
        *,
        seed: str,
        name: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> WorldEvent:
        return self.store.create_world(
            world_id, seed=str(seed), name=name, metadata=metadata
        )

    def fork_world(
        self,
        parent_world_id: str,
        child_world_id: str,
        fork_seq: int,
        *,
        name: Optional[str] = None,
        seed: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> WorldRecord:
        return self.store.fork_world(
            parent_world_id,
            child_world_id,
            fork_seq,
            name=name,
            seed=seed,
            metadata=metadata,
        )

    def get_state(
        self, world_id: str, upto_seq: Optional[int] = None
    ) -> WorldState:
        return replay(world_id, self.store.load_events(world_id, upto_seq))

    # Concise alias for interactive callers.
    state = get_state
    replay = get_state

    def history(
        self, world_id: str, upto_seq: Optional[int] = None
    ) -> Tuple[WorldEvent, ...]:
        return self.store.load_events(world_id, upto_seq)

    def submit(
        self,
        world_id: str,
        proposal: ActionProposal,
        *,
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        """Validate and commit an ordinary human/AI/delegate proposal."""

        if proposal.actor_id is None:
            raise ProposalRejected(
                "ordinary proposals require actor_id; trusted code must use submit_system"
            )
        return self._submit(
            world_id, proposal, expected_seq=expected_seq, system_authority=False
        )

    submit_proposal = submit

    def submit_system(
        self,
        world_id: str,
        proposal: ActionProposal,
        *,
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        """Commit a proposal from trusted bootstrap/kernel-side code."""

        if proposal.actor_id is not None:
            raise ProposalRejected("system proposals must have actor_id=None")
        return self._submit(
            world_id, proposal, expected_seq=expected_seq, system_authority=True
        )

    def create_entity(
        self,
        world_id: str,
        entity: Entity,
        *,
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        """Trusted v0 bootstrap helper; still passes through proposal validation."""

        proposal = ActionProposal(
            action_type=ACTION_CREATE_ENTITY,
            actor_id=None,
            parameters={"entity": entity.to_payload()},
            observed_seq=expected_seq,
        )
        return self.submit_system(
            world_id, proposal, expected_seq=expected_seq
        )

    def move(
        self,
        world_id: str,
        actor_id: str,
        to_location_id: str,
        *,
        entity_id: Optional[str] = None,
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        return self.submit(
            world_id,
            ActionProposal(
                action_type=ACTION_MOVE_ENTITY,
                actor_id=actor_id,
                parameters={
                    "entity_id": entity_id or actor_id,
                    "to_location_id": to_location_id,
                },
                observed_seq=expected_seq,
            ),
            expected_seq=expected_seq,
        )

    def speak(
        self,
        world_id: str,
        actor_id: str,
        text: str,
        *,
        target_ids: Sequence[str] = (),
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        return self.submit(
            world_id,
            ActionProposal(
                action_type=ACTION_UTTER_SPEECH,
                actor_id=actor_id,
                parameters={"text": text, "target_ids": list(target_ids)},
                observed_seq=expected_seq,
            ),
            expected_seq=expected_seq,
        )

    def submit_wish(
        self,
        world_id: str,
        actor_id: str,
        text: str,
        *,
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        return self.submit(
            world_id,
            ActionProposal(
                action_type=ACTION_SUBMIT_WISH,
                actor_id=actor_id,
                parameters={"text": text},
                observed_seq=expected_seq,
            ),
            expected_seq=expected_seq,
        )

    def select_child_goal(
        self,
        world_id: str,
        child_id: str,
        description: str,
        *,
        source_wish_ids: Sequence[str] = (),
        rationale: str = "Autonomous developmental choice.",
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        return self.submit(
            world_id,
            ActionProposal(
                action_type=ACTION_SELECT_CHILD_GOAL,
                actor_id=child_id,
                parameters={
                    "child_id": child_id,
                    "description": description,
                    "source_wish_ids": list(source_wish_ids),
                    "rationale": rationale,
                },
                observed_seq=expected_seq,
            ),
            expected_seq=expected_seq,
        )

    def freeze_latent(
        self,
        world_id: str,
        *,
        key: str,
        value: Any,
        scope: str,
        exploration_context_hash: str,
        fact_id: Optional[str] = None,
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        parameters = {
            "key": key,
            "value": value,
            "scope": scope,
            "exploration_context_hash": exploration_context_hash,
        }
        if fact_id is not None:
            parameters["fact_id"] = fact_id
        return self.submit_system(
            world_id,
            ActionProposal(
                action_type=ACTION_FREEZE_LATENT_FACT,
                actor_id=None,
                parameters=parameters,
                observed_seq=expected_seq,
            ),
            expected_seq=expected_seq,
        )

    def freeze_latent_fact(
        self,
        world_id: str,
        fact: Any,
        *,
        expected_seq: Optional[int] = None,
    ) -> WorldEvent:
        """Freeze a cognition ``LatentFact`` without coupling to its class."""

        try:
            return self.freeze_latent(
                world_id,
                key=fact.key,
                value=fact.value,
                scope=fact.scope,
                exploration_context_hash=fact.exploration_context_hash,
                fact_id=fact.fact_id,
                expected_seq=expected_seq,
            )
        except AttributeError as exc:
            raise ProposalRejected("fact does not match the latent fact interface") from exc

    def validate_and_translate(
        self,
        world_id: str,
        proposal: ActionProposal,
        *,
        state: Optional[WorldState] = None,
        system_authority: bool = False,
    ) -> EventDraft:
        """Translate a whitelist action into exactly one typed event draft."""

        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")
        if proposal.action_type not in SUPPORTED_ACTION_TYPES:
            raise ProposalRejected(
                "unsupported action type: %s" % proposal.action_type
            )
        state = state or self.get_state(world_id)
        if state.world_id != world_id:
            raise ProposalRejected("state belongs to a different world")
        if system_authority:
            if proposal.actor_id is not None:
                raise ProposalRejected("system proposal cannot impersonate an entity")
        else:
            self._require_actor(state, proposal.actor_id)

        translators = {
            ACTION_CREATE_ENTITY: self._translate_create_entity,
            ACTION_MOVE_ENTITY: self._translate_move_entity,
            ACTION_UTTER_SPEECH: self._translate_speech,
            ACTION_SUBMIT_WISH: self._translate_wish,
            ACTION_SELECT_CHILD_GOAL: self._translate_child_goal,
            ACTION_UNLOCK_CAPABILITY: self._translate_capability,
            ACTION_FREEZE_LATENT_FACT: self._translate_latent_fact,
        }
        payload = translators[proposal.action_type](
            state, proposal, system_authority
        )
        return EventDraft(
            event_type=ACTION_EVENT_TYPES[proposal.action_type],
            payload=payload,
            actor_id=proposal.actor_id,
            proposal_id=proposal.proposal_id,
        )

    def _submit(
        self,
        world_id: str,
        proposal: ActionProposal,
        *,
        expected_seq: Optional[int],
        system_authority: bool,
    ) -> WorldEvent:
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")

        existing = self.store.find_by_proposal(world_id, proposal.proposal_id)
        if existing is not None:
            expected_type = ACTION_EVENT_TYPES.get(proposal.action_type)
            if existing.actor_id != proposal.actor_id or existing.event_type != expected_type:
                raise ProposalRejected(
                    "proposal_id collides with a different committed proposal"
                )
            return existing

        state = self.get_state(world_id)
        draft = self.validate_and_translate(
            world_id,
            proposal,
            state=state,
            system_authority=system_authority,
        )
        commit_seq = expected_seq
        if commit_seq is None:
            commit_seq = (
                proposal.observed_seq
                if proposal.observed_seq is not None
                else state.seq
            )
        return self.store.append(
            world_id, draft, expected_seq=commit_seq
        )

    @staticmethod
    def _params(
        proposal: ActionProposal,
        *,
        required: Iterable[str],
        optional: Iterable[str] = (),
    ) -> Mapping[str, Any]:
        required_set = set(required)
        allowed = required_set | set(optional)
        actual = set(proposal.parameters.keys())
        missing = required_set - actual
        extra = actual - allowed
        if missing or extra:
            parts = []
            if missing:
                parts.append("missing %s" % sorted(missing))
            if extra:
                parts.append("unexpected %s" % sorted(extra))
            raise ProposalRejected(
                "%s parameters invalid: %s"
                % (proposal.action_type, "; ".join(parts))
            )
        return proposal.parameters

    @staticmethod
    def _text(value: Any, field_name: str, max_length: int = 2000) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ProposalRejected("%s must be a non-empty string" % field_name)
        cleaned = value.strip()
        if len(cleaned) > max_length:
            raise ProposalRejected(
                "%s exceeds %d characters" % (field_name, max_length)
            )
        return cleaned

    @classmethod
    def _string_tuple(
        cls, value: Any, field_name: str, max_items: int = 100
    ) -> Tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ProposalRejected("%s must be a list" % field_name)
        if len(value) > max_items:
            raise ProposalRejected(
                "%s has more than %d items" % (field_name, max_items)
            )
        values = tuple(cls._text(item, field_name, 256) for item in value)
        if len(values) != len(set(values)):
            raise ProposalRejected("%s contains duplicates" % field_name)
        return values

    @staticmethod
    def _require_actor(state: WorldState, actor_id: Optional[str]) -> Entity:
        if actor_id is None:
            raise ProposalRejected("actor_id is required")
        actor = state.entities.get(actor_id)
        if actor is None:
            raise ProposalRejected("actor does not exist: %s" % actor_id)
        if not actor.is_agent:
            raise ProposalRejected(
                "entity has no policy and therefore is not an agent: %s" % actor_id
            )
        return actor

    def _translate_create_entity(
        self,
        state: WorldState,
        proposal: ActionProposal,
        system_authority: bool,
    ) -> Mapping[str, Any]:
        params = self._params(proposal, required={"entity"})
        raw_entity = params["entity"]
        if not isinstance(raw_entity, Mapping):
            raise ProposalRejected("entity must be an object")
        allowed_entity_fields = {
            "id",
            "name",
            "kind",
            "policy_id",
            "location_id",
            "attributes",
        }
        required_entity_fields = {"id", "name", "kind"}
        actual = set(raw_entity.keys())
        if not required_entity_fields.issubset(actual) or not actual.issubset(
            allowed_entity_fields
        ):
            raise ProposalRejected("entity object does not match the v0 schema")
        try:
            entity = Entity.from_payload(raw_entity)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProposalRejected("invalid entity: %s" % exc) from exc
        if entity.id in state.entities:
            raise ProposalRejected("entity already exists: %s" % entity.id)
        if entity.location_id is not None and entity.location_id not in state.entities:
            raise ProposalRejected(
                "entity location does not exist: %s" % entity.location_id
            )
        if not system_authority:
            actor = self._require_actor(state, proposal.actor_id)
            if "capability.create_entities" not in state.capabilities_for(actor.id):
                raise ProposalRejected("actor lacks capability.create_entities")
        return {"entity": entity.to_payload()}

    def _translate_move_entity(
        self,
        state: WorldState,
        proposal: ActionProposal,
        system_authority: bool,
    ) -> Mapping[str, Any]:
        params = self._params(
            proposal, required={"entity_id", "to_location_id"}
        )
        entity_id = self._text(params["entity_id"], "entity_id", 256)
        to_location_id = self._text(
            params["to_location_id"], "to_location_id", 256
        )
        entity = state.entities.get(entity_id)
        if entity is None:
            raise ProposalRejected("entity does not exist: %s" % entity_id)
        destination = state.entities.get(to_location_id)
        if destination is None:
            raise ProposalRejected(
                "destination does not exist: %s" % to_location_id
            )
        if destination.kind not in {"place", "location", "room", "zone"}:
            raise ProposalRejected("destination is not a spatial entity")
        if entity.location_id == to_location_id:
            raise ProposalRejected("entity is already at the destination")
        if not system_authority:
            actor = self._require_actor(state, proposal.actor_id)
            if entity_id != actor.id and "capability.move_entities" not in state.capabilities_for(
                actor.id
            ):
                raise ProposalRejected("actor may move only itself")
            if (
                entity.kind == "child"
                and "capability:move" not in state.capabilities_for(entity.id)
            ):
                raise ProposalRejected(
                    "child has not unlocked capability:move; no body shape is assumed"
                )
        return {
            "entity_id": entity_id,
            "from_location_id": entity.location_id,
            "to_location_id": to_location_id,
        }

    def _translate_speech(
        self,
        state: WorldState,
        proposal: ActionProposal,
        system_authority: bool,
    ) -> Mapping[str, Any]:
        if system_authority:
            raise ProposalRejected("the world kernel cannot speak as a resident")
        params = self._params(
            proposal, required={"text"}, optional={"target_ids", "utterance_id"}
        )
        actor = self._require_actor(state, proposal.actor_id)
        if (
            actor.kind == "child"
            and "capability:communicate" not in state.capabilities_for(actor.id)
        ):
            raise ProposalRejected("child has not unlocked capability:communicate")
        text = self._text(params["text"], "text", 4000)
        target_ids = self._string_tuple(
            params.get("target_ids", ()), "target_ids", 20
        )
        for target_id in target_ids:
            if target_id not in state.entities:
                raise ProposalRejected("speech target does not exist: %s" % target_id)
        utterance_id = self._text(
            params.get("utterance_id", new_id("utterance")),
            "utterance_id",
            256,
        )
        if utterance_id in state.entities:
            raise ProposalRejected("utterance id already exists: %s" % utterance_id)
        return {
            "utterance_id": utterance_id,
            "speaker_id": actor.id,
            "text": text,
            "target_ids": target_ids,
            "location_id": actor.location_id,
        }

    def _translate_wish(
        self,
        state: WorldState,
        proposal: ActionProposal,
        system_authority: bool,
    ) -> Mapping[str, Any]:
        if system_authority:
            raise ProposalRejected("the world kernel cannot submit a wish")
        params = self._params(
            proposal, required={"text"}, optional={"wish_id"}
        )
        actor = self._require_actor(state, proposal.actor_id)
        text = self._text(params["text"], "text", 2000)
        default_id = "wish_" + hashlib.sha256(
            proposal.proposal_id.encode("utf-8")
        ).hexdigest()[:24]
        wish_id = self._text(params.get("wish_id", default_id), "wish_id", 256)
        if wish_id in state.entities:
            raise ProposalRejected("wish id already exists: %s" % wish_id)
        return {"wish_id": wish_id, "submitted_by": actor.id, "text": text}

    def _translate_child_goal(
        self,
        state: WorldState,
        proposal: ActionProposal,
        system_authority: bool,
    ) -> Mapping[str, Any]:
        if system_authority:
            raise ProposalRejected(
                "child goals must be selected by the child, not the world kernel"
            )
        params = self._params(
            proposal,
            required={"child_id", "description"},
            optional={"goal_id", "source_wish_ids", "rationale"},
        )
        actor = self._require_actor(state, proposal.actor_id)
        child_id = self._text(params["child_id"], "child_id", 256)
        child = state.entities.get(child_id)
        if child is None or child.kind != "child" or not child.is_agent:
            raise ProposalRejected("child_id is not a child agent")
        if actor.id != child_id:
            raise ProposalRejected("only the child may select its own goal")
        source_wish_ids = self._string_tuple(
            params.get("source_wish_ids", ()), "source_wish_ids", 100
        )
        for wish_id in source_wish_ids:
            wish = state.entities.get(wish_id)
            if wish is None or wish.kind != "wish":
                raise ProposalRejected("goal source is not a wish: %s" % wish_id)
        description = self._text(params["description"], "description", 2000)
        rationale = self._text(
            params.get("rationale", "Autonomous developmental choice."),
            "rationale",
            2000,
        )
        default_id = "goal_" + hashlib.sha256(
            proposal.proposal_id.encode("utf-8")
        ).hexdigest()[:24]
        goal_id = self._text(params.get("goal_id", default_id), "goal_id", 256)
        if goal_id in state.entities:
            raise ProposalRejected("goal id already exists: %s" % goal_id)
        return {
            "goal_id": goal_id,
            "child_id": child_id,
            "description": description,
            "source_wish_ids": source_wish_ids,
            "rationale": rationale,
        }

    def _translate_capability(
        self,
        state: WorldState,
        proposal: ActionProposal,
        system_authority: bool,
    ) -> Mapping[str, Any]:
        params = self._params(
            proposal,
            required={"entity_id", "capability_id", "name", "description"},
            optional={"prerequisite_ids", "evidence_event_ids"},
        )
        entity_id = self._text(params["entity_id"], "entity_id", 256)
        owner = state.entities.get(entity_id)
        if owner is None or not owner.is_agent:
            raise ProposalRejected("capability owner is not an agent")
        if not system_authority:
            actor = self._require_actor(state, proposal.actor_id)
            if actor.id != entity_id:
                raise ProposalRejected("an agent may unlock only its own capability")
        capability_id = self._text(
            params["capability_id"], "capability_id", 256
        )
        if capability_id in state.capabilities_for(entity_id):
            raise ProposalRejected("capability is already unlocked")
        existing = state.entities.get(capability_id)
        if existing is not None and existing.kind != "capability":
            raise ProposalRejected("capability id belongs to another entity kind")
        prerequisite_ids = self._string_tuple(
            params.get("prerequisite_ids", ()), "prerequisite_ids", 100
        )
        if not set(prerequisite_ids).issubset(state.capabilities_for(entity_id)):
            raise ProposalRejected("capability prerequisites are not unlocked")
        evidence_event_ids = self._string_tuple(
            params.get("evidence_event_ids", ()), "evidence_event_ids", 100
        )
        if not system_authority and not evidence_event_ids:
            raise ProposalRejected(
                "agent-proposed capability unlock requires prior event evidence"
            )
        if not set(evidence_event_ids).issubset(state.applied_event_ids):
            raise ProposalRejected("capability evidence must reference prior events")
        return {
            "entity_id": entity_id,
            "capability_id": capability_id,
            "name": self._text(params["name"], "name", 512),
            "description": self._text(
                params["description"], "description", 2000
            ),
            "prerequisite_ids": prerequisite_ids,
            "evidence_event_ids": evidence_event_ids,
        }

    def _translate_latent_fact(
        self,
        state: WorldState,
        proposal: ActionProposal,
        system_authority: bool,
    ) -> Mapping[str, Any]:
        if not system_authority:
            raise ProposalRejected("only trusted latent-resolution code may freeze truth")
        params = self._params(
            proposal,
            required={"key", "value", "scope", "exploration_context_hash"},
            optional={"fact_id"},
        )
        if state.seed is None:
            raise ProposalRejected("world has no fixed seed")
        key = self._text(params["key"], "key", 512)
        scope = self._text(params["scope"], "scope", 512)
        context_hash = self._text(
            params["exploration_context_hash"],
            "exploration_context_hash",
            64,
        ).lower()
        if len(context_hash) != 64:
            raise ProposalRejected("exploration_context_hash must be SHA-256 hex")
        try:
            int(context_hash, 16)
        except ValueError as exc:
            raise ProposalRejected(
                "exploration_context_hash must be SHA-256 hex"
            ) from exc
        try:
            canonical_json(params["value"])
        except ValueError as exc:
            raise ProposalRejected("latent value must be JSON serializable") from exc
        semantic_material = "%s\0%s\0%s" % (state.seed, scope, key)
        semantic_key = hashlib.sha256(
            semantic_material.encode("utf-8")
        ).hexdigest()
        determination_material = "%s\0%s" % (
            semantic_material,
            context_hash,
        )
        determinism_key = hashlib.sha256(
            determination_material.encode("utf-8")
        ).hexdigest()
        if state.latent_fact_for(semantic_key) is not None:
            raise ProposalRejected(
                "latent truth is already frozen for this seed, scope, and key"
            )
        default_id = "latent_" + determinism_key[:24]
        fact_id = self._text(params.get("fact_id", default_id), "fact_id", 256)
        if fact_id in state.entities:
            raise ProposalRejected("latent fact id already exists")
        return {
            "fact_id": fact_id,
            "key": key,
            "value": thaw(params["value"]),
            "scope": scope,
            "exploration_context_hash": context_hash,
            "determinism_key": determinism_key,
        }


# A concise conventional name for callers.
Kernel = WorldKernel
