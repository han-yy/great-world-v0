from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app.api as api_module
from app.runtime import NOTICE_VERSION
from app.service import WorldService


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_access_code = api_module.ACCESS_CODE
        api_module.ACCESS_CODE = None
        api_module.service = WorldService(Path(self.tempdir.name) / "api.sqlite3")
        self.client_context = TestClient(api_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        api_module.ACCESS_CODE = self.original_access_code
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

        self.assertEqual(5, len(view["locations"]))
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
            json={"type": "move", "payload": {"destination_id": "place:cafe"}},
        )
        self.assertEqual(201, moved.status_code, moved.text)

        spoken = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=first_headers,
            json={"type": "speak", "payload": {"text": "只在中庭听得见"}},
        )
        self.assertEqual(201, spoken.status_code, spoken.text)

        first_view = self.client.get(
            f"/api/worlds/{world_id}/view", headers=first_headers
        ).json()
        second_view = self.client.get(
            f"/api/worlds/{world_id}/view", headers=second_headers
        ).json()
        self.assertIn("只在中庭听得见", json.dumps(first_view, ensure_ascii=False))
        self.assertNotIn("只在中庭听得见", json.dumps(second_view, ensure_ascii=False))

    def test_wish_child_response_and_scripted_resident_response(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        wish = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={"type": "wish", "payload": {"text": "请帮助大家一起学习修理商场的灯。"}},
        )
        self.assertEqual(201, wish.status_code, wish.text)
        advanced = self.client.post(
            f"/api/worlds/{world_id}/advance", headers=headers, json={}
        )
        self.assertEqual(200, advanced.status_code, advanced.text)
        self.assertEqual(1, len(advanced.json()["event_ids"]))
        view = self.client.get(f"/api/worlds/{world_id}/view", headers=headers).json()
        self.assertEqual("请帮助大家一起学习修理商场的灯。", view["child"]["goal"])

        self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={"type": "move", "payload": {"destination_id": "place:cafe"}},
        )
        self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={"type": "speak", "payload": {"text": "这里刚刚开业吗？"}},
        )
        response = self.client.post(
            f"/api/worlds/{world_id}/advance", headers=headers, json={}
        )
        self.assertEqual(1, len(response.json()["event_ids"]))
        updated = self.client.get(f"/api/worlds/{world_id}/view", headers=headers).json()
        self.assertIn("我听见了", json.dumps(updated["experiences"], ensure_ascii=False))

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
            json={"type": "move", "payload": {"destination_id": "place:cafe"}},
        )
        self.assertEqual(201, move.status_code, move.text)
        parent_after = self.client.get(f"/api/worlds/{world_id}/view", headers=headers).json()
        child_after = self.client.get(f"/api/worlds/{child_id}/view", headers=headers).json()
        self.assertEqual("place:atrium", parent_after["self"]["location_id"])
        self.assertEqual("place:cafe", child_after["self"]["location_id"])
        self.assertEqual(parent["world"]["seq"], parent_after["world"]["seq"])

    def test_fork_preserves_pending_agent_experience(self) -> None:
        world_id, _, headers = self.consent_and_join("甲")
        wish = self.client.post(
            f"/api/worlds/{world_id}/actions",
            headers=headers,
            json={"type": "wish", "payload": {"text": "请先观察商场的灯。"}},
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
        self.assertEqual("请先观察商场的灯。", child_view["child"]["goal"])

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


if __name__ == "__main__":
    unittest.main()
