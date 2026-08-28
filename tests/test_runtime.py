from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.runtime import NOTICE_VERSION, RuntimeStore


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "runtime.sqlite3"
        self.store = RuntimeStore(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_consent_token_is_required_and_round_trips(self) -> None:
        with self.assertRaises(ValueError):
            self.store.record_consent("测试者", False, NOTICE_VERSION)

        participant, token = self.store.record_consent("测试者", True, NOTICE_VERSION)
        recovered = self.store.participant_for_token(token)

        self.assertEqual(participant, recovered)
        self.assertIsNone(self.store.participant_for_token("not-a-token"))

    def test_membership_and_cursor_are_reality_layer_state(self) -> None:
        participant, _ = self.store.record_consent("测试者", True, NOTICE_VERSION)
        self.store.join(participant.participant_id, "world-a", "visitor-1")
        self.assertEqual(
            "visitor-1", self.store.membership(participant.participant_id, "world-a")
        )

        self.assertEqual(0, self.store.get_cursor("world-a", "resident-1"))
        self.store.set_cursor("world-a", "resident-1", 12)
        self.assertEqual(12, self.store.get_cursor("world-a", "resident-1"))


if __name__ == "__main__":
    unittest.main()
