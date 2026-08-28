"""Persistence for private experience, belief and memory streams.

Rows here are not accepted world facts.  Every row keeps provenance back to a
truth-layer event or to an adjacent cognitive record.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _jsonable(item) for key, item in vars(value).items()}
    if hasattr(value, "__slots__"):
        return {
            slot: _jsonable(getattr(value, slot))
            for slot in value.__slots__
            if hasattr(value, slot)
        }
    return value


class EpistemicStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS perceptions (
                    world_id TEXT NOT NULL,
                    perception_id TEXT NOT NULL,
                    observer_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    source_seq INTEGER,
                    perceived_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    observed_at TEXT,
                    PRIMARY KEY (world_id, perception_id)
                );
                CREATE INDEX IF NOT EXISTS perceptions_by_observer
                    ON perceptions(world_id, observer_id, source_seq);

                CREATE TABLE IF NOT EXISTS beliefs (
                    world_id TEXT NOT NULL,
                    belief_id TEXT NOT NULL,
                    holder_id TEXT NOT NULL,
                    subject_id TEXT,
                    predicate TEXT NOT NULL,
                    object_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    provenance_json TEXT NOT NULL,
                    formed_at TEXT,
                    PRIMARY KEY (world_id, belief_id)
                );
                CREATE INDEX IF NOT EXISTS beliefs_by_holder
                    ON beliefs(world_id, holder_id, predicate);

                CREATE TABLE IF NOT EXISTS memories (
                    world_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    salience REAL NOT NULL,
                    provenance_json TEXT NOT NULL,
                    encoded_at TEXT,
                    PRIMARY KEY (world_id, memory_id)
                );
                CREATE INDEX IF NOT EXISTS memories_by_owner
                    ON memories(world_id, owner_id, encoded_at);
                """
            )

    def add_perception(self, world_id: str, record: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO perceptions
                    (world_id, perception_id, observer_id, source_event_id,
                     source_seq, perceived_type, details_json, confidence, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    world_id,
                    record.perception_id,
                    record.observer_id,
                    record.source_event_id,
                    record.source_seq,
                    record.perceived_type,
                    json.dumps(_jsonable(record.details), ensure_ascii=False, sort_keys=True),
                    float(record.confidence),
                    None if record.observed_at is None else str(record.observed_at),
                ),
            )

    def add_beliefs(self, world_id: str, records: Iterable[Any]) -> None:
        with self._connect() as connection:
            for record in records:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO beliefs
                        (world_id, belief_id, holder_id, subject_id, predicate,
                         object_json, confidence, provenance_json, formed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        world_id,
                        record.belief_id,
                        record.holder_id,
                        record.subject_id,
                        record.predicate,
                        json.dumps(_jsonable(record.object_value), ensure_ascii=False, sort_keys=True),
                        float(record.confidence),
                        json.dumps(_jsonable(record.provenance), ensure_ascii=False, sort_keys=True),
                        None if record.formed_at is None else str(record.formed_at),
                    ),
                )

    def add_memories(self, world_id: str, records: Iterable[Any]) -> None:
        with self._connect() as connection:
            for record in records:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO memories
                        (world_id, memory_id, owner_id, memory_type, content_json,
                         confidence, salience, provenance_json, encoded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        world_id,
                        record.memory_id,
                        record.owner_id,
                        record.memory_type,
                        json.dumps(_jsonable(record.content), ensure_ascii=False, sort_keys=True),
                        float(record.confidence),
                        float(record.salience),
                        json.dumps(_jsonable(record.provenance), ensure_ascii=False, sort_keys=True),
                        None if record.encoded_at is None else str(record.encoded_at),
                    ),
                )

    def perceptions_for(
        self, world_id: str, observer_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT perception_id, source_event_id, source_seq, perceived_type,
                       details_json, confidence, observed_at
                FROM perceptions
                WHERE world_id = ? AND observer_id = ?
                ORDER BY COALESCE(source_seq, 0) DESC, rowid DESC
                LIMIT ?
                """,
                (world_id, observer_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [
            {
                "perception_id": row["perception_id"],
                "source_event_id": row["source_event_id"],
                "source_seq": row["source_seq"],
                "perceived_type": row["perceived_type"],
                "details": json.loads(row["details_json"]),
                "confidence": row["confidence"],
                "observed_at": row["observed_at"],
            }
            for row in reversed(rows)
        ]

    def beliefs_for(
        self, world_id: str, holder_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT belief_id, subject_id, predicate, object_json, confidence,
                       provenance_json, formed_at
                FROM beliefs
                WHERE world_id = ? AND holder_id = ?
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (world_id, holder_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [
            {
                "belief_id": row["belief_id"],
                "subject_id": row["subject_id"],
                "predicate": row["predicate"],
                "object_value": json.loads(row["object_json"]),
                "confidence": row["confidence"],
                "provenance": json.loads(row["provenance_json"]),
                "formed_at": row["formed_at"],
            }
            for row in reversed(rows)
        ]

    def memories_for(
        self, world_id: str, owner_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, memory_type, content_json, confidence, salience,
                       provenance_json, encoded_at
                FROM memories
                WHERE world_id = ? AND owner_id = ?
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (world_id, owner_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "memory_type": row["memory_type"],
                "content": json.loads(row["content_json"]),
                "confidence": row["confidence"],
                "salience": row["salience"],
                "provenance": json.loads(row["provenance_json"]),
                "encoded_at": row["encoded_at"],
            }
            for row in reversed(rows)
        ]

    def copy_prefix(
        self, source_world_id: str, child_world_id: str, *, through_seq: int
    ) -> None:
        """Copy private derived streams when the world itself is forked."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO perceptions
                SELECT ?, perception_id, observer_id, source_event_id, source_seq,
                       perceived_type, details_json, confidence, observed_at
                FROM perceptions
                WHERE world_id = ? AND COALESCE(source_seq, 0) <= ?
                """,
                (child_world_id, source_world_id, int(through_seq)),
            )
            # Belief and memory records do not carry source_seq in v0, so copy
            # only those whose provenance mentions a perception in the prefix.
            prefix_ids = {
                row["perception_id"]
                for row in connection.execute(
                    """
                    SELECT perception_id FROM perceptions
                    WHERE world_id = ? AND COALESCE(source_seq, 0) <= ?
                    """,
                    (source_world_id, int(through_seq)),
                ).fetchall()
            }
            if not prefix_ids:
                return
            beliefs = connection.execute(
                "SELECT * FROM beliefs WHERE world_id = ?", (source_world_id,)
            ).fetchall()
            for row in beliefs:
                provenance = row["provenance_json"]
                if any(perception_id in provenance for perception_id in prefix_ids):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO beliefs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (child_world_id, *tuple(row)[1:]),
                    )
            memories = connection.execute(
                "SELECT * FROM memories WHERE world_id = ?", (source_world_id,)
            ).fetchall()
            for row in memories:
                provenance = row["provenance_json"]
                if any(perception_id in provenance for perception_id in prefix_ids):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (child_world_id, *tuple(row)[1:]),
                    )
