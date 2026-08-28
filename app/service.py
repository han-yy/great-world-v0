"""Application use cases without weakening the kernel boundary."""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.epistemic_store import EpistemicStore
from app.intents import (
    ACTION_EXPLORE,
    IntentInterpreter,
    IntentStep,
    PlayerIntentContext,
    RuleBasedIntentInterpreter,
    free_activity,
)
from app.runtime import Participant, RuntimeStore
from app.scenario import (
    ALL_ENTITIES,
    ARRIVAL_DETAIL,
    ARRIVAL_SUMMARY,
    BOOTSTRAP_HEAD_SEQ,
    CAPABILITY_LABELS,
    CHILD,
    CONTROLLER_BINDINGS,
    FUTURE_MODE,
    INITIAL_STATE_MODE,
    LATENT_RULE_VERSION,
    LATENT_ASPECTS,
    PLAYER_SLOTS,
    RULES_VERSION,
    SCENARIO_ID,
    SCENARIO_NAME,
    SCENARIO_THEME,
    STARTING_LOCATION_ID,
)
from world.child import Wish, select_child_goal
from world.controllers import (
    ActionIntent,
    ControllerUnavailable,
    DecisionContext,
    DelegateController,
    Policy,
    ScriptedAIController,
)
from world.event_store import SQLiteEventStore, WorldNotFound
from world.kernel import (
    ACTION_PERFORM_ACTIVITY,
    ACTION_FREEZE_LATENT_FACT,
    ACTION_MOVE_ENTITY,
    ACTION_SELECT_CHILD_GOAL,
    ACTION_SUBMIT_WISH,
    ACTION_UNLOCK_CAPABILITY,
    ACTION_UTTER_SPEECH,
    WorldKernel,
)
from world.latent import ExplorationContext, LatentFact, LatentRealityResolver
from world.models import (
    ACTIVITY_PERFORMED,
    CAPABILITY_UNLOCKED,
    CHILD_GOAL_SELECTED,
    ENTITY_MOVED,
    LATENT_FACT_FROZEN,
    SPEECH_UTTERED,
    WISH_SUBMITTED,
    ActionProposal,
    Entity,
    WorldEvent,
    canonical_json,
    new_id,
    thaw,
)
from world.perception import (
    BeliefRecord,
    MemoryRecord,
    PerceptionBeliefMemoryPipeline,
    PerceptionRecord,
    ProvenanceRef,
)
from world.state import WorldState, reduce_event


class AuthorizationError(RuntimeError):
    pass


class CapacityError(RuntimeError):
    pass


class ArchivedWorldError(RuntimeError):
    def __init__(self, world_id: str, current_world_id: str | None) -> None:
        self.world_id = world_id
        self.current_world_id = current_world_id
        super().__init__("这个世界已经封存，只能观察。")


logger = logging.getLogger(__name__)

# Worlds created before scenario metadata recorded their bootstrap boundary
# used 21 initial events. Keeping that value here makes archived v0 epochs
# render their original tick numbers after the scenario reset.
LEGACY_BOOTSTRAP_HEAD_SEQ = 21


@dataclass(frozen=True)
class AdvanceResult:
    events: tuple[WorldEvent, ...]
    message: str


@dataclass(frozen=True)
class TurnResult:
    player_events: tuple[WorldEvent, ...]
    response_events: tuple[WorldEvent, ...]
    message: str
    feedback: tuple[Mapping[str, Any], ...]
    view: Mapping[str, Any]


@dataclass(frozen=True)
class ResetResult:
    archived_world_id: str
    world_id: str
    epoch_index: int
    seed_changed: bool


def _clean_text(value: Any, field_name: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{field_name} 不能超过 {maximum} 个字符。")
    return cleaned


def _require_exact_keys(payload: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        raise ValueError(f"行动包含未知字段：{', '.join(sorted(extra))}")


class WorldService:
    """Orchestrates reality identity, kernel commits and private projections."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        llm_policy: Policy | None = None,
        llm_agent_ids: Iterable[str] = (),
        intent_interpreter: IntentInterpreter | None = None,
    ):
        self.database_path = Path(database_path)
        self.events = SQLiteEventStore(self.database_path)
        self.kernel = WorldKernel(self.events)
        self.runtime = RuntimeStore(self.database_path)
        self.epistemic = EpistemicStore(self.database_path)
        self.pipeline = PerceptionBeliefMemoryPipeline()
        self.llm_policy = llm_policy
        self.intent_interpreter = intent_interpreter
        self.rule_intent_interpreter = RuleBasedIntentInterpreter()
        self.llm_agent_ids = frozenset(str(item) for item in llm_agent_ids)
        self._advance_lock = Lock()
        invalid_llm_agents = {
            entity_id
            for entity_id in self.llm_agent_ids
            if CONTROLLER_BINDINGS.get(entity_id) not in {"scripted_ai", "delegate"}
        }
        if invalid_llm_agents:
            raise ValueError(
                "LLM 只能绑定到已有的 AI 或 delegate 实体："
                + ", ".join(sorted(invalid_llm_agents))
            )
        if self.llm_agent_ids and self.llm_policy is None:
            raise ValueError("配置了 LLM 实体，但没有提供 LLM policy。")

    def initialize(self) -> str:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime.initialize()
        self.epistemic.initialize()
        existing = self.runtime.default_world()
        if existing is not None:
            try:
                self.events.get_world(existing)
                self.runtime.ensure_epoch(existing)
                return existing
            except WorldNotFound:
                # A stale reality-layer pointer has no authority over the
                # ledger.  Create a new root rather than inventing history.
                pass
        world_id = f"world_{uuid.uuid4().hex}"
        seed = uuid.uuid4().hex + uuid.uuid4().hex
        self._bootstrap_world(world_id, seed)
        self.runtime.set_default_world(world_id)
        self.runtime.ensure_epoch(world_id)
        return world_id

    def _bootstrap_world(self, world_id: str, seed: str) -> None:
        self.kernel.create_world(
            world_id,
            seed=seed,
            name=SCENARIO_NAME,
            metadata={
                "scenario": SCENARIO_ID,
                "scenario_theme": SCENARIO_THEME,
                "initial_state_mode": INITIAL_STATE_MODE,
                "future_mode": FUTURE_MODE,
                "rules_version": RULES_VERSION,
                "bootstrap_head_seq": BOOTSTRAP_HEAD_SEQ,
            },
        )
        for raw in ALL_ENTITIES:
            excluded = {
                "entity_id",
                "name",
                "archetype",
                "policy_id",
                "location_id",
                "capabilities",
                "memory",
                "knowledge",
                "skills",
            }
            attributes = {
                key: value for key, value in raw.items() if key not in excluded
            }
            if raw["archetype"] == "child":
                attributes["development"] = {
                    "memory": list(raw.get("memory", ())),
                    "knowledge": list(raw.get("knowledge", ())),
                    "skills": list(raw.get("skills", ())),
                }
            entity = Entity(
                id=raw["entity_id"],
                name=raw["name"],
                kind=raw["archetype"],
                policy_id=raw.get("policy_id"),
                location_id=raw.get("location_id"),
                attributes=attributes,
            )
            self.kernel.create_entity(world_id, entity)

        # Initial abilities are explicit graph nodes/events, not an anatomical
        # assumption hidden in prose.
        prerequisites: dict[str, tuple[str, ...]] = {
            "capability:perceive": (),
            "capability:remember": ("capability:perceive",),
            "capability:communicate": ("capability:perceive",),
        }
        names = {
            "capability:perceive": "感知",
            "capability:remember": "记忆",
            "capability:communicate": "交流",
        }
        for capability_id in (
            "capability:perceive",
            "capability:remember",
            "capability:communicate",
        ):
            state = self.kernel.state(world_id)
            proposal = ActionProposal(
                action_type=ACTION_UNLOCK_CAPABILITY,
                actor_id=None,
                parameters={
                    "entity_id": CHILD["entity_id"],
                    "capability_id": capability_id,
                    "name": names[capability_id],
                    "description": f"孩子已经获得最小的{names[capability_id]}能力。",
                    "prerequisite_ids": prerequisites[capability_id],
                    "evidence_event_ids": (),
                },
                observed_seq=state.seq,
            )
            self.kernel.submit_system(world_id, proposal)

    def join_default_world(self, participant: Participant) -> tuple[str, str]:
        world_id = self.runtime.default_world() or self.initialize()
        existing = self.runtime.membership(participant.participant_id, world_id)
        if existing:
            return world_id, existing

        state = self.kernel.state(world_id)
        claimed = self.runtime.claimed_entities(world_id)
        available = [
            raw["entity_id"]
            for raw in PLAYER_SLOTS
            if raw["entity_id"] in state.entities and raw["entity_id"] not in claimed
        ]
        if not available:
            raise CapacityError("这个 v0 世界的 5 个来客席位已经满了。")
        entity_id = available[0]
        self.runtime.join(participant.participant_id, world_id, entity_id)
        self._record_arrival_experience(world_id, entity_id)
        return world_id, entity_id

    def _membership(self, participant: Participant, world_id: str) -> str:
        entity_id = self.runtime.membership(participant.participant_id, world_id)
        if entity_id is None:
            raise AuthorizationError("你尚未进入这个世界分支。")
        return entity_id

    def _require_active_world(self, world_id: str) -> None:
        if self.runtime.is_archived_world(world_id):
            raise ArchivedWorldError(world_id, self.runtime.default_world())

    def _record_arrival_experience(self, world_id: str, observer_id: str) -> None:
        source = self.events.event_at(world_id, 1)
        wrapper = {
            "event_id": source.event_id,
            "event_type": "world.entered",
            "payload": {"observable_by": [observer_id]},
            "seq": source.seq,
            "occurred_at": source.occurred_at,
        }
        update = self.pipeline.process(
            wrapper,
            observer_id,
            projection=lambda _event, _observer: {
                "kind": "arrival",
                "location_id": STARTING_LOCATION_ID,
                "summary": ARRIVAL_SUMMARY,
                "detail": ARRIVAL_DETAIL,
            },
            salience=0.9,
        )
        self._persist_update(world_id, update)

    def submit_action(
        self,
        participant: Participant,
        world_id: str,
        action_type: str,
        payload: Mapping[str, Any],
    ) -> tuple[WorldEvent, ...]:
        self._require_active_world(world_id)
        actor_id = self._membership(participant, world_id)
        if not isinstance(payload, Mapping):
            raise ValueError("行动参数必须是对象。")
        if action_type == "explore":
            return self._explore(world_id, actor_id, payload)

        state_before = self.kernel.state(world_id)
        if action_type == "move":
            _require_exact_keys(payload, {"destination_id"})
            destination_id = _clean_text(payload.get("destination_id"), "目的地", maximum=256)
            proposal_type = ACTION_MOVE_ENTITY
            parameters = {"entity_id": actor_id, "to_location_id": destination_id}
        elif action_type == "speak":
            _require_exact_keys(payload, {"text"})
            proposal_type = ACTION_UTTER_SPEECH
            parameters = {"text": _clean_text(payload.get("text"), "说话内容")}
        elif action_type == "wish":
            _require_exact_keys(payload, {"text"})
            proposal_type = ACTION_SUBMIT_WISH
            parameters = {"text": _clean_text(payload.get("text"), "愿望")}
        else:
            raise ValueError("未知行动。")

        proposal = ActionProposal(
            action_type=proposal_type,
            actor_id=actor_id,
            parameters=parameters,
            observed_seq=state_before.seq,
        )
        event = self.kernel.submit(world_id, proposal)
        state_after = self.kernel.state(world_id)
        self._derive_cognition(world_id, event, state_before, state_after)
        return (event,)

    def _player_intent_context(
        self, state: WorldState, actor_id: str
    ) -> PlayerIntentContext:
        actor = state.entity(actor_id)
        location = state.entities.get(actor.location_id or "")
        locations = tuple(
            {
                "id": entity.id,
                "name": entity.name,
                "description": str(entity.attributes.get("description", "")),
            }
            for entity in state.entities.values()
            if entity.kind == "place"
        )
        nearby = tuple(
            {
                "id": entity.id,
                "name": entity.name,
                "kind": entity.kind,
                "description": str(entity.attributes.get("description", "")),
            }
            for entity in state.entities.values()
            if (
                entity.id == actor.location_id
                or entity.location_id == actor.location_id
                or entity.id == actor.id
            )
            and entity.kind
            not in {
                "activity",
                "capability",
                "goal",
                "latent_fact",
                "utterance",
                "wish",
            }
        )
        return PlayerIntentContext(
            actor_id=actor.id,
            observed_seq=state.seq,
            self_name=actor.name,
            location_id=actor.location_id,
            location_name=location.name if location else None,
            locations=locations,
            nearby=nearby,
        )

    def _interpret_turn(
        self, text: str, context: PlayerIntentContext
    ) -> tuple[IntentStep, ...]:
        local = self.rule_intent_interpreter(text, context)
        if local:
            return local
        if self.intent_interpreter is not None:
            try:
                interpreted = self.intent_interpreter(text, context)
                if interpreted:
                    return tuple(interpreted)
            except ControllerUnavailable as exc:
                logger.warning("intent interpreter unavailable: %s", exc)
        return free_activity(text)

    def _proposal_for_turn_step(
        self,
        *,
        world_id: str,
        actor_id: str,
        step: IntentStep,
        observed_seq: int,
        request_id: str,
        index: int,
    ) -> ActionProposal:
        parameters = dict(step.parameters)
        if step.action_type == ACTION_MOVE_ENTITY:
            _require_exact_keys(parameters, {"to_location_id"})
            parameters = {
                "entity_id": actor_id,
                "to_location_id": _clean_text(
                    parameters.get("to_location_id"), "地点", maximum=256
                ),
            }
        elif step.action_type == ACTION_UTTER_SPEECH:
            _require_exact_keys(parameters, {"text"})
            parameters = {"text": _clean_text(parameters.get("text"), "说话内容")}
        elif step.action_type == ACTION_SUBMIT_WISH:
            _require_exact_keys(parameters, {"text"})
            parameters = {"text": _clean_text(parameters.get("text"), "愿望")}
        elif step.action_type == ACTION_PERFORM_ACTIVITY:
            _require_exact_keys(parameters, {"description", "target_ids"})
            target_ids = parameters.get("target_ids", ())
            if not isinstance(target_ids, (list, tuple)):
                raise ValueError("活动对象必须是列表。")
            parameters = {
                "description": _clean_text(
                    parameters.get("description"), "想做的事", maximum=500
                ),
                "target_ids": list(target_ids),
            }
        else:
            raise ValueError("这个意图还没有可验证的世界语义。")

        identity = canonical_json(
            {
                "world_id": world_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "index": index,
                "action_type": step.action_type,
                "parameters": parameters,
            }
        )
        proposal_id = "turn_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return ActionProposal(
            action_type=step.action_type,
            actor_id=actor_id,
            parameters=parameters,
            proposal_id=proposal_id,
            observed_seq=observed_seq,
        )

    def _commit_kernel_steps(
        self,
        world_id: str,
        actor_id: str,
        steps: Sequence[IntentStep],
        *,
        request_id: str,
        start_index: int,
    ) -> tuple[WorldEvent, ...]:
        if not steps:
            return ()
        state = self.kernel.state(world_id)
        proposals = tuple(
            self._proposal_for_turn_step(
                world_id=world_id,
                actor_id=actor_id,
                step=step,
                observed_seq=state.seq + offset,
                request_id=request_id,
                index=start_index + offset,
            )
            for offset, step in enumerate(steps)
        )
        committed = self.kernel.submit_many(
            world_id, proposals, expected_seq=state.seq
        )
        before = state
        for event in committed:
            after = reduce_event(before, event)
            self._derive_cognition(world_id, event, before, after)
            before = after
        return committed

    def _commit_turn_steps(
        self,
        world_id: str,
        actor_id: str,
        steps: Sequence[IntentStep],
        *,
        request_id: str,
    ) -> tuple[WorldEvent, ...]:
        committed: list[WorldEvent] = []
        pending: list[IntentStep] = []
        pending_start = 0

        def flush() -> None:
            nonlocal pending, pending_start
            if not pending:
                return
            committed.extend(
                self._commit_kernel_steps(
                    world_id,
                    actor_id,
                    pending,
                    request_id=request_id,
                    start_index=pending_start,
                )
            )
            pending = []

        for index, step in enumerate(steps):
            if step.action_type == ACTION_EXPLORE:
                flush()
                payload = dict(step.parameters)
                _require_exact_keys(payload, {"target_id", "aspect"})
                committed.extend(self._explore(world_id, actor_id, payload))
                pending_start = index + 1
                continue
            if not pending:
                pending_start = index
            pending.append(step)
        flush()
        return tuple(committed)

    def perform_turn(
        self,
        participant: Participant,
        world_id: str,
        text: str,
        *,
        observed_seq: int,
        request_id: str,
    ) -> TurnResult:
        self._require_active_world(world_id)
        actor_id = self._membership(participant, world_id)
        cleaned = _clean_text(text, "你想做的事", maximum=1000)
        request_id = _clean_text(request_id, "request_id", maximum=128)
        with self._advance_lock:
            seen_experience_ids = {
                item["perception_id"]
                for item in self.epistemic.perceptions_for(
                    world_id, actor_id, limit=200
                )
            }
            initial = self.kernel.state(world_id)
            if int(observed_seq) != initial.seq:
                from world.event_store import ConcurrencyConflict

                raise ConcurrencyConflict(world_id, int(observed_seq), initial.seq)
            context = self._player_intent_context(initial, actor_id)
            steps = self._interpret_turn(cleaned, context)
            player_events = self._commit_turn_steps(
                world_id,
                actor_id,
                steps,
                request_id=request_id,
            )
            responses = self._advance_locked(world_id)
            view = self.observer_view(participant, world_id)
            feedback = tuple(
                item
                for item in view["experiences"]
                if item["id"] not in seen_experience_ids
            )[-6:]
            if not feedback:
                feedback = (
                    {
                        "id": f"turn-feedback-{request_id}",
                        "tick": view["world"]["tick"],
                        "summary": "你留意着眼前的结果。",
                        "detail": "这一次没有出现新的可观察变化；这里仍保持着刚才的样子。",
                    },
                )
            latest = feedback[-1]
            message = str(latest["summary"])
            if latest.get("detail"):
                message += f" {latest['detail']}"
            return TurnResult(
                player_events,
                responses.events,
                message,
                feedback,
                view,
            )

    def _latent_value_factory(
        self, determinism_key: str, context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        parameters = context.get("parameters", {})
        aspect = str(parameters.get("aspect", "condition"))
        rule = LATENT_ASPECTS.get(aspect)
        if rule is None:
            raise ValueError("这个探索维度没有固定规则。")
        point = int(determinism_key[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
        cumulative = 0.0
        selected = rule["values"][-1]
        for value, weight in zip(rule["values"], rule["weights"]):
            cumulative += float(weight)
            if point <= cumulative:
                selected = value
                break
        return {
            "target_id": parameters["target_id"],
            "aspect": aspect,
            "description": selected,
            "rule_version": LATENT_RULE_VERSION,
        }

    def _existing_latent(self, state: WorldState, key: str, scope: str) -> Entity | None:
        for entity in state.entities.values():
            if entity.kind != "latent_fact":
                continue
            if entity.attributes.get("key") == key and entity.attributes.get("scope") == scope:
                return entity
        return None

    def _source_event_for_entity(self, world_id: str, entity_id: str) -> WorldEvent:
        for event in reversed(self.kernel.history(world_id)):
            if event.payload.get("fact_id") == entity_id:
                return event
        raise RuntimeError("冻结事实缺少来源事件。")

    def _explore(
        self, world_id: str, actor_id: str, payload: Mapping[str, Any]
    ) -> tuple[WorldEvent, ...]:
        _require_exact_keys(payload, {"target_id", "aspect"})
        target_id = _clean_text(payload.get("target_id"), "探索对象", maximum=256)
        aspect = _clean_text(payload.get("aspect"), "探索维度", maximum=64)
        if aspect not in LATENT_ASPECTS:
            raise ValueError("这个探索维度尚未定义固定规则。")
        state_before = self.kernel.state(world_id)
        actor = state_before.entity(actor_id)
        target = state_before.entities.get(target_id)
        if target is None:
            raise ValueError("探索对象不存在。")
        if target.kind not in {"place", "object"}:
            raise ValueError("人的过去不会由隐藏规则生成；请通过相处和交谈了解对方。")
        visible = target.id == actor.location_id or target.location_id == actor.location_id
        if not visible:
            raise AuthorizationError("这个对象不在你的可观察范围内。")

        key = f"{target_id}:{aspect}"
        scope = f"entity:{target_id}"
        existing = self._existing_latent(state_before, key, scope)
        committed: tuple[WorldEvent, ...] = ()
        if existing is None:
            context = ExplorationContext.from_kernel(
                exploration_id=f"explore:{actor_id}:{target_id}:{aspect}:seq:{state_before.seq}",
                observed_seq=state_before.seq,
                actor_id=actor_id,
                location_id=actor.location_id,
                method="inspect",
                parameters={
                    "target_id": target_id,
                    "aspect": aspect,
                    "rule_version": LATENT_RULE_VERSION,
                },
            )
            resolver = LatentRealityResolver(
                state_before.seed or "missing-seed",
                value_factory=self._latent_value_factory,
            )
            candidate = resolver.preview_resolution(key, context, scope=scope)
            proposal = ActionProposal(
                action_type=ACTION_FREEZE_LATENT_FACT,
                actor_id=None,
                parameters={
                    "fact_id": candidate.fact_id,
                    "key": candidate.key,
                    "value": candidate.value,
                    "scope": candidate.scope,
                    "exploration_context_hash": candidate.exploration_context_hash,
                },
                observed_seq=state_before.seq,
            )
            event = self.kernel.submit_system(world_id, proposal)
            committed = (event,)
            source_event = event
            state_after = self.kernel.state(world_id)
            existing = state_after.entities[event.payload["fact_id"]]
        else:
            source_event = self._source_event_for_entity(world_id, existing.id)

        revealed = dict(existing.attributes["value"])
        wrapper = {
            "event_id": source_event.event_id,
            "event_type": "latent.discovered",
            "payload": {"observable_by": [actor_id]},
            "seq": source_event.seq,
            "occurred_at": source_event.occurred_at,
        }
        update = self.pipeline.process(
            wrapper,
            actor_id,
            projection=lambda _event, _observer: {
                "kind": "latent_discovery",
                "target_id": target_id,
                "target_name": target.name,
                "aspect": aspect,
                "description": revealed["description"],
            },
            salience=0.8,
        )
        self._persist_update(world_id, update)
        return committed

    def _persist_update(self, world_id: str, update: Any) -> None:
        if update.perception is None:
            return
        self.epistemic.add_perception(world_id, update.perception)
        self.epistemic.add_beliefs(world_id, update.beliefs)
        self.epistemic.add_memories(world_id, update.memories)

    def _agents(self, state: WorldState) -> tuple[Entity, ...]:
        return tuple(entity for entity in state.entities.values() if entity.is_agent)

    def _derive_for_observer(
        self,
        world_id: str,
        event: WorldEvent,
        observer_id: str,
        details: Mapping[str, Any],
        *,
        salience: float = 0.5,
    ) -> None:
        update = self.pipeline.process(
            event,
            observer_id,
            projection=lambda _event, _observer: details,
            salience=salience,
        )
        self._persist_update(world_id, update)

    def _derive_cognition(
        self,
        world_id: str,
        event: WorldEvent,
        before: WorldState,
        after: WorldState,
    ) -> None:
        agents = self._agents(after)
        if event.event_type == ENTITY_MOVED:
            actor_id = str(event.payload["entity_id"])
            origin_id = event.payload["from_location_id"]
            destination_id = str(event.payload["to_location_id"])
            for observer in agents:
                direction: str | None = None
                if observer.id == actor_id:
                    direction = "self"
                elif before.entities.get(observer.id) and before.entities[observer.id].location_id == origin_id:
                    direction = "left"
                elif observer.location_id == destination_id:
                    direction = "arrived"
                if direction:
                    self._derive_for_observer(
                        world_id,
                        event,
                        observer.id,
                        {
                            "kind": "movement",
                            "actor_id": actor_id,
                            "direction": direction,
                            "from_location_id": origin_id,
                            "to_location_id": destination_id,
                        },
                    )
            return

        if event.event_type == SPEECH_UTTERED:
            speaker_id = str(event.payload["speaker_id"])
            location_id = event.payload["location_id"]
            targets = set(event.payload["target_ids"])
            for observer in agents:
                if observer.location_id == location_id or observer.id in targets:
                    # Use the cognition module's special speech projection: it
                    # records only "speaker said X", never X-as-truth.
                    update = self.pipeline.process(event, observer.id, salience=0.65)
                    self._persist_update(world_id, update)
            return

        if event.event_type == ACTIVITY_PERFORMED:
            actor_id = str(event.payload["actor_id"])
            location_id = event.payload["location_id"]
            targets = set(event.payload["target_ids"])
            for observer in agents:
                if (
                    observer.id == actor_id
                    or observer.location_id == location_id
                    or observer.id in targets
                ):
                    self._derive_for_observer(
                        world_id,
                        event,
                        observer.id,
                        {
                            "kind": "activity",
                            "actor_id": actor_id,
                            "description": event.payload["description"],
                            "target_ids": event.payload["target_ids"],
                            "location_id": location_id,
                        },
                        salience=0.6,
                    )
            return

        if event.event_type == WISH_SUBMITTED:
            # The wish pool is a declared public communication surface, so all
            # agents receive the text without receiving the submitter's
            # controller identity or private cognition.
            for observer in agents:
                self._derive_for_observer(
                    world_id,
                    event,
                    observer.id,
                    {
                        "kind": "wish",
                        "wish_id": event.payload["wish_id"],
                        "submitted_by": event.payload["submitted_by"],
                        "text": event.payload["text"],
                    },
                    salience=0.75,
                )
            return

        if event.event_type == CHILD_GOAL_SELECTED:
            for observer in agents:
                self._derive_for_observer(
                    world_id,
                    event,
                    observer.id,
                    {
                        "kind": "goal",
                        "child_id": event.payload["child_id"],
                        "description": event.payload["description"],
                        "source_wish_ids": event.payload["source_wish_ids"],
                    },
                    salience=0.9,
                )
            return

        if event.event_type == CAPABILITY_UNLOCKED:
            owner_id = str(event.payload["entity_id"])
            self._derive_for_observer(
                world_id,
                event,
                owner_id,
                {
                    "kind": "capability",
                    "entity_id": owner_id,
                    "capability_id": event.payload["capability_id"],
                    "name": event.payload["name"],
                },
                salience=0.9,
            )

    def _safe_agent_view(self, state: WorldState, observer_id: str) -> Mapping[str, Any]:
        observer = state.entity(observer_id)
        nearby = [
            {"id": entity.id, "name": entity.name, "kind": entity.kind}
            for entity in state.entities.values()
            if entity.location_id == observer.location_id
            and entity.kind
            not in {
                "activity",
                "capability",
                "goal",
                "latent_fact",
                "utterance",
                "wish",
            }
        ]
        return {
            "self": {
                "id": observer.id,
                "name": observer.name,
                "description": observer.attributes.get("description", ""),
                "location_id": observer.location_id,
            },
            "nearby": nearby,
            "locations": [
                {
                    "id": entity.id,
                    "name": entity.name,
                    "description": str(entity.attributes.get("description", "")),
                }
                for entity in state.entities.values()
                if entity.kind == "place"
            ],
            "seq": state.seq,
        }

    def _script_policy(self, resident_id: str) -> Callable[[DecisionContext], ActionIntent | None]:
        replies = {
            "resident:linqiao": "我听见了。你愿意再说具体一点吗？",
            "resident:meiyu": "我先把这句话记下来，免得我们以为自己记得一样。",
            "resident:laozhu": "如果它能拆成一个小步骤，我们可以试试。",
            "resident:qiaoan": "先确认每个人都安全，再决定下一步。",
            "resident:chihe": "这里留下了一点变化，我想再观察一会儿。",
        }

        def policy(context: DecisionContext) -> ActionIntent | None:
            for trigger in reversed(context.trigger_events):
                details = trigger.details if isinstance(trigger, PerceptionRecord) else {}
                if (
                    isinstance(trigger, PerceptionRecord)
                    and trigger.perceived_type == "heard_speech"
                    and details.get("speaker_id") != resident_id
                ):
                    return ActionIntent(ACTION_UTTER_SPEECH, {"text": replies[resident_id]})
            return None

        return policy

    def _resident_policy(self, resident_id: str) -> Policy:
        if self.llm_policy is not None and resident_id in self.llm_agent_ids:
            return self.llm_policy
        return self._script_policy(resident_id)

    def _child_policy(
        self, state: WorldState
    ) -> Callable[[DecisionContext], ActionIntent | None]:
        def policy(_context: DecisionContext) -> ActionIntent | None:
            if state.active_goal_for(CHILD["entity_id"]) is not None:
                return None
            wishes = [
                Wish(
                    wish_id=wish_id,
                    submitted_by=str(state.entities[wish_id].attributes["submitted_by"]),
                    text=str(state.entities[wish_id].attributes["text"]),
                )
                for wish_id in state.wish_ids
            ]
            goal = select_child_goal(
                wishes,
                child_id=CHILD["entity_id"],
                world_seed=state.seed or "missing-seed",
            )
            if goal is None:
                return None
            return ActionIntent(
                ACTION_SELECT_CHILD_GOAL,
                {
                    "child_id": goal.child_id,
                    "goal_id": goal.goal_id,
                    "description": goal.description,
                    "source_wish_ids": goal.source_wish_ids,
                    "rationale": goal.rationale,
                },
            )

        return policy

    def advance(self, participant: Participant, world_id: str) -> AdvanceResult:
        self._require_active_world(world_id)
        self._membership(participant, world_id)
        # v0 runs one process and serializes bounded agent batches. Human
        # actions may still race, in which case the event store's expected-seq
        # check rejects the stale proposal rather than rebasing it.
        with self._advance_lock:
            return self._advance_locked(world_id)

    def _advance_locked(self, world_id: str) -> AdvanceResult:
        initial_state = self.kernel.state(world_id)
        initial_head = initial_state.seq
        committed: list[WorldEvent] = []
        unavailable = 0

        for entity_id, controller_type in CONTROLLER_BINDINGS.items():
            if controller_type == "human" or entity_id not in initial_state.entities:
                continue
            cursor = self.runtime.get_cursor(world_id, entity_id)
            trigger_rows = tuple(
                item
                for item in self.epistemic.perceptions_for(world_id, entity_id, limit=100)
                if cursor < int(item.get("source_seq") or 0) <= initial_head
            )
            if controller_type != "child_selector":
                trigger_rows = tuple(
                    item
                    for item in trigger_rows
                    if not (
                        item.get("perceived_type") == "heard_speech"
                        and item.get("details", {}).get("speaker_id") == entity_id
                    )
                )
            if not trigger_rows:
                self.runtime.set_cursor(world_id, entity_id, initial_head)
                continue

            triggers = tuple(
                PerceptionRecord(
                    perception_id=item["perception_id"],
                    observer_id=entity_id,
                    source_event_id=item["source_event_id"],
                    perceived_type=item["perceived_type"],
                    details=item["details"],
                    confidence=float(item["confidence"]),
                    observed_at=item.get("observed_at"),
                    source_seq=item.get("source_seq"),
                )
                for item in trigger_rows
            )
            beliefs = tuple(
                BeliefRecord(
                    belief_id=item["belief_id"],
                    holder_id=entity_id,
                    subject_id=item.get("subject_id"),
                    predicate=item["predicate"],
                    object_value=item["object_value"],
                    confidence=float(item["confidence"]),
                    provenance=tuple(ProvenanceRef(**ref) for ref in item["provenance"]),
                    formed_at=item.get("formed_at"),
                )
                for item in self.epistemic.beliefs_for(world_id, entity_id, limit=40)
            )
            memories = tuple(
                MemoryRecord(
                    memory_id=item["memory_id"],
                    owner_id=entity_id,
                    memory_type=item["memory_type"],
                    content=item["content"],
                    confidence=float(item["confidence"]),
                    provenance=tuple(ProvenanceRef(**ref) for ref in item["provenance"]),
                    encoded_at=item.get("encoded_at"),
                    salience=float(item["salience"]),
                )
                for item in self.epistemic.memories_for(world_id, entity_id, limit=40)
            )

            current_state = self.kernel.state(world_id)
            available_actions = (
                (ACTION_SELECT_CHILD_GOAL,)
                if controller_type == "child_selector"
                else (
                    ACTION_MOVE_ENTITY,
                    ACTION_PERFORM_ACTIVITY,
                    ACTION_SUBMIT_WISH,
                    ACTION_UTTER_SPEECH,
                )
            )
            context = DecisionContext(
                agent_id=entity_id,
                batch_id=f"{world_id}:{entity_id}:{cursor + 1}-{initial_head}",
                observed_seq=current_state.seq,
                available_actions=available_actions,
                world_view=self._safe_agent_view(current_state, entity_id),
                perceptions=triggers,
                beliefs=beliefs,
                memories=memories,
                trigger_events=triggers,
            )
            if controller_type == "child_selector":
                controller = ScriptedAIController(self._child_policy(current_state))
            elif controller_type == "delegate":
                controller = DelegateController(self._resident_policy(entity_id))
            else:
                controller = ScriptedAIController(self._resident_policy(entity_id))
            try:
                proposal = controller.propose(context)
            except ControllerUnavailable as exc:
                unavailable += 1
                logger.warning("controller unavailable for %s: %s", entity_id, exc)
                # A bounded event batch is consumed once. The SDK already gets
                # one retry; repeated player clicks must not create an
                # unbounded paid retry loop.
                self.runtime.set_cursor(world_id, entity_id, initial_head)
                continue
            if proposal is None:
                self.runtime.set_cursor(world_id, entity_id, initial_head)
                continue
            before = self.kernel.state(world_id)
            event = self.kernel.submit(world_id, proposal)
            after = self.kernel.state(world_id)
            self._derive_cognition(world_id, event, before, after)
            self.runtime.set_cursor(world_id, entity_id, initial_head)
            committed.append(event)

        if committed:
            suffix = " 另有回应尚未抵达。" if unavailable else ""
            return AdvanceResult(
                tuple(committed),
                f"世界中的 {len(committed)} 个回应已写入历史。{suffix}",
            )
        if unavailable:
            return AdvanceResult((), "有回应尚未抵达；世界历史没有因此被改写。")
        return AdvanceResult((), "没有相关的新事件需要角色回应。")

    def fork(self, participant: Participant, world_id: str, at_seq: int) -> str:
        self._require_active_world(world_id)
        self._membership(participant, world_id)
        child_world_id = f"world_{uuid.uuid4().hex}"
        source_record = self.events.get_world(world_id)
        self.kernel.fork_world(
            world_id,
            child_world_id,
            int(at_seq),
            name=f"{source_record.name} · 分支",
            metadata=thaw(source_record.metadata),
        )
        self.runtime.copy_membership(participant.participant_id, world_id, child_world_id)
        self.epistemic.copy_prefix(world_id, child_world_id, through_seq=int(at_seq))
        state = self.kernel.state(child_world_id)
        for entity in self._agents(state):
            source_cursor = self.runtime.get_cursor(world_id, entity.id)
            # A fork inherits both the event prefix and whether its agents have
            # consumed that prefix. Do not mark pending experience as handled.
            self.runtime.set_cursor(
                child_world_id,
                entity.id,
                min(source_cursor, int(at_seq)),
            )
        return child_world_id

    def _bootstrap_head_seq(self, world_id: str) -> int:
        """Read the epoch's own time origin instead of today's scenario."""

        metadata = self.events.get_world(world_id).metadata
        raw = metadata.get("bootstrap_head_seq", LEGACY_BOOTSTRAP_HEAD_SEQ)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return LEGACY_BOOTSTRAP_HEAD_SEQ
        return value if value >= 1 else LEGACY_BOOTSTRAP_HEAD_SEQ

    def observer_view(self, participant: Participant, world_id: str) -> dict[str, Any]:
        observer_id = self._membership(participant, world_id)
        state = self.kernel.state(world_id)
        observer = state.entity(observer_id)
        world_record = self.events.get_world(world_id)
        epoch = self.runtime.world_epoch(world_id)
        bootstrap_head_seq = self._bootstrap_head_seq(world_id)

        locations = [entity for entity in state.entities.values() if entity.kind == "place"]
        location_payloads: list[dict[str, Any]] = []
        for location in locations:
            occupants = []
            if location.id == observer.location_id:
                occupants = [
                    {"id": entity.id, "name": entity.name, "kind": entity.kind}
                    for entity in state.entities.values()
                    if entity.location_id == location.id and entity.kind in {"resident", "child"}
                ]
            location_payloads.append(
                {
                    "id": location.id,
                    "name": location.name,
                    "description": str(location.attributes.get("description", "")),
                    "occupants": occupants,
                }
            )

        visible_entities = [
            {
                "id": entity.id,
                "name": entity.name,
                "kind": entity.kind,
                "description": str(entity.attributes.get("description", "")),
            }
            for entity in state.entities.values()
            if (
                entity.id == observer.location_id
                or entity.location_id == observer.location_id
            )
            and entity.kind
            not in {
                "activity",
                "utterance",
                "wish",
                "goal",
                "capability",
                "latent_fact",
            }
        ]

        child = state.entity(CHILD["entity_id"])
        child_goal = state.active_goal_for(child.id)
        capabilities = [
            CAPABILITY_LABELS.get(capability_id.removeprefix("capability:"), capability_id)
            for capability_id in sorted(state.capabilities_for(child.id))
        ]
        experiences = self.epistemic.perceptions_for(world_id, observer_id, limit=40)

        return {
            "world": {
                "id": world_id,
                "name": state.name or world_record.name,
                "seq": state.seq,
                "tick": max(0, state.seq - bootstrap_head_seq),
                "is_branch": world_record.parent_world_id is not None,
                "is_archived": bool(epoch and epoch.status == "archived"),
                "current_world_id": self.runtime.default_world(),
            },
            "self": {
                "id": observer.id,
                "name": observer.name,
                "location_id": observer.location_id,
                "location_name": state.entities[observer.location_id].name if observer.location_id else None,
            },
            "child": {
                "id": child.id,
                "name": child.name,
                "description": str(child.attributes.get("description", "")),
                "capabilities": capabilities,
                "goal": str(child_goal.attributes["description"]) if child_goal else None,
            },
            "locations": location_payloads,
            "visible_entities": visible_entities,
            "wishes": [
                {"id": wish_id, "text": str(state.entities[wish_id].attributes["text"])}
                for wish_id in state.wish_ids
            ],
            "experiences": [
                self._present_experience(
                    item,
                    state,
                    observer_id,
                    bootstrap_head_seq=bootstrap_head_seq,
                )
                for item in experiences
            ],
        }

    def observer_snapshot(self, world_id: str | None = None) -> dict[str, Any]:
        """Return all three layers to the isolated, read-only control plane."""

        selected_world_id = world_id or self.runtime.default_world()
        if selected_world_id is None:
            raise WorldNotFound("还没有可观察的世界。")
        record = self.events.get_world(selected_world_id)
        state = self.kernel.state(selected_world_id)
        epoch = self.runtime.world_epoch(selected_world_id)
        history = self.kernel.history(selected_world_id)

        entities = [
            {
                **entity.to_payload(),
                "is_agent": entity.is_agent,
                "controller_binding": CONTROLLER_BINDINGS.get(entity.id),
            }
            for entity in state.entities.values()
            if entity.kind
            not in {"activity", "utterance", "wish", "goal", "capability", "latent_fact"}
        ]
        cognition: dict[str, Any] = {}
        for entity in self._agents(state):
            cognition[entity.id] = {
                "name": entity.name,
                "perceptions": self.epistemic.perceptions_for(
                    selected_world_id, entity.id, limit=200
                ),
                "beliefs": self.epistemic.beliefs_for(
                    selected_world_id, entity.id, limit=200
                ),
                "memories": self.epistemic.memories_for(
                    selected_world_id, entity.id, limit=200
                ),
            }

        child = state.entity(CHILD["entity_id"])
        child_goal = state.active_goal_for(child.id)
        capability_events = [
            event
            for event in history
            if event.event_type == CAPABILITY_UNLOCKED
            and event.payload.get("entity_id") == child.id
        ]
        latent_facts = [
            {
                "fact_id": entity.id,
                "key": entity.attributes.get("key"),
                "value": thaw(entity.attributes.get("value")),
                "scope": entity.attributes.get("scope"),
                "exploration_context_hash": entity.attributes.get(
                    "exploration_context_hash"
                ),
            }
            for entity in state.entities.values()
            if entity.kind == "latent_fact"
        ]
        events = [
            {
                "seq": event.seq,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "actor_id": event.actor_id,
                "payload": thaw(event.payload),
                "occurred_at": event.occurred_at,
                "event_hash": event.event_hash,
                "prev_hash": event.prev_hash,
            }
            for event in history[-120:]
        ]
        epochs = []
        for item in self.runtime.list_epochs():
            try:
                item_record = self.events.get_world(item.world_id)
                seq = self.events.head(item.world_id)
            except WorldNotFound:
                continue
            epochs.append(
                {
                    "world_id": item.world_id,
                    "name": item_record.name,
                    "epoch_index": item.epoch_index,
                    "status": item.status,
                    "previous_world_id": item.previous_world_id,
                    "created_at": item.created_at,
                    "archived_at": item.archived_at,
                    "seq": seq,
                }
            )

        return {
            "world": {
                "id": selected_world_id,
                "name": state.name or record.name,
                "seed": state.seed,
                "seq": state.seq,
                "status": epoch.status if epoch else "untracked",
                "epoch_index": epoch.epoch_index if epoch else None,
                "previous_world_id": epoch.previous_world_id if epoch else None,
                "created_at": record.created_at,
                "is_default": selected_world_id == self.runtime.default_world(),
                "chain_valid": self.events.verify_chain(selected_world_id),
                "metadata": thaw(record.metadata),
            },
            "epochs": epochs,
            "truth": {
                "entities": entities,
                "events": events,
                "event_count": len(history),
                "latent_facts": latent_facts,
                "child": {
                    "id": child.id,
                    "name": child.name,
                    "development": thaw(child.attributes.get("development", {})),
                    "capabilities": [
                        CAPABILITY_LABELS.get(
                            capability_id.removeprefix("capability:"), capability_id
                        )
                        for capability_id in sorted(state.capabilities_for(child.id))
                    ],
                    "goal": (
                        str(child_goal.attributes.get("description", ""))
                        if child_goal
                        else None
                    ),
                    "evidence_event_ids": [event.event_id for event in capability_events],
                },
            },
            "cognition": cognition,
        }

    def reset_default_world(self, world_id: str) -> ResetResult:
        """Archive the current epoch and create a new-seed root world."""

        with self._advance_lock:
            current_world_id = self.runtime.default_world()
            if current_world_id is None:
                raise ValueError("当前没有可以重置的世界。")
            if current_world_id != world_id:
                raise ValueError("默认世界已经变化，请刷新观察台后重试。")
            self._require_active_world(world_id)
            previous_seed = self.events.get_world(world_id).seed
            new_world_id = f"world_{uuid.uuid4().hex}"
            new_seed = uuid.uuid4().hex + uuid.uuid4().hex
            self._bootstrap_world(new_world_id, new_seed)
            epoch = self.runtime.rotate_default_world(world_id, new_world_id)
            return ResetResult(
                archived_world_id=world_id,
                world_id=new_world_id,
                epoch_index=epoch.epoch_index,
                seed_changed=new_seed != previous_seed,
            )

    def _name(self, state: WorldState, entity_id: Any) -> str:
        entity = state.entities.get(str(entity_id))
        return entity.name if entity else "某个存在"

    def _present_experience(
        self,
        item: Mapping[str, Any],
        state: WorldState,
        observer_id: str,
        *,
        bootstrap_head_seq: int,
    ) -> dict[str, Any]:
        details = item["details"]
        kind = details.get("kind")
        summary = "你注意到世界发生了变化。"
        detail = ""
        if kind == "arrival":
            summary = str(details.get("summary") or "你来到了白榆社区商业中心。")
            detail = str(
                details.get("detail")
                or "中庭的灯刚亮不久，许愿池在你面前泛着很浅的光。"
            )
        elif item["perceived_type"] == "heard_speech":
            speaker = self._name(state, details.get("speaker_id"))
            summary = f"{speaker}说了一句话。"
            detail = f"“{details.get('utterance', '')}”"
        elif kind == "activity":
            actor_name = self._name(state, details.get("actor_id"))
            if str(details.get("actor_id")) == observer_id:
                summary = "你做了刚才想做的事。"
            else:
                summary = f"{actor_name}在这里做了一件事。"
            detail = str(details.get("description", ""))
        elif kind == "movement":
            actor_name = self._name(state, details.get("actor_id"))
            destination = self._name(state, details.get("to_location_id"))
            origin = self._name(state, details.get("from_location_id"))
            if details.get("direction") == "self":
                summary = f"你来到了{destination}。"
                detail = f"你离开了{origin}。"
            elif details.get("direction") == "arrived":
                summary = f"{actor_name}来到了这里。"
            else:
                summary = f"{actor_name}离开了这里。"
        elif kind == "wish":
            summary = "一则愿望出现在水面。"
            detail = f"“{details.get('text', '')}”"
        elif kind == "goal":
            summary = "孩子选择了一个目标。"
            detail = str(details.get("description", ""))
        elif kind == "latent_discovery":
            summary = f"你在{details.get('target_name', '那里')}发现了一个细节。"
            detail = str(details.get("description", ""))
        elif kind == "capability":
            summary = f"{self._name(state, details.get('entity_id'))}获得了一项能力。"
            detail = str(details.get("name", ""))
        return {
            "id": item["perception_id"],
            "tick": max(
                0,
                int(item.get("source_seq") or 0) - bootstrap_head_seq,
            ),
            "summary": summary,
            "detail": detail,
        }
