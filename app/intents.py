"""Observer-scoped interpretation of free-form player intent.

Natural language is never a world event by itself.  Interpreters may only
produce bounded candidates; the service binds the actor and the World Kernel
validates every causal effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from world.kernel import (
    ACTION_MOVE_ENTITY,
    ACTION_PERFORM_ACTIVITY,
    ACTION_SUBMIT_WISH,
    ACTION_UTTER_SPEECH,
)


ACTION_EXPLORE = "experience.explore"
MAX_TURN_STEPS = 4


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class IntentStep:
    """One app-layer candidate in an ordered natural-language plan."""

    action_type: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, str) or not self.action_type.strip():
            raise ValueError("intent action_type is required")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("intent parameters must be a mapping")
        object.__setattr__(self, "action_type", self.action_type.strip())
        object.__setattr__(self, "parameters", _freeze_mapping(self.parameters))


@dataclass(frozen=True)
class PlayerIntentContext:
    """Only what the current player can reasonably use to express an intent."""

    actor_id: str
    observed_seq: int
    self_name: str
    location_id: str | None
    location_name: str | None
    locations: tuple[Mapping[str, Any], ...]
    nearby: tuple[Mapping[str, Any], ...]


class IntentInterpreter(Protocol):
    def __call__(
        self, text: str, context: PlayerIntentContext
    ) -> tuple[IntentStep, ...] | None: ...


_MOVE_WORDS = re.compile(r"(?:去|前往|走到|走进|来到|到|进|回到|过去|逛到)")
_SPEECH_WORDS = re.compile(r"(?:说|问问|问|告诉|喊|叫住|打招呼|聊聊|聊)")
_WISH_WORDS = re.compile(r"(?:许愿(?!池)|愿望|投下愿望|希望孩子|希望这个世界)")
_EXPLORE_WORDS = re.compile(
    r"(?:^|[，。！？,\s])(?:"
    r"我(?:想|要)?(?:看看|看一看|观察|检查|寻找|找找|研究|翻看|留意)|"
    r"看看|看一看|观察一下|检查一下|寻找|找找|翻看"
    r")"
)
_HISTORY_WORDS = re.compile(r"(?:过去|以前|历史|记录|痕迹|曾经)")
_ACCESS_WORDS = re.compile(r"(?:入口|门|打开|进去|通往|锁)")
_QUOTED = re.compile(r"[“\"]([^”\"]+)[”\"]")


class RuleBasedIntentInterpreter:
    """Deterministic parser for clear everyday phrases; complex input returns None."""

    def __call__(
        self, text: str, context: PlayerIntentContext
    ) -> tuple[IntentStep, ...] | None:
        cleaned = text.strip()
        if not cleaned:
            return None

        steps: list[IntentStep] = []
        destination: Mapping[str, Any] | None = None
        for location in context.locations:
            name = str(location.get("name", ""))
            if name and name in cleaned and _MOVE_WORDS.search(cleaned):
                destination = location
                steps.append(
                    IntentStep(
                        ACTION_MOVE_ENTITY,
                        {"to_location_id": str(location["id"])},
                    )
                )
                break

        if _WISH_WORDS.search(cleaned):
            wish_text = re.sub(
                r"^.*?(?:许愿(?!池)|投下愿望|愿望是|我希望(?:孩子|这个世界)?)[:：，,\s]*",
                "",
                cleaned,
                count=1,
            ).strip() or cleaned
            steps.append(IntentStep(ACTION_SUBMIT_WISH, {"text": wish_text}))

        if _EXPLORE_WORDS.search(cleaned):
            target: Mapping[str, Any] | None = None
            candidates = (*context.nearby, *context.locations)
            for entity in candidates:
                name = str(entity.get("name", ""))
                if name and name in cleaned:
                    target = entity
                    break
            if target is None and destination is not None:
                target = destination
            if target is not None:
                aspect = (
                    "history"
                    if _HISTORY_WORDS.search(cleaned)
                    else "access"
                    if _ACCESS_WORDS.search(cleaned)
                    else "condition"
                )
                steps.append(
                    IntentStep(
                        ACTION_EXPLORE,
                        {"target_id": str(target["id"]), "aspect": aspect},
                    )
                )

        if _SPEECH_WORDS.search(cleaned):
            quoted = _QUOTED.search(cleaned)
            speech = quoted.group(1).strip() if quoted else ""
            if not speech:
                match = _SPEECH_WORDS.search(cleaned)
                if match:
                    speech = cleaned[match.end() :].lstrip("：:，,给向和跟 ").strip()
            steps.append(
                IntentStep(ACTION_UTTER_SPEECH, {"text": speech or cleaned})
            )
        elif not steps and len(cleaned) <= 120 and not cleaned.startswith("我"):
            # A bare short phrase such as “早上好” is naturally treated as speech.
            steps.append(IntentStep(ACTION_UTTER_SPEECH, {"text": cleaned}))

        if not steps:
            return None
        return tuple(steps[:MAX_TURN_STEPS])


def free_activity(text: str) -> tuple[IntentStep, ...]:
    """Conservative fallback: record only the actor's own local activity."""

    return (IntentStep(ACTION_PERFORM_ACTIVITY, {"description": text.strip()}),)
