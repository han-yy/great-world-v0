from __future__ import annotations

from hashlib import sha256
import unittest

from world.child import (
    Capability,
    CapabilityGraph,
    ChildDevelopmentState,
    DevelopmentalPriors,
    Wish,
    WishAssessment,
    WishPool,
    score_wishes,
    select_child_goal,
)
from world.controllers import (
    ActionIntent,
    DecisionContext,
    DelegateController,
    HumanController,
    ScriptedAIController,
)
from world.latent import (
    ExplorationContext,
    LatentContextError,
    LatentRealityResolver,
)
from world.models import ActionProposal, Entity
from world.perception import (
    PerceptionBeliefMemoryPipeline,
    beliefs_from_perception,
    memory_from_belief,
    perceive_event,
)


class PerceptionPipelineTests(unittest.TestCase):
    def test_speech_creates_only_a_belief_about_the_speech_act(self) -> None:
        event = {
            "event_id": "event-7",
            "event_type": "speech.uttered",
            "actor_id": "alice",
            "seq": 7,
            "occurred_at": "2026-08-28T00:00:00Z",
            "payload": {
                "speaker_id": "alice",
                "text": "The locked door is open",
                "target_ids": ["bob"],
                "location_id": "atrium",
            },
        }

        perception = perceive_event(event, "bob", confidence=0.8)
        self.assertIsNotNone(perception)
        assert perception is not None
        self.assertEqual(perception.perceived_type, "heard_speech")
        self.assertEqual(perception.observer_id, "bob")
        self.assertNotIn("truth", perception.details)

        beliefs = beliefs_from_perception(perception)
        self.assertEqual(len(beliefs), 1)
        belief = beliefs[0]
        self.assertEqual((belief.subject_id, belief.predicate), ("alice", "said"))
        self.assertEqual(belief.object_value, "The locked door is open")
        self.assertNotEqual(belief.predicate, "open")
        self.assertEqual(belief.epistemic_layer, "belief")
        self.assertEqual(belief.confidence, 0.8)
        self.assertTrue(any(item.source_id == "event-7" for item in belief.provenance))

        memory = memory_from_belief(belief, salience=0.7)
        self.assertEqual(memory.epistemic_layer, "memory")
        self.assertEqual(memory.confidence, 0.8)
        self.assertEqual(memory.content["predicate"], "said")
        self.assertTrue(any(item.source_id == belief.belief_id for item in memory.provenance))

    def test_projection_is_observer_specific_and_respects_audience(self) -> None:
        event = {
            "event_id": "private-1",
            "event_type": "entity.moved",
            "payload": {
                "entity_id": "alice",
                "from_location_id": "shop",
                "to_location_id": "hall",
                "observable_by": ["bob"],
                "secret": "kernel-only",
            },
        }
        bob = perceive_event(event, "bob")
        eve = perceive_event(event, "eve")
        self.assertIsNotNone(bob)
        self.assertIsNone(eve)
        assert bob is not None
        self.assertNotIn("secret", bob.details)
        self.assertNotIn("observable_by", bob.details)

        public = dict(event)
        public["payload"] = {"entity_id": "alice"}
        bob_public = perceive_event(public, "bob")
        eve_public = perceive_event(public, "eve")
        assert bob_public is not None and eve_public is not None
        self.assertNotEqual(bob_public.perception_id, eve_public.perception_id)

    def test_pipeline_returns_no_cognition_for_an_unobserved_event(self) -> None:
        pipeline = PerceptionBeliefMemoryPipeline()
        update = pipeline.process(
            {"event_id": "hidden-1", "event_type": "entity.moved", "payload": {}},
            "observer",
            observable=False,
        )
        self.assertIsNone(update.perception)
        self.assertEqual(update.beliefs, ())
        self.assertEqual(update.memories, ())


class ControllerTests(unittest.TestCase):
    def context(self, *, batch_id: str = "batch-1", trigger_events=()) -> DecisionContext:
        return DecisionContext(
            agent_id="resident-1",
            batch_id=batch_id,
            observed_seq=12,
            available_actions=("entity.move", "speech.utter"),
            world_view={"location_id": "atrium", "inventory": ["key"]},
            trigger_events=trigger_events,
        )

    def test_human_ai_and_delegate_all_return_action_proposals(self) -> None:
        context = self.context(trigger_events=("event-12",))

        human = HumanController()
        human.submit("entity.move", {"to_location_id": "cafe"})
        human_proposal = human.propose(context)
        self.assertIsInstance(human_proposal, ActionProposal)
        assert human_proposal is not None
        self.assertEqual(human_proposal.actor_id, "resident-1")
        self.assertEqual(human_proposal.observed_seq, 12)

        ai = ScriptedAIController(
            lambda _: ActionIntent("speech.utter", {"text": "hello", "reasoning": "private"})
        )
        ai_proposal = ai.propose(context)
        self.assertIsInstance(ai_proposal, ActionProposal)
        assert ai_proposal is not None
        self.assertNotIn("reasoning", ai_proposal.parameters)

        delegate = DelegateController(
            lambda _: {"action_type": "speech.utter", "parameters": {"text": "still here"}}
        )
        self.assertIsInstance(delegate.propose(context), ActionProposal)

    def test_decision_context_is_a_read_only_agent_view(self) -> None:
        mutable_view = {"coins": 3, "nested": {"door": "closed"}}
        context = DecisionContext(
            agent_id="resident-1",
            batch_id="batch-view",
            observed_seq=1,
            world_view=mutable_view,
        )
        with self.assertRaises(TypeError):
            context.world_view["coins"] = 100  # type: ignore[index]
        with self.assertRaises(TypeError):
            context.world_view["nested"]["door"] = "open"  # type: ignore[index]
        self.assertEqual(mutable_view, {"coins": 3, "nested": {"door": "closed"}})

    def test_decision_context_rejects_raw_truth_events(self) -> None:
        with self.assertRaises(TypeError):
            DecisionContext(
                agent_id="resident-1",
                batch_id="raw-truth",
                observed_seq=3,
                trigger_events=(
                    {
                        "event_id": "event-3",
                        "event_type": "entity.moved",
                        "payload": {"secret": "must not reach controller"},
                    },
                ),
            )

    def test_decision_context_accepts_owner_scoped_serialized_perception(self) -> None:
        serialized = {
            "perception_id": "perception-3",
            "source_event_id": "event-3",
            "source_seq": 3,
            "perceived_type": "heard_speech",
            "details": {"speaker_id": "resident-2", "utterance": "hello"},
            "confidence": 1.0,
            "observed_at": "2026-08-28T00:00:00Z",
        }
        context = DecisionContext(
            agent_id="resident-1",
            batch_id="serialized-perception",
            observed_seq=3,
            trigger_events=(serialized,),
        )
        trigger = context.trigger_events[0]
        self.assertEqual(trigger["perceived_type"], "heard_speech")
        with self.assertRaises(TypeError):
            trigger["details"]["utterance"] = "rewritten"  # type: ignore[index]

    def test_delegate_is_lazy_and_allows_at_most_one_action_per_batch(self) -> None:
        calls: list[str] = []

        def policy(context: DecisionContext) -> ActionIntent:
            calls.append(context.batch_id)
            return ActionIntent(
                "speech.utter",
                {
                    "text": "checked in",
                    "scratchpad": "must never persist",
                    "nested": {"chain_of_thought": "also private", "safe": True},
                },
            )

        delegate = DelegateController(policy)
        empty = self.context(batch_id="empty", trigger_events=())
        self.assertIsNone(delegate.propose(empty))
        self.assertEqual(calls, [])

        triggered = self.context(batch_id="events-13-15", trigger_events=("e13", "e14", "e15"))
        proposal = delegate.propose(triggered)
        self.assertIsInstance(proposal, ActionProposal)
        self.assertIsNone(delegate.propose(triggered))
        self.assertEqual(calls, ["events-13-15"])
        assert proposal is not None
        self.assertNotIn("scratchpad", proposal.parameters)
        self.assertNotIn("chain_of_thought", proposal.parameters["nested"])

    def test_controller_cannot_submit_for_a_different_actor(self) -> None:
        proposal = ActionProposal("entity.move", "somebody-else", {"to_location_id": "cafe"})
        controller = ScriptedAIController(lambda _: proposal)
        with self.assertRaises(ValueError):
            controller.propose(self.context())

    def test_same_agent_batch_has_a_stable_server_proposal_id(self) -> None:
        controller = ScriptedAIController(
            lambda _: ActionIntent("speech.utter", {"text": "same bounded decision"})
        )
        context = self.context(batch_id="stable-batch", trigger_events=("event-12",))
        first = controller.propose(context)
        second = controller.propose(context)
        assert first is not None and second is not None
        self.assertEqual(first.proposal_id, second.proposal_id)


class ChildDevelopmentTests(unittest.TestCase):
    def test_wishes_are_scored_individually_and_order_does_not_change_selection(self) -> None:
        wishes = (
            Wish("wish-a", "resident-a", "Learn how to repair the community door"),
            Wish("wish-b", "resident-b", "Learn how to repair the community lamp"),
        )
        equal = lambda _: WishAssessment(0.8, 0.7, 0.9, 0.8)

        scores = score_wishes(
            wishes,
            child_id="child",
            world_seed="seed-17",
            signal_provider=equal,
        )
        self.assertEqual({score.wish_id for score in scores}, {"wish-a", "wish-b"})
        self.assertEqual(scores[0].total, scores[1].total)
        self.assertNotEqual(scores[0].tie_break, scores[1].tie_break)

        first = select_child_goal(
            wishes,
            child_id="child",
            world_seed="seed-17",
            signal_provider=equal,
        )
        reversed_order = select_child_goal(
            reversed(wishes),
            child_id="child",
            world_seed="seed-17",
            signal_provider=equal,
        )
        self.assertEqual(first, reversed_order)
        assert first is not None
        self.assertEqual(len(first.source_wish_ids), 1)
        self.assertEqual(
            set(first.to_action_parameters()),
            {"goal_id", "child_id", "description", "source_wish_ids", "rationale"},
        )

    def test_wish_can_be_read_from_the_generic_entity_projection(self) -> None:
        entity = Entity(
            id="wish-entity",
            name="A wish",
            kind="wish",
            attributes={"submitted_by": "resident", "text": "Help the cafe"},
        )
        self.assertEqual(
            Wish.from_value(entity),
            Wish("wish-entity", "resident", "Help the cafe"),
        )
        pool = WishPool.from_values([entity])
        self.assertEqual(len(pool), 1)
        self.assertEqual(tuple(pool)[0].wish_id, "wish-entity")

    def test_child_can_reject_all_wishes(self) -> None:
        no = lambda _: WishAssessment(0.0, 0.0, 0.0, 0.0)
        goal = select_child_goal(
            [Wish("wish-harm", "resident", "Do something")],
            child_id="child",
            world_seed="fixed",
            signal_provider=no,
        )
        self.assertIsNone(goal)

    def test_capability_prerequisites_and_non_anatomical_growth(self) -> None:
        graph = CapabilityGraph(
            [
                Capability("read-signs", "Read signs"),
                Capability(
                    "navigate-mall",
                    "Navigate the mall",
                    prerequisite_ids=("read-signs",),
                ),
            ]
        )
        initial = ChildDevelopmentState(child_id="child")
        with self.assertRaises(ValueError):
            initial.unlock(graph, "navigate-mall")
        learned = (
            initial.remember("memory-1")
            .learn("the mall has two floors")
            .practice("reading", 0.4)
            .unlock(graph, "read-signs")
            .unlock(graph, "navigate-mall")
        )
        self.assertEqual(initial.memory_ids, ())
        self.assertIn("memory-1", learned.memory_ids)
        self.assertIn("the mall has two floors", learned.knowledge)
        self.assertEqual(learned.skills["reading"], 0.4)
        self.assertEqual(learned.capability_ids, {"read-signs", "navigate-mall"})

    def test_capability_graph_rejects_cycles(self) -> None:
        with self.assertRaises(ValueError):
            CapabilityGraph(
                [
                    Capability("a", "A", prerequisite_ids=("b",)),
                    Capability("b", "B", prerequisite_ids=("a",)),
                ]
            )


class LatentRealityTests(unittest.TestCase):
    def context(self, exploration_id: str = "explore-1") -> ExplorationContext:
        return ExplorationContext.from_kernel(
            exploration_id=exploration_id,
            observed_seq=9,
            actor_id="resident-1",
            location_id="sealed-door",
            method="inspect",
            parameters={"tool": "flashlight", "angle": 20},
        )

    def test_seed_and_exploration_context_are_replay_deterministic(self) -> None:
        first = LatentRealityResolver("world-seed").preview_resolution(
            "sealed-door.material", self.context()
        )
        replay = LatentRealityResolver("world-seed").preview_resolution(
            "sealed-door.material", self.context()
        )
        different_seed = LatentRealityResolver("other-seed").preview_resolution(
            "sealed-door.material", self.context()
        )
        self.assertEqual(first, replay)
        self.assertNotEqual(first.determinism_key, different_seed.determinism_key)
        self.assertEqual(
            set(first.to_event_payload()),
            {
                "fact_id",
                "key",
                "value",
                "scope",
                "exploration_context_hash",
                "determinism_key",
            },
        )
        expected_key = sha256(
            (
                "world-seed\0world\0sealed-door.material\0"
                + first.exploration_context_hash
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(first.determinism_key, expected_key)
        self.assertNotIn("determinism_key", first.to_action_parameters())

    def test_request_id_and_unrelated_ledger_progress_do_not_change_latent_fact(self) -> None:
        first_context = self.context("before-unrelated-wish")
        later_context = ExplorationContext.from_kernel(
            exploration_id="after-unrelated-wish",
            observed_seq=999,
            actor_id="resident-1",
            location_id="sealed-door",
            method="inspect",
            parameters={"tool": "flashlight", "angle": 20},
        )
        resolver = LatentRealityResolver("world-seed")
        first = resolver.preview_resolution("sealed-door.material", first_context)
        later = resolver.preview_resolution("sealed-door.material", later_context)
        self.assertEqual(first, later)

    def test_first_resolution_freezes_the_fact(self) -> None:
        resolver = LatentRealityResolver("world-seed")
        first = resolver.resolve("sealed-door.material", self.context("first"))
        later = resolver.resolve("sealed-door.material", self.context("later"))
        proposed_later = resolver.propose_resolution(
            "sealed-door.material", self.context("even-later")
        )
        self.assertIs(first, later)
        self.assertIs(first, proposed_later)
        self.assertEqual(len(resolver.frozen_facts()), 1)

    def test_wish_and_goal_fields_are_rejected_at_any_depth(self) -> None:
        with self.assertRaises(LatentContextError):
            ExplorationContext.from_kernel(
                exploration_id="bad-wish",
                observed_seq=1,
                parameters={"wish_id": "please-make-it-gold"},
            )
        with self.assertRaises(LatentContextError):
            ExplorationContext.from_kernel(
                exploration_id="bad-goal",
                observed_seq=1,
                parameters={"nested": {"currentGoal": "find treasure"}},
            )
        with self.assertRaises(LatentContextError):
            LatentRealityResolver("seed").resolve(
                "door",
                {"source": "kernel", "observed_seq": 1, "wish": "open sesame"},
            )

    def test_arbitrary_external_context_is_not_accepted_as_kernel_context(self) -> None:
        with self.assertRaises(LatentContextError):
            LatentRealityResolver("seed").resolve(
                "door",
                {"source": "player", "observed_seq": 1},
            )


if __name__ == "__main__":
    unittest.main()
