from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.runtime import NOTICE_VERSION
from app.service import WorldService
from world.controllers import ActionIntent, ControllerUnavailable, DecisionContext
from world.event_store import ConcurrencyConflict
from world.kernel import ACTION_UTTER_SPEECH


class RecordingPolicy:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.contexts: list[DecisionContext] = []

    def __call__(self, context: DecisionContext):
        self.contexts.append(context)
        if self.fail:
            raise ControllerUnavailable("test outage")
        return ActionIntent(ACTION_UTTER_SPEECH, {"text": "菜单确实还在慢慢长出来。"})


class MutatingPolicy(RecordingPolicy):
    def __init__(self):
        super().__init__()
        self.mutate = None

    def __call__(self, context: DecisionContext):
        self.contexts.append(context)
        assert self.mutate is not None
        self.mutate()
        return ActionIntent(ACTION_UTTER_SPEECH, {"text": "这个回应已经过时。"})


class ServiceLLMTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def service_with(self, policy: RecordingPolicy) -> tuple[WorldService, object, str]:
        service = WorldService(
            Path(self.tempdir.name) / "world.sqlite3",
            llm_policy=policy,
            llm_agent_ids={"resident:linqiao"},
        )
        service.initialize()
        participant, _ = service.runtime.record_consent("测试者", True, NOTICE_VERSION)
        world_id, _ = service.join_default_world(participant)
        service.submit_action(
            participant,
            world_id,
            "move",
            {"destination_id": "place:cafe"},
        )
        service.submit_action(
            participant,
            world_id,
            "speak",
            {"text": "这里刚刚开业吗？"},
        )
        return service, participant, world_id

    def test_one_observed_batch_calls_llm_once_and_self_speech_does_not_loop(self) -> None:
        policy = RecordingPolicy()
        service, participant, world_id = self.service_with(policy)

        first = service.advance(participant, world_id)

        self.assertEqual(1, len(policy.contexts))
        self.assertEqual(1, len(first.events))
        seen = policy.contexts[0]
        self.assertEqual((ACTION_UTTER_SPEECH,), seen.available_actions)
        self.assertTrue(seen.beliefs)
        self.assertTrue(seen.memories)
        self.assertTrue(all(item.holder_id == "resident:linqiao" for item in seen.beliefs))
        self.assertTrue(all(item.owner_id == "resident:linqiao" for item in seen.memories))

        second = service.advance(participant, world_id)
        self.assertEqual(1, len(policy.contexts))
        self.assertEqual((), second.events)

    def test_provider_failure_consumes_one_batch_without_writing_an_event(self) -> None:
        policy = RecordingPolicy(fail=True)
        service, participant, world_id = self.service_with(policy)
        before = service.kernel.state(world_id).seq

        first = service.advance(participant, world_id)

        self.assertEqual((), first.events)
        self.assertEqual(before, service.kernel.state(world_id).seq)
        self.assertEqual(1, len(policy.contexts))

        second = service.advance(participant, world_id)
        self.assertEqual((), second.events)
        self.assertEqual(1, len(policy.contexts))

    def test_concurrent_world_change_rejects_stale_llm_proposal_without_cursor_rebase(self) -> None:
        policy = MutatingPolicy()
        service, participant, world_id = self.service_with(policy)
        cursor_before = service.runtime.get_cursor(world_id, "resident:linqiao")
        policy.mutate = lambda: service.submit_action(
            participant,
            world_id,
            "speak",
            {"text": "在模型回答前，世界已经继续了。"},
        )

        with self.assertRaises(ConcurrencyConflict):
            service.advance(participant, world_id)

        self.assertEqual(
            cursor_before,
            service.runtime.get_cursor(world_id, "resident:linqiao"),
        )
        utterances = service.kernel.state(world_id).utterance_ids
        self.assertEqual(2, len(utterances))


if __name__ == "__main__":
    unittest.main()
