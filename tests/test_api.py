from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app.api as api_module
from app.runtime import NOTICE_VERSION
from app.scenario import (
    BOOTSTRAP_HEAD_SEQ,
    DEFAULT_SOCIAL_LOCATION_ID,
    FUTURE_MODE,
    INITIAL_STATE_MODE,
    LOCATIONS,
    SCENARIO_ID,
    SCENARIO_NAME,
    SCENARIO_THEME,
    STARTING_LOCATION_ID,
)
from app.service import WorldService


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_access_code = api_module.ACCESS_CODE
        self.original_observer_token = api_module.OBSERVER_TOKEN
        api_module.ACCESS_CODE = None
        api_module.OBSERVER_TOKEN = "observer-test-token-1234567890"
        api_module.service = WorldService(Path(self.tempdir.name) / "api.sqlite3")
        self.client_context = TestClient(api_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        api_module.ACCESS_CODE = self.original_access_code
        api_module.OBSERVER_TOKEN = self.original_observer_token
        self.tempdir.cleanup()

    def consent_and_join(self, name: str) -> tuple[str, str, dict[str, str]]:
        consent = self.client.post(
            "/api/reality/consents",
            json={
                "display_name": name,
                "accepted": True,
                "notice_version": NOTICE_VERSION,
            },
        )
        self.assertEqual(201, consent.status_code, consent.text)
        token = consent.json()["consent_token"]
        headers = {"X-Consent-Token": token}
        joined = self.client.post("/api/worlds/default/join", headers=headers, json={})
        self.assertEqual(200, joined.status_code, joined.text)
        return joined.json()["world_id"], joined.json()["entity_id"], headers

    def test_consent_is_reality_layer_gate_and_view_hides_technical_identity(self) -> None:
        blocked = self.client.post("/api/worlds/default/join", json={})
        self.assertEqual(401, blocked.status_code)

        world_id, _, headers = self.consent_and_join("甲")
        response = self.client.get(f"/api/worlds/{world_id}/view", headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        view = response.json()
        serialized = json.dumps(view, ensure_ascii=False)

        self.assertEqual(len(LOCATIONS), len(view["locations"]))
        self.assertEqual(SCENARIO_NAME, view["world"]["name"])
        location_names = {item["name"] for item in view["locations"]}
        self.assertTrue(
            {
                "曙光家属区",
                "厂职工医院",
                "曙光子弟学校",
                "千禧百货商店",
                "蓝鲸餐厅",
                "第二食堂",
            }.issubset(location_names)
        )
        self.assertEqual(3, len(view["child"]["capabilities"]))
        self.assertEqual(0, view["world"]["tick"])
        for forbidden in (
            "policy_id",
            "controller_type",
            "scripted_ai",
            '"seed"',
            "latent_fact",
        ):
            self.assertNotIn(forbidden, serialized)

        oversized = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            content="x" * (64 * 1024 + 1),
        )
        self.assertEqual(413, oversized.status_code)

    def test_optional_invite_code_gates_new_consent_without_entering_the_world(self) -> None:
        api_module.ACCESS_CODE = "invite-code-1234"
        notice = self.client.get("/api/reality/consent-notice")
        self.assertTrue(notice.json()["access_code_required"])
        payload = {
            "display_name": "受邀者",
            "accepted": True,
            "notice_version": NOTICE_VERSION,
        }

        blocked = self.client.post("/api/reality/consents", json=payload)
        self.assertEqual(403, blocked.status_code)
        accepted = self.client.post(
            "/api/reality/consents",
            headers={"X-World-Access-Code": "invite-code-1234"},
            json=payload,
        )
        self.assertEqual(201, accepted.status_code, accepted.text)

    def test_speech_is_visible_locally_but_not_to_remote_observer(self) -> None:
        world_id, _, first_headers = self.consent_and_join("甲")
        second_world, _, second_headers = self.consent_and_join("乙")
        self.assertEqual(world_id, second_world)

        moved = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=second_headers,
            json={
                "type": "move",
                "payload": {"destination_id": DEFAULT_SOCIAL_LOCATION_ID},
            },
        )
        self.assertEqual(201, moved.status_code, moved.text)

        spoken = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=first_headers,
            json={"type": "speak", "payload": {"text": "只在厂前广场听得见"}},
        )
        self.assertEqual(201, spoken.status_code, spoken.text)

        first_view = self.client.get(
            f"/api/worlds/{world_id}/view", headers=first_headers
        ).json()
        second_view = self.client.get(
            f"/api/worlds/{world_id}/view", headers=second_headers
        ).json()
        self.assertIn("只在厂前广场听得见", json.dumps(first_view, ensure_ascii=False))
        self.assertNotIn("只在厂前广场听得见", json.dumps(second_view, ensure_ascii=False))

    def test_wish_child_response_and_scripted_resident_response(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        wish = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={"type": "wish", "payload": {"text": "请帮助大家办一次新世纪联欢会。"}},
        )
        self.assertEqual(201, wish.status_code, wish.text)
        advanced = self.client.post(
            f"/api/worlds/{world_id}/advance", headers=headers, json={}
        )
        self.assertEqual(200, advanced.status_code, advanced.text)
        self.assertEqual(1, len(advanced.json()["event_ids"]))
        view = self.client.get(f"/api/worlds/{world_id}/view", headers=headers).json()
        self.assertEqual("请帮助大家办一次新世纪联欢会。", view["child"]["goal"])

        self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={
                "type": "move",
                "payload": {"destination_id": DEFAULT_SOCIAL_LOCATION_ID},
            },
        )
        self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={"type": "speak", "payload": {"text": "今天的工友套餐是什么？"}},
        )
        response = self.client.post(
            f"/api/worlds/{world_id}/advance", headers=headers, json={}
        )
        self.assertEqual(1, len(response.json()["event_ids"]))
        updated = self.client.get(f"/api/worlds/{world_id}/view", headers=headers).json()
        self.assertIn("我听见了", json.dumps(updated["experiences"], ensure_ascii=False))

    def test_one_natural_language_turn_moves_speaks_and_gets_a_response(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        before = self.client.get(
            f"/api/worlds/{world_id}/view", headers=headers
        ).json()

        response = self.client.post(
            f"/api/worlds/{world_id}/turns",
            headers=headers,
            json={
                "text": "我去蓝鲸餐厅，问问今天推荐什么？",
                "observed_seq": before["world"]["seq"],
                "request_id": "turn-natural-0001",
            },
        )

        self.assertEqual(201, response.status_code, response.text)
        result = response.json()
        self.assertEqual(2, len(result["player_event_ids"]))
        self.assertEqual(1, len(result["response_event_ids"]))
        self.assertTrue(result["feedback"])
        self.assertEqual(
            DEFAULT_SOCIAL_LOCATION_ID,
            result["view"]["self"]["location_id"],
        )
        rendered = json.dumps(result["view"]["experiences"], ensure_ascii=False)
        self.assertIn("今天推荐什么", rendered)
        self.assertIn("我听见了", rendered)

    def test_free_form_activity_is_committed_without_an_action_category(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        before = self.client.get(
            f"/api/worlds/{world_id}/view", headers=headers
        ).json()
        response = self.client.post(
            f"/api/worlds/{world_id}/turns",
            headers=headers,
            json={
                "text": "我在愿望留言台边坐下，写下一段今天的观察。",
                "observed_seq": before["world"]["seq"],
                "request_id": "turn-natural-0002",
            },
        )
        self.assertEqual(201, response.status_code, response.text)
        result = response.json()
        self.assertEqual(1, len(result["player_event_ids"]))
        self.assertIn(
            "我在愿望留言台边坐下，写下一段今天的观察。",
            json.dumps(result["view"]["experiences"], ensure_ascii=False),
        )
        self.assertGreaterEqual(len(result["feedback"]), 1)
        self.assertEqual([], result["view"]["wishes"])

    def test_observer_console_is_authenticated_read_only_and_layered(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        observer_headers = {"X-Observer-Token": api_module.OBSERVER_TOKEN}

        blocked = self.client.get("/api/observer/worlds/current")
        self.assertEqual(401, blocked.status_code)
        snapshot_response = self.client.get(
            "/api/observer/worlds/current", headers=observer_headers
        )
        self.assertEqual(200, snapshot_response.status_code, snapshot_response.text)
        snapshot = snapshot_response.json()
        self.assertEqual(world_id, snapshot["world"]["id"])
        self.assertEqual(SCENARIO_ID, snapshot["world"]["metadata"]["scenario"])
        self.assertEqual(
            SCENARIO_THEME,
            snapshot["world"]["metadata"]["scenario_theme"],
        )
        self.assertEqual(
            BOOTSTRAP_HEAD_SEQ,
            snapshot["world"]["metadata"]["bootstrap_head_seq"],
        )
        self.assertEqual(
            INITIAL_STATE_MODE,
            snapshot["world"]["metadata"]["initial_state_mode"],
        )
        self.assertEqual(FUTURE_MODE, snapshot["world"]["metadata"]["future_mode"])
        self.assertIn("seed", snapshot["world"])
        self.assertIn("truth", snapshot)
        self.assertIn("cognition", snapshot)
        self.assertTrue(snapshot["world"]["chain_valid"])
        before_seq = snapshot["world"]["seq"]

        query = self.client.post(
            "/api/observer/query",
            headers=observer_headers,
            json={"question": "最近发生了什么？", "world_id": world_id},
        )
        self.assertEqual(200, query.status_code, query.text)
        self.assertTrue(query.json()["read_only"])
        after = self.client.get(
            f"/api/observer/worlds/{world_id}", headers=observer_headers
        ).json()
        self.assertEqual(before_seq, after["world"]["seq"])

        player_view = self.client.get(
            f"/api/worlds/{world_id}/view", headers=headers
        ).json()
        self.assertNotIn("seed", player_view["world"])
        self.assertNotIn("cognition", player_view)

    def test_observer_reset_archives_old_epoch_and_uses_a_new_seed(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        observer_headers = {"X-Observer-Token": api_module.OBSERVER_TOKEN}
        old_snapshot = self.client.get(
            "/api/observer/worlds/current", headers=observer_headers
        ).json()

        rejected = self.client.post(
            "/api/observer/reset",
            headers=observer_headers,
            json={"world_id": world_id, "confirmation": "RESET wrong-world"},
        )
        self.assertEqual(422, rejected.status_code)

        reset = self.client.post(
            "/api/observer/reset",
            headers=observer_headers,
            json={
                "world_id": world_id,
                "confirmation": f"RESET {world_id}",
            },
        )
        self.assertEqual(201, reset.status_code, reset.text)
        result = reset.json()
        self.assertEqual(world_id, result["archived_world_id"])
        self.assertTrue(result["seed_changed"])
        self.assertNotEqual(world_id, result["world_id"])

        old_after = self.client.get(
            f"/api/observer/worlds/{world_id}", headers=observer_headers
        ).json()
        new_snapshot = self.client.get(
            "/api/observer/worlds/current", headers=observer_headers
        ).json()
        self.assertEqual("archived", old_after["world"]["status"])
        self.assertEqual(old_snapshot["world"]["seq"], old_after["world"]["seq"])
        self.assertNotEqual(old_snapshot["world"]["seed"], new_snapshot["world"]["seed"])
        self.assertEqual(2, new_snapshot["world"]["epoch_index"])

        old_turn = self.client.post(
            f"/api/worlds/{world_id}/turns",
            headers=headers,
            json={
                "text": "我继续留在这里。",
                "observed_seq": old_after["world"]["seq"],
                "request_id": "archived-turn-0001",
            },
        )
        self.assertEqual(409, old_turn.status_code)
        self.assertEqual("world_archived", old_turn.json()["code"])

        rejoined = self.client.post(
            "/api/worlds/default/join", headers=headers, json={}
        )
        self.assertEqual(200, rejoined.status_code, rejoined.text)
        self.assertEqual(result["world_id"], rejoined.json()["world_id"])

    def test_fork_diverges_without_changing_parent(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        parent = self.client.get(f"/api/worlds/{world_id}/view", headers=headers).json()
        forked = self.client.post(
            f"/api/worlds/{world_id}/forks",
            headers=headers,
            json={"at_seq": parent["world"]["seq"]},
        )
        self.assertEqual(201, forked.status_code, forked.text)
        child_id = forked.json()["world_id"]

        move = self.client.post(
            f"/api/worlds/{child_id}/actions",
            headers=headers,
            json={
                "type": "move",
                "payload": {"destination_id": DEFAULT_SOCIAL_LOCATION_ID},
            },
        )
        self.assertEqual(201, move.status_code, move.text)
        parent_after = self.client.get(f"/api/worlds/{world_id}/view", headers=headers).json()
        child_after = self.client.get(f"/api/worlds/{child_id}/view", headers=headers).json()
        self.assertEqual(STARTING_LOCATION_ID, parent_after["self"]["location_id"])
        self.assertEqual(
            DEFAULT_SOCIAL_LOCATION_ID,
            child_after["self"]["location_id"],
        )
        self.assertEqual(parent["world"]["seq"], parent_after["world"]["seq"])

    def test_fork_preserves_pending_agent_experience(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        wish = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={"type": "wish", "payload": {"text": "请先听听换班广播。"}},
        )
        self.assertEqual(201, wish.status_code, wish.text)
        forked = self.client.post(
            f"/api/worlds/{world_id}/forks",
            headers=headers,
            json={"at_seq": wish.json()["seq"]},
        )
        self.assertEqual(201, forked.status_code, forked.text)
        child_id = forked.json()["world_id"]

        child_advance = self.client.post(
            f"/api/worlds/{child_id}/advance", headers=headers, json={}
        )
        self.assertEqual(200, child_advance.status_code, child_advance.text)
        self.assertEqual(1, len(child_advance.json()["event_ids"]))
        child_view = self.client.get(
            f"/api/worlds/{child_id}/view", headers=headers
        ).json()
        self.assertEqual("请先听听换班广播。", child_view["child"]["goal"])

    def test_exploration_freezes_once_and_repeated_read_does_not_rewrite_truth(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        endpoint = f"/api/worlds/{world_id}/actions"
        first = self.client.post(
            endpoint,
            headers=headers,
            json={
                "type": "explore",
                "payload": {"target_id": "object:wish_pool", "aspect": "history"},
            },
        )
        self.assertEqual(201, first.status_code, first.text)
        first_event_ids = first.json()["event_ids"]
        self.assertEqual(1, len(first_event_ids))

        second = self.client.post(
            endpoint,
            headers=headers,
            json={
                "type": "explore",
                "payload": {"target_id": "object:wish_pool", "aspect": "history"},
            },
        )
        self.assertEqual(201, second.status_code, second.text)
        self.assertEqual([], second.json()["event_ids"])
        view = self.client.get(f"/api/worlds/{world_id}/view", headers=headers).json()
        discoveries = [
            item for item in view["experiences"] if "发现了一个细节" in item["summary"]
        ]
        self.assertEqual(1, len(discoveries))

    def test_latent_rules_never_generate_a_persons_hidden_past(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        response = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={
                "type": "explore",
                "payload": {"target_id": "child:one", "aspect": "history"},
            },
        )

        self.assertEqual(400, response.status_code, response.text)
        self.assertIn("通过相处和交谈了解", response.json()["detail"])
        snapshot = self.client.get(
            "/api/observer/worlds/current",
            headers={"X-Observer-Token": api_module.OBSERVER_TOKEN},
        ).json()
        self.assertEqual([], snapshot["truth"]["latent_facts"])


if __name__ == "__main__":
    unittest.main()
