import sqlite3
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import unittest

from world import (
    ACTION_FREEZE_LATENT_FACT,
    ACTION_MOVE_ENTITY,
    ACTION_PERFORM_ACTIVITY,
    ACTION_SELECT_CHILD_GOAL,
    ACTION_SUBMIT_WISH,
    ACTION_UNLOCK_CAPABILITY,
    ACTION_UTTER_SPEECH,
    ActionProposal,
    ConcurrencyConflict,
    Entity,
    InvalidFork,
    ProposalRejected,
    SQLiteEventStore,
    WorldKernel,
)


class KernelTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "world.sqlite3"
        self.store = SQLiteEventStore(self.database_path)
        self.kernel = WorldKernel(self.store)
        self.kernel.create_world(
            "mall", seed="fixed-seed-001", name="New Arc Community Center"
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def bootstrap(self):
        self.kernel.create_entity(
            "mall", Entity("atrium", "Atrium", "place")
        )
        self.kernel.create_entity("mall", Entity("cafe", "Cafe", "place"))
        self.kernel.create_entity(
            "mall",
            Entity(
                "player-1",
                "Lin",
                "resident",
                policy_id="policy:human:player-1",
                location_id="atrium",
            ),
        )
        self.kernel.create_entity(
            "mall",
            Entity(
                "child",
                "The Child",
                "child",
                policy_id="policy:child:v0",
                location_id="atrium",
                attributes={"form": None},
            ),
        )

    def test_entity_is_agent_only_with_nonempty_policy(self):
        object_entity = Entity("bench", "Bench", "furniture", policy_id="  ")
        agent_entity = Entity("resident", "Resident", "resident", policy_id="policy:x")
        self.assertFalse(object_entity.is_agent)
        self.assertTrue(agent_entity.is_agent)
        with self.assertRaises(TypeError):
            object_entity.attributes["secret"] = "changed"

    def test_replay_of_all_v0_event_types(self):
        self.bootstrap()

        move_event = self.kernel.submit(
            "mall",
            ActionProposal(
                ACTION_MOVE_ENTITY,
                "player-1",
                {"entity_id": "player-1", "to_location_id": "cafe"},
            ),
        )
        activity_event = self.kernel.submit(
            "mall",
            ActionProposal(
                ACTION_PERFORM_ACTIVITY,
                "player-1",
                {
                    "activity_id": "activity-1",
                    "description": "Sits by the cafe window and opens a notebook.",
                    "target_ids": ["cafe"],
                },
            ),
        )
        speech_event = self.kernel.submit(
            "mall",
            ActionProposal(
                ACTION_UTTER_SPEECH,
                "player-1",
                {
                    "utterance_id": "utterance-1",
                    "text": "Hello, world.",
                    "target_ids": ["child"],
                },
            ),
        )
        wish_event = self.kernel.submit(
            "mall",
            ActionProposal(
                ACTION_SUBMIT_WISH,
                "player-1",
                {"wish_id": "wish-1", "text": "Help the cafe stay open."},
            ),
        )
        goal_event = self.kernel.submit(
            "mall",
            ActionProposal(
                ACTION_SELECT_CHILD_GOAL,
                "child",
                {
                    "goal_id": "goal-1",
                    "child_id": "child",
                    "description": "Learn why shared places survive.",
                    "source_wish_ids": ["wish-1"],
                    "rationale": "This teaches me about cooperation.",
                },
            ),
        )
        capability_event = self.kernel.submit(
            "mall",
            ActionProposal(
                ACTION_UNLOCK_CAPABILITY,
                "child",
                {
                    "entity_id": "child",
                    "capability_id": "capability.observe_trade",
                    "name": "Observe trade",
                    "description": "Can recognize a completed exchange.",
                    "evidence_event_ids": [speech_event.event_id],
                },
            ),
        )
        latent_event = self.kernel.submit_system(
            "mall",
            ActionProposal(
                ACTION_FREEZE_LATENT_FACT,
                None,
                {
                    "key": "service-tunnel-behind-cafe",
                    "value": {"exists": True, "condition": "locked"},
                    "scope": "mall.infrastructure",
                    "exploration_context_hash": "a" * 64,
                },
            ),
        )

        state = self.kernel.get_state("mall")
        self.assertEqual("cafe", state.entity("player-1").location_id)
        self.assertEqual(("activity-1",), state.activity_ids)
        self.assertEqual(
            "Sits by the cafe window and opens a notebook.",
            state.entity("activity-1").attributes["description"],
        )
        self.assertEqual("Hello, world.", state.entity("utterance-1").attributes["text"])
        self.assertEqual(("wish-1",), state.wish_ids)
        self.assertEqual("goal-1", state.active_goal_for("child").id)
        self.assertIn("capability.observe_trade", state.capabilities_for("child"))
        self.assertEqual(
            {"exists": True, "condition": "locked"},
            dict(state.entity(latent_event.payload["fact_id"]).attributes["value"]),
        )
        self.assertEqual(self.store.head("mall"), state.seq)
        self.assertTrue(self.store.verify_chain("mall"))
        self.assertEqual(move_event.seq + 1, activity_event.seq)
        self.assertEqual(activity_event.seq + 1, speech_event.seq)
        self.assertEqual(wish_event.seq + 1, goal_event.seq)
        self.assertEqual(goal_event.seq + 1, capability_event.seq)

        with self.assertRaisesRegex(ProposalRejected, "already frozen"):
            self.kernel.freeze_latent(
                "mall",
                key="service-tunnel-behind-cafe",
                value={"exists": False},
                scope="mall.infrastructure",
                exploration_context_hash="c" * 64,
            )

    def test_multi_step_plan_is_validated_then_appended_atomically(self):
        self.bootstrap()
        observed = self.store.head("mall")
        committed = self.kernel.submit_many(
            "mall",
            (
                ActionProposal(
                    ACTION_MOVE_ENTITY,
                    "player-1",
                    {"entity_id": "player-1", "to_location_id": "cafe"},
                    observed_seq=observed,
                ),
                ActionProposal(
                    ACTION_UTTER_SPEECH,
                    "player-1",
                    {"text": "What is good today?"},
                    observed_seq=observed + 1,
                ),
            ),
            expected_seq=observed,
        )
        self.assertEqual(2, len(committed))
        self.assertEqual("cafe", committed[1].payload["location_id"])
        self.assertEqual("cafe", self.kernel.state("mall").entity("player-1").location_id)
        self.assertTrue(self.store.verify_chain("mall"))

        head = self.store.head("mall")
        with self.assertRaises(ProposalRejected):
            self.kernel.submit_many(
                "mall",
                (
                    ActionProposal(
                        ACTION_MOVE_ENTITY,
                        "player-1",
                        {"entity_id": "player-1", "to_location_id": "atrium"},
                        observed_seq=head,
                    ),
                    ActionProposal(
                        ACTION_UTTER_SPEECH,
                        "player-1",
                        {"text": "bad", "state": "rewrite"},
                        observed_seq=head + 1,
                    ),
                ),
                expected_seq=head,
            )
        self.assertEqual(head, self.store.head("mall"))
        self.assertEqual("cafe", self.kernel.state("mall").entity("player-1").location_id)

    def test_proposal_cannot_patch_state_or_impersonate_non_agent(self):
        self.bootstrap()
        with self.assertRaisesRegex(ProposalRejected, "capability:move"):
            self.kernel.submit(
                "mall",
                ActionProposal(
                    ACTION_MOVE_ENTITY,
                    "child",
                    {"entity_id": "child", "to_location_id": "cafe"},
                ),
            )
        with self.assertRaisesRegex(ProposalRejected, "unexpected"):
            self.kernel.submit(
                "mall",
                ActionProposal(
                    ACTION_UTTER_SPEECH,
                    "player-1",
                    {"text": "hi", "state": {"seed": "replace-me"}},
                ),
            )

        self.kernel.create_entity("mall", Entity("statue", "Statue", "object"))
        with self.assertRaisesRegex(ProposalRejected, "not an agent"):
            self.kernel.submit(
                "mall",
                ActionProposal(ACTION_UTTER_SPEECH, "statue", {"text": "hi"}),
            )
        with self.assertRaisesRegex(ProposalRejected, "submit_system"):
            self.kernel.submit(
                "mall",
                ActionProposal(
                    ACTION_FREEZE_LATENT_FACT,
                    None,
                    {
                        "key": "x",
                        "value": True,
                        "scope": "test",
                        "exploration_context_hash": "b" * 64,
                    },
                ),
            )

    def test_expected_seq_prevents_lost_updates(self):
        self.bootstrap()
        observed = self.store.head("mall")
        first = ActionProposal(
            ACTION_UTTER_SPEECH,
            "player-1",
            {"text": "first"},
            observed_seq=observed,
        )
        stale = ActionProposal(
            ACTION_UTTER_SPEECH,
            "player-1",
            {"text": "stale"},
            observed_seq=observed,
        )
        self.kernel.submit("mall", first)
        with self.assertRaises(ConcurrencyConflict) as caught:
            self.kernel.submit("mall", stale)
        self.assertEqual(observed, caught.exception.expected_seq)
        self.assertEqual(observed + 1, caught.exception.actual_seq)

    def test_independent_connections_serialize_concurrent_appends(self):
        self.bootstrap()
        observed = self.store.head("mall")
        proposals = tuple(
            ActionProposal(
                ACTION_UTTER_SPEECH,
                "player-1",
                {"text": "parallel-%d" % index},
                observed_seq=observed,
            )
            for index in range(2)
        )

        def commit(proposal):
            try:
                return self.kernel.submit("mall", proposal)
            except ConcurrencyConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(commit, proposals))
        self.assertEqual(1, sum(event is not None for event in results))
        self.assertEqual(observed + 1, self.store.head("mall"))
        self.assertTrue(self.store.verify_chain("mall"))

    def test_proposal_retry_is_idempotent(self):
        self.bootstrap()
        proposal = ActionProposal(
            ACTION_SUBMIT_WISH,
            "player-1",
            {"text": "Keep one quiet room."},
            proposal_id="proposal-fixed",
        )
        first = self.kernel.submit("mall", proposal)
        second = self.kernel.submit("mall", proposal)
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(1, len(self.kernel.get_state("mall").wish_ids))

    def test_fork_resolves_parent_prefix_and_diverges(self):
        self.bootstrap()
        fork_seq = self.store.head("mall")
        parent_prefix_hash = self.store.event_at("mall", fork_seq).event_hash
        self.kernel.fork_world("mall", "mall-alternate", fork_seq)

        parent_event = self.kernel.submit(
            "mall",
            ActionProposal(
                ACTION_MOVE_ENTITY,
                "player-1",
                {"entity_id": "player-1", "to_location_id": "cafe"},
            ),
        )
        child_event = self.kernel.submit(
            "mall-alternate",
            ActionProposal(
                ACTION_UTTER_SPEECH,
                "player-1",
                {"text": "I stayed in the atrium."},
            ),
        )

        child_history = self.store.load_events("mall-alternate")
        self.assertEqual(fork_seq + 1, len(child_history))
        self.assertTrue(all(e.world_id == "mall" for e in child_history[:fork_seq]))
        self.assertEqual("mall-alternate", child_history[-1].world_id)
        self.assertEqual(parent_prefix_hash, child_event.prev_hash)
        self.assertNotEqual(parent_event.event_hash, child_event.event_hash)
        self.assertEqual("cafe", self.kernel.state("mall").entity("player-1").location_id)
        self.assertEqual(
            "atrium",
            self.kernel.state("mall-alternate").entity("player-1").location_id,
        )
        self.assertIn("mall-alternate", self.kernel.state("mall-alternate").entities)
        self.assertTrue(self.store.verify_chain("mall-alternate"))

        with self.assertRaisesRegex(InvalidFork, "fixed world seed"):
            self.kernel.fork_world(
                "mall", "mall-wrong-seed", fork_seq, seed="different-seed"
            )

    def test_events_are_deeply_immutable_in_memory_and_sql(self):
        self.bootstrap()
        event = self.store.event_at("mall", 2)
        with self.assertRaises(TypeError):
            event.payload["entity"] = {}
        with self.assertRaises(TypeError):
            event.payload["entity"]["attributes"]["changed"] = True

        connection = sqlite3.connect(str(self.database_path))
        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "events are immutable"):
                connection.execute(
                    "UPDATE events SET event_type = ? WHERE event_id = ?",
                    ("wish.submitted", event.event_id),
                )
        finally:
            connection.close()

        connection = sqlite3.connect(str(self.database_path))
        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "world records are immutable"):
                connection.execute(
                    "UPDATE worlds SET seed = ? WHERE world_id = ?",
                    ("rewritten", "mall"),
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
