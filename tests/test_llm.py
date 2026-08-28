from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

import httpx
from openai import OpenAI

from app.llm import (
    DeepSeekConfigurationError,
    DeepSeekPolicy,
    DeepSeekSettings,
)
from world.controllers import ControllerUnavailable, DecisionContext
from world.kernel import ACTION_UTTER_SPEECH
from world.perception import (
    BeliefRecord,
    MemoryRecord,
    PerceptionRecord,
    ProvenanceRef,
)


class FakeCompletions:
    def __init__(self, *, output_text: str | None = None, error: Exception | None = None):
        self.output_text = output_text
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self.output_text),
                )
            ]
        )


class FakeClient:
    def __init__(self, completions: FakeCompletions):
        self.chat = SimpleNamespace(completions=completions)


def settings() -> DeepSeekSettings:
    return DeepSeekSettings(
        enabled=True,
        api_key="test-secret-key",
        model="deepseek-v4-pro",
        agent_ids=frozenset({"resident:linqiao"}),
    )


def context(*, actions=(ACTION_UTTER_SPEECH,)) -> DecisionContext:
    provenance = (ProvenanceRef("world_event", "event-secret", "perceived_from"),)
    perception = PerceptionRecord(
        perception_id="perception-secret",
        observer_id="resident:linqiao",
        source_event_id="event-secret",
        perceived_type="heard_speech",
        details={
            "speaker_id": "visitor:1",
            "utterance": "这里刚刚开业吗？",
            "seed": "must-not-leak",
            "policy_id": "must-not-leak",
        },
        confidence=1.0,
        source_seq=17,
    )
    belief = BeliefRecord(
        belief_id="belief-secret",
        holder_id="resident:linqiao",
        subject_id="visitor:1",
        predicate="said",
        object_value="这里刚刚开业吗？",
        confidence=1.0,
        provenance=provenance,
    )
    memory = MemoryRecord(
        memory_id="memory-secret",
        owner_id="resident:linqiao",
        memory_type="episodic",
        content={"predicate": "said", "reasoning": "must-not-leak"},
        confidence=1.0,
        provenance=provenance,
        salience=0.8,
    )
    return DecisionContext(
        agent_id="resident:linqiao",
        batch_id="world-secret:resident:linqiao:17-17",
        observed_seq=17,
        perceptions=(perception,),
        beliefs=(belief,),
        memories=(memory,),
        available_actions=actions,
        world_view={
            "self": {
                "id": "resident:linqiao",
                "name": "林乔",
                "description": "折页咖啡的店主",
                "location_id": "place:cafe",
                "policy_id": "must-not-leak",
            },
            "nearby": [
                {
                    "id": "visitor:1",
                    "name": "来客一",
                    "kind": "resident",
                    "controller_type": "human",
                }
            ],
            "seed": "must-not-leak",
            "seq": 17,
        },
        trigger_events=(perception,),
    )


class DeepSeekSettingsTests(unittest.TestCase):
    def test_enabled_configuration_requires_a_key(self) -> None:
        with self.assertRaises(DeepSeekConfigurationError):
            DeepSeekSettings.from_env({"DEEPSEEK_ENABLED": "true"})

        disabled = DeepSeekSettings.from_env({})
        self.assertFalse(disabled.enabled)
        self.assertEqual("deepseek-v4-pro", disabled.model)

    def test_secret_is_not_in_settings_repr(self) -> None:
        self.assertNotIn("test-secret-key", repr(settings()))

    def test_custom_api_host_requires_explicit_secret_routing_opt_in(self) -> None:
        base = {
            "DEEPSEEK_ENABLED": "true",
            "DEEPSEEK_API_KEY": "test-secret-key",
            "DEEPSEEK_BASE_URL": "https://llm-gateway.example.com",
        }
        with self.assertRaises(DeepSeekConfigurationError):
            DeepSeekSettings.from_env(base)
        allowed = DeepSeekSettings.from_env(
            {**base, "DEEPSEEK_ALLOW_CUSTOM_BASE_URL": "true"}
        )
        self.assertTrue(allowed.allow_custom_base_url)


class DeepSeekPolicyTests(unittest.TestCase):
    def test_valid_candidate_is_bounded_and_provider_input_is_sanitized(self) -> None:
        endpoint = FakeCompletions(
            output_text=json.dumps(
                {
                    "action_type": "speech.utter",
                    "parameters": {"text": "是的，菜单还没有写完。"},
                },
                ensure_ascii=False,
            )
        )
        policy = DeepSeekPolicy(settings(), client=FakeClient(endpoint))

        intent = policy(context())

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(ACTION_UTTER_SPEECH, intent.action_type)
        self.assertEqual("是的，菜单还没有写完。", intent.parameters["text"])
        self.assertEqual(1, len(endpoint.calls))
        request = endpoint.calls[0]
        self.assertEqual("deepseek-v4-pro", request["model"])
        self.assertEqual({"type": "json_object"}, request["response_format"])
        self.assertEqual("disabled", request["extra_body"]["thinking"]["type"])
        self.assertNotIn("reasoning_effort", request)
        serialized = request["messages"][1]["content"]
        self.assertIn("这里刚刚开业吗", serialized)
        for forbidden in (
            "event-secret",
            "perception-secret",
            "belief-secret",
            "memory-secret",
            "world-secret",
            "must-not-leak",
            "policy_id",
            "controller_type",
            "test-secret-key",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_no_available_speech_action_makes_no_request(self) -> None:
        endpoint = FakeCompletions(output_text="{}")
        policy = DeepSeekPolicy(settings(), client=FakeClient(endpoint))
        self.assertIsNone(policy(context(actions=("child.select_goal",))))
        self.assertEqual([], endpoint.calls)

    def test_invalid_or_unauthorized_candidate_fails_closed(self) -> None:
        endpoint = FakeCompletions(
            output_text='{"action_type":"entity.move","parameters":{"text":"go"}}'
        )
        policy = DeepSeekPolicy(settings(), client=FakeClient(endpoint))
        self.assertIsNone(policy(context()))

    def test_transport_failure_is_distinct_from_model_silence(self) -> None:
        endpoint = FakeCompletions(error=TimeoutError("offline"))
        policy = DeepSeekPolicy(settings(), client=FakeClient(endpoint))
        with self.assertRaises(ControllerUnavailable):
            policy(context())

    def test_real_openai_sdk_serializes_the_supported_chat_endpoint(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "chat-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"action_type":"none","parameters":{}}'
                                ),
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = OpenAI(
            api_key="test-secret-key",
            base_url="https://api.deepseek.com",
            http_client=http_client,
        )
        try:
            policy = DeepSeekPolicy(settings(), client=client)
            self.assertIsNone(policy(context()))
        finally:
            client.close()

        self.assertEqual("/chat/completions", captured["path"])
        body = captured["body"]
        self.assertEqual("deepseek-v4-pro", body["model"])
        self.assertEqual({"type": "disabled"}, body["thinking"])
        self.assertEqual({"type": "json_object"}, body["response_format"])
        self.assertNotIn("reasoning_effort", body)
        self.assertTrue(body["user_id"].startswith("agent_"))


if __name__ == "__main__":
    unittest.main()
