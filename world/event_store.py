"""SQLite-backed append-only event store with forked logical histories."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .models import (
    EventDraft,
    GENESIS_HASH,
    WORLD_CREATED,
    WorldEvent,
    WorldRecord,
    canonical_json,
    compute_event_hash,
    new_id,
    thaw,
    utc_now,
)


class EventStoreError(RuntimeError):
    pass


class WorldNotFound(EventStoreError):
    pass


class WorldAlreadyExists(EventStoreError):
    pass


class ConcurrencyConflict(EventStoreError):
    def __init__(self, world_id: str, expected_seq: int, actual_seq: int) -> None:
        self.world_id = world_id
        self.expected_seq = expected_seq
        self.actual_seq = actual_seq
        super().__init__(
            "world %s is at seq %d, expected %d"
            % (world_id, actual_seq, expected_seq)
        )


class DuplicateProposal(EventStoreError):
    pass


class InvalidFork(EventStoreError):
    pass


class EventIntegrityError(EventStoreError):
    pass


class SQLiteEventStore:
    """Persist immutable event streams in one small SQLite database.

    A new connection is opened for every public operation.  ``BEGIN IMMEDIATE``
    serializes the short head-check + append critical section, while WAL mode
    keeps readers responsive.  Forks retain only their own suffix; their parent
    prefix is resolved when events are loaded.
    """

    def __init__(self, database_path: Union[str, Path], busy_timeout_ms: int = 5000) -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError(
                "SQLiteEventStore uses independent connections; provide a file path, not :memory:"
            )
        self.busy_timeout_ms = int(busy_timeout_ms)
        if self.busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        Path(self.database_path).expanduser().resolve().parent.mkdir(
            parents=True, exist_ok=True
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = %d" % self.busy_timeout_ms)
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worlds (
                    world_id TEXT PRIMARY KEY,
                    seed TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    parent_world_id TEXT REFERENCES worlds(world_id),
                    fork_seq INTEGER,
                    metadata_json TEXT NOT NULL,
                    CHECK (
                        (parent_world_id IS NULL AND fork_seq IS NULL)
                        OR
                        (parent_world_id IS NOT NULL AND fork_seq >= 1)
                    )
                );

                CREATE TABLE IF NOT EXISTS events (
                    world_id TEXT NOT NULL REFERENCES worlds(world_id),
                    seq INTEGER NOT NULL CHECK (seq >= 1),
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    actor_id TEXT,
                    proposal_id TEXT,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    PRIMARY KEY (world_id, seq)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS events_world_proposal_unique
                    ON events(world_id, proposal_id)
                    WHERE proposal_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS worlds_parent_idx
                    ON worlds(parent_world_id);

                CREATE TRIGGER IF NOT EXISTS events_are_immutable_update
                BEFORE UPDATE ON events
                BEGIN
                    SELECT RAISE(ABORT, 'events are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS events_are_immutable_delete
                BEFORE DELETE ON events
                BEGIN
                    SELECT RAISE(ABORT, 'events are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS worlds_are_immutable_update
                BEFORE UPDATE ON worlds
                BEGIN
                    SELECT RAISE(ABORT, 'world records are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS worlds_are_immutable_delete
                BEFORE DELETE ON worlds
                BEGIN
                    SELECT RAISE(ABORT, 'world records are immutable');
                END;
                """
            )

    def create_world(
        self,
        world_id: str,
        *,
        seed: str,
        name: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> WorldEvent:
        created_at = utc_now()
        record = WorldRecord(
            world_id=world_id,
            seed=str(seed),
            name=name,
            created_at=created_at,
            metadata=metadata or {},
        )
        draft = EventDraft(
            event_type=WORLD_CREATED,
            payload={
                "world_id": record.world_id,
                "name": record.name,
                "seed": record.seed,
                "metadata": thaw(record.metadata),
            },
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO worlds(
                    world_id, seed, name, created_at, parent_world_id,
                    fork_seq, metadata_json
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    record.world_id,
                    record.seed,
                    record.name,
                    record.created_at,
                    canonical_json(record.metadata),
                ),
            )
            event = self._insert_event(
                connection,
                record.world_id,
                1,
                GENESIS_HASH,
                draft,
                occurred_at=created_at,
            )
            connection.commit()
            return event
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if self._world_exists(connection, world_id):
                raise WorldAlreadyExists("world already exists: %s" % world_id) from exc
            raise EventStoreError("could not create world %s" % world_id) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            parent = self._get_world(connection, parent_world_id)
            if parent is None:
                raise WorldNotFound("world not found: %s" % parent_world_id)
            if not isinstance(fork_seq, int) or fork_seq < 1:
                raise InvalidFork("fork_seq must be a positive integer")
            parent_head = self._head_with_connection(connection, parent)
            if fork_seq > parent_head:
                raise InvalidFork(
                    "cannot fork %s at seq %d; head is %d"
                    % (parent_world_id, fork_seq, parent_head)
                )
            # Loading also verifies that the complete prefix and hash chain exist.
            self._load_events_with_connection(connection, parent_world_id, fork_seq, set())
            if seed is not None and str(seed) != parent.seed:
                raise InvalidFork("a fork must inherit its parent's fixed world seed")
            record = WorldRecord(
                world_id=child_world_id,
                seed=parent.seed,
                name=name or (parent.name + " (fork)"),
                created_at=utc_now(),
                parent_world_id=parent_world_id,
                fork_seq=fork_seq,
                metadata=metadata or {},
            )
            connection.execute(
                """
                INSERT INTO worlds(
                    world_id, seed, name, created_at, parent_world_id,
                    fork_seq, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.world_id,
                    record.seed,
                    record.name,
                    record.created_at,
                    record.parent_world_id,
                    record.fork_seq,
                    canonical_json(record.metadata),
                ),
            )
            connection.commit()
            return record
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if self._world_exists(connection, child_world_id):
                raise WorldAlreadyExists("world already exists: %s" % child_world_id) from exc
            raise EventStoreError("could not fork world %s" % child_world_id) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_world(self, world_id: str) -> WorldRecord:
        with self._connect() as connection:
            record = self._get_world(connection, world_id)
            if record is None:
                raise WorldNotFound("world not found: %s" % world_id)
            return record

    def list_worlds(self) -> Tuple[WorldRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM worlds ORDER BY created_at, world_id"
            ).fetchall()
            return tuple(self._row_to_world(row) for row in rows)

    def head(self, world_id: str) -> int:
        with self._connect() as connection:
            record = self._get_world(connection, world_id)
            if record is None:
                raise WorldNotFound("world not found: %s" % world_id)
            return self._head_with_connection(connection, record)

    def append(
        self,
        world_id: str,
        draft: EventDraft,
        *,
        expected_seq: int,
    ) -> WorldEvent:
        """Atomically append one validated event at ``expected_seq + 1``."""

        if not isinstance(draft, EventDraft):
            raise TypeError("append accepts EventDraft values produced by the kernel")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            record = self._get_world(connection, world_id)
            if record is None:
                raise WorldNotFound("world not found: %s" % world_id)
            current_seq = self._head_with_connection(connection, record)
            if expected_seq != current_seq:
                raise ConcurrencyConflict(world_id, expected_seq, current_seq)
            previous = self._event_at_with_connection(connection, world_id, current_seq)
            if previous is None:
                raise EventIntegrityError(
                    "world %s has no event at its reported head %d"
                    % (world_id, current_seq)
                )
            event = self._insert_event(
                connection,
                world_id,
                current_seq + 1,
                previous.event_hash,
                draft,
            )
            connection.commit()
            return event
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if draft.proposal_id and self._find_by_proposal_with_connection(
                connection, world_id, draft.proposal_id
            ):
                raise DuplicateProposal(
                    "proposal already committed: %s" % draft.proposal_id
                ) from exc
            raise EventStoreError("could not append to world %s" % world_id) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_events(
        self, world_id: str, upto_seq: Optional[int] = None
    ) -> Tuple[WorldEvent, ...]:
        with self._connect() as connection:
            return tuple(
                self._load_events_with_connection(
                    connection, world_id, upto_seq, set()
                )
            )

    def event_at(self, world_id: str, seq: int) -> WorldEvent:
        if not isinstance(seq, int) or seq < 1:
            raise ValueError("seq must be a positive integer")
        with self._connect() as connection:
            event = self._event_at_with_connection(connection, world_id, seq)
            if event is None:
                raise EventStoreError("event not found: %s@%d" % (world_id, seq))
            return event

    def find_by_proposal(
        self, world_id: str, proposal_id: str
    ) -> Optional[WorldEvent]:
        with self._connect() as connection:
            if self._get_world(connection, world_id) is None:
                raise WorldNotFound("world not found: %s" % world_id)
            return self._find_by_proposal_with_connection(
                connection, world_id, proposal_id
            )

    def verify_chain(self, world_id: str) -> bool:
        # load_events performs the full verification and raises on corruption.
        self.load_events(world_id)
        return True

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        seq: int,
        prev_hash: str,
        draft: EventDraft,
        *,
        occurred_at: Optional[str] = None,
    ) -> WorldEvent:
        event_id = new_id("event")
        timestamp = occurred_at or utc_now()
        event_hash = compute_event_hash(
            world_id=world_id,
            seq=seq,
            event_id=event_id,
            event_type=draft.event_type,
            payload=draft.payload,
            occurred_at=timestamp,
            actor_id=draft.actor_id,
            proposal_id=draft.proposal_id,
            prev_hash=prev_hash,
        )
        event = WorldEvent(
            world_id=world_id,
            seq=seq,
            event_id=event_id,
            event_type=draft.event_type,
            payload=draft.payload,
            occurred_at=timestamp,
            actor_id=draft.actor_id,
            proposal_id=draft.proposal_id,
            prev_hash=prev_hash,
            event_hash=event_hash,
        )
        connection.execute(
            """
            INSERT INTO events(
                world_id, seq, event_id, event_type, payload_json,
                occurred_at, actor_id, proposal_id, prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.world_id,
                event.seq,
                event.event_id,
                event.event_type,
                canonical_json(event.payload),
                event.occurred_at,
                event.actor_id,
                event.proposal_id,
                event.prev_hash,
                event.event_hash,
            ),
        )
        return event

    def _get_world(
        self, connection: sqlite3.Connection, world_id: str
    ) -> Optional[WorldRecord]:
        row = connection.execute(
            "SELECT * FROM worlds WHERE world_id = ?", (world_id,)
        ).fetchone()
        return self._row_to_world(row) if row is not None else None

    def _world_exists(self, connection: sqlite3.Connection, world_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM worlds WHERE world_id = ?", (world_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _row_to_world(row: sqlite3.Row) -> WorldRecord:
        return WorldRecord(
            world_id=row["world_id"],
            seed=row["seed"],
            name=row["name"],
            created_at=row["created_at"],
            parent_world_id=row["parent_world_id"],
            fork_seq=row["fork_seq"],
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> WorldEvent:
        return WorldEvent(
            world_id=row["world_id"],
            seq=row["seq"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            occurred_at=row["occurred_at"],
            actor_id=row["actor_id"],
            proposal_id=row["proposal_id"],
            prev_hash=row["prev_hash"],
            event_hash=row["event_hash"],
        )

    def _head_with_connection(
        self, connection: sqlite3.Connection, record: WorldRecord
    ) -> int:
        row = connection.execute(
            "SELECT MAX(seq) AS max_seq FROM events WHERE world_id = ?",
            (record.world_id,),
        ).fetchone()
        local_max = row["max_seq"]
        if local_max is not None:
            return int(local_max)
        if record.fork_seq is not None:
            return record.fork_seq
        raise EventIntegrityError("root world has no world.created event: %s" % record.world_id)

    def _event_at_with_connection(
        self, connection: sqlite3.Connection, world_id: str, seq: int
    ) -> Optional[WorldEvent]:
        events = self._load_events_with_connection(
            connection, world_id, seq, set()
        )
        if not events or events[-1].seq != seq:
            return None
        return events[-1]

    def _find_by_proposal_with_connection(
        self, connection: sqlite3.Connection, world_id: str, proposal_id: str
    ) -> Optional[WorldEvent]:
        for event in self._load_events_with_connection(
            connection, world_id, None, set()
        ):
            if event.proposal_id == proposal_id:
                return event
        return None

    def _load_events_with_connection(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        upto_seq: Optional[int],
        ancestry: set,
    ) -> List[WorldEvent]:
        if world_id in ancestry:
            raise EventIntegrityError("cycle detected in world ancestry at %s" % world_id)
        ancestry = set(ancestry)
        ancestry.add(world_id)
        record = self._get_world(connection, world_id)
        if record is None:
            raise WorldNotFound("world not found: %s" % world_id)
        head = self._head_with_connection(connection, record)
        limit = head if upto_seq is None else min(upto_seq, head)
        if limit < 1:
            return []

        events: List[WorldEvent] = []
        local_start = 1
        if record.parent_world_id is not None:
            assert record.fork_seq is not None
            prefix_limit = min(limit, record.fork_seq)
            events.extend(
                self._load_events_with_connection(
                    connection,
                    record.parent_world_id,
                    prefix_limit,
                    ancestry,
                )
            )
            local_start = record.fork_seq + 1

        if limit >= local_start:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE world_id = ? AND seq >= ? AND seq <= ?
                ORDER BY seq
                """,
                (world_id, local_start, limit),
            ).fetchall()
            events.extend(self._row_to_event(row) for row in rows)

        self._verify_loaded_history(world_id, events, limit)
        return events

    @staticmethod
    def _verify_loaded_history(
        logical_world_id: str, events: Sequence[WorldEvent], expected_limit: int
    ) -> None:
        if len(events) != expected_limit:
            raise EventIntegrityError(
                "world %s history has gaps: expected %d events, loaded %d"
                % (logical_world_id, expected_limit, len(events))
            )
        previous_hash = GENESIS_HASH
        for expected_seq, event in enumerate(events, start=1):
            if event.seq != expected_seq:
                raise EventIntegrityError(
                    "world %s expected seq %d but loaded %d"
                    % (logical_world_id, expected_seq, event.seq)
                )
            if event.prev_hash != previous_hash:
                raise EventIntegrityError(
                    "hash-chain break in world %s at seq %d"
                    % (logical_world_id, event.seq)
                )
            if not event.verify_hash():
                raise EventIntegrityError(
                    "event hash mismatch in world %s at seq %d"
                    % (logical_world_id, event.seq)
                )
            previous_hash = event.event_hash


# A shorter name for callers that do not care about the storage implementation.
EventStore = SQLiteEventStore
