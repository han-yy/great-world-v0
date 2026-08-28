"""Reality-layer participation records.

This module is deliberately separate from the world event ledger. Consent and
authentication are facts about the real service, not facts characters can
observe inside the fiction.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


NOTICE_VERSION = "v0-2026-08-28.2"
CONSENT_NOTICE = {
    "version": NOTICE_VERSION,
    "summary": (
        "这是一个研究原型。你会进入一个持续演化的虚构社会；"
        "世界内部不会标注每个角色由什么技术支撑。"
    ),
    "points": [
        "环境包含由人工智能生成或控制的角色与内容；真人离线后也可能由自动代理有限托管。",
        (
            "互动和行动会写入事件账本，用于回放、分叉与研究；启用外部语言模型时，"
            "玩家提交的自然语言意图，以及角色自身可观察到的有限上下文，会发送给配置的"
            "模型供应商用于解释或回应。完整账本、隐藏事实、世界种子和其他角色的私人认知不会发送。"
        ),
        "角色可能犯错、虚构、拒绝或产生令人不适的内容；不要把它当作医疗、法律、金融或紧急服务。",
        "世界内的身份模糊只是一种叙事规则；现实层的安全、退出和数据边界始终优先。",
    ],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Participant:
    participant_id: str
    display_name: str
    notice_version: str


class RuntimeStore:
    """Small SQLite store for consent, membership and lazy-agent cursors."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_consents (
                    participant_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    notice_version TEXT NOT NULL,
                    accepted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_memberships (
                    participant_id TEXT NOT NULL,
                    world_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY (participant_id, world_id),
                    UNIQUE (world_id, entity_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_defaults (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    world_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_agent_cursors (
                    world_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (world_id, entity_id)
                );
                """
            )

    def record_consent(
        self, display_name: str, accepted: bool, notice_version: str
    ) -> tuple[Participant, str]:
        cleaned = display_name.strip()
        if not accepted:
            raise ValueError("必须明确同意后才能进入。")
        if notice_version != NOTICE_VERSION:
            raise ValueError("知情说明已经更新，请重新阅读。")
        if not cleaned or len(cleaned) > 80:
            raise ValueError("显示名需要在 1–80 个字符之间。")

        participant = Participant(str(uuid.uuid4()), cleaned, notice_version)
        token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_consents
                    (participant_id, token_hash, display_name, notice_version, accepted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    participant.participant_id,
                    _token_hash(token),
                    participant.display_name,
                    participant.notice_version,
                    _utc_now(),
                ),
            )
        return participant, token

    def participant_for_token(self, token: str | None) -> Participant | None:
        if not token:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT participant_id, display_name, notice_version
                FROM runtime_consents WHERE token_hash = ?
                """,
                (_token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        return Participant(row["participant_id"], row["display_name"], row["notice_version"])

    def set_default_world(self, world_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_defaults (singleton, world_id) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET world_id = excluded.world_id
                """,
                (world_id,),
            )

    def default_world(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT world_id FROM runtime_defaults WHERE singleton = 1"
            ).fetchone()
        return row["world_id"] if row else None

    def membership(self, participant_id: str, world_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT entity_id FROM runtime_memberships
                WHERE participant_id = ? AND world_id = ?
                """,
                (participant_id, world_id),
            ).fetchone()
        return row["entity_id"] if row else None

    def claimed_entities(self, world_id: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT entity_id FROM runtime_memberships WHERE world_id = ?",
                (world_id,),
            ).fetchall()
        return {row["entity_id"] for row in rows}

    def join(self, participant_id: str, world_id: str, entity_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_memberships
                    (participant_id, world_id, entity_id, joined_at)
                VALUES (?, ?, ?, ?)
                """,
                (participant_id, world_id, entity_id, _utc_now()),
            )

    def copy_membership(
        self, participant_id: str, source_world_id: str, child_world_id: str
    ) -> str:
        entity_id = self.membership(participant_id, source_world_id)
        if entity_id is None:
            raise ValueError("你尚未进入源世界。")
        self.join(participant_id, child_world_id, entity_id)
        return entity_id

    def get_cursor(self, world_id: str, entity_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT last_seq FROM runtime_agent_cursors
                WHERE world_id = ? AND entity_id = ?
                """,
                (world_id, entity_id),
            ).fetchone()
        return int(row["last_seq"]) if row else 0

    def set_cursor(self, world_id: str, entity_id: str, last_seq: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_agent_cursors (world_id, entity_id, last_seq)
                VALUES (?, ?, ?)
                ON CONFLICT(world_id, entity_id)
                DO UPDATE SET last_seq = excluded.last_seq
                """,
                (world_id, entity_id, int(last_seq)),
            )
