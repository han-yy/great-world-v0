"""DeepSeek adapter for observer-scoped action proposals.

The provider receives only a ``DecisionContext`` projection and can return at
most one candidate.  It never receives the truth-layer state and never writes
to the world ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from app.intents import (
    ACTION_EXPLORE,
    MAX_TURN_STEPS,
    IntentStep,
    PlayerIntentContext,
)
from world.controllers import ActionIntent, ControllerUnavailable, DecisionContext
from world.kernel import (
    ACTION_MOVE_ENTITY,
    ACTION_PERFORM_ACTIVITY,
    ACTION_SUBMIT_WISH,
    ACTION_UTTER_SPEECH,
)


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_AGENT_IDS = frozenset({"resident:linqiao"})
_REASONING_EFFORTS = frozenset({"none", "low", "high", "max"})
_FORBIDDEN_PROVIDER_KEYS = frozenset(
    {
        "analysis",
        "chain_of_thought",
        "controller_type",
        "event_hash",
        "event_type",
        "hidden_reasoning",
        "latent",
        "payload",
        "policy_id",
        "prev_hash",
        "reasoning",
        "scratchpad",
        "seed",
        "world_id",
        "world_seed",
    }
)


class _ChatCompletionsEndpoint(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _ChatNamespace(Protocol):
    completions: _ChatCompletionsEndpoint


class _ChatClient(Protocol):
    chat: _ChatNamespace


class DeepSeekConfigurationError(ValueError):
    """Raised when server-side DeepSeek configuration is unsafe or incomplete."""


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise DeepSeekConfigurationError("DEEPSEEK_ENABLED 必须是 true 或 false。")


def _parse_int(
    raw: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise DeepSeekConfigurationError(f"{name} 必须是整数。") from exc
    if not minimum <= value <= maximum:
        raise DeepSeekConfigurationError(
            f"{name} 必须在 {minimum} 到 {maximum} 之间。"
        )
    return value


def _parse_float(
    raw: str | None,
    *,
    default: float,
    minimum: float,
    maximum: float,
    name: str,
) -> float:
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise DeepSeekConfigurationError(f"{name} 必须是数字。") from exc
    if not minimum <= value <= maximum:
        raise DeepSeekConfigurationError(
            f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间。"
        )
    return value


@dataclass(frozen=True)
class DeepSeekSettings:
    """Server-side configuration; the secret is excluded from repr output."""

    enabled: bool = False
    api_key: str | None = field(default=None, repr=False)
    model: str = DEFAULT_DEEPSEEK_MODEL
    base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    agent_ids: frozenset[str] = DEFAULT_DEEPSEEK_AGENT_IDS
    reasoning_effort: str = "none"
    timeout_seconds: float = 20.0
    max_output_tokens: int = 300
    allow_custom_base_url: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DeepSeekSettings":
        source = os.environ if environ is None else environ
        enabled = _parse_bool(source.get("DEEPSEEK_ENABLED"))
        api_key = source.get("DEEPSEEK_API_KEY", "").strip() or None
        model = source.get("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip()
        base_url = source.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).strip()
        allow_custom_base_url = _parse_bool(
            source.get("DEEPSEEK_ALLOW_CUSTOM_BASE_URL")
        )
        agent_ids = frozenset(
            item.strip()
            for item in source.get(
                "DEEPSEEK_AGENT_IDS", ",".join(sorted(DEFAULT_DEEPSEEK_AGENT_IDS))
            ).split(",")
            if item.strip()
        )
        reasoning_effort = source.get("DEEPSEEK_REASONING_EFFORT", "none").strip()
        timeout_seconds = _parse_float(
            source.get("DEEPSEEK_TIMEOUT_SECONDS"),
            default=20.0,
            minimum=1.0,
            maximum=120.0,
            name="DEEPSEEK_TIMEOUT_SECONDS",
        )
        max_output_tokens = _parse_int(
            source.get("DEEPSEEK_MAX_OUTPUT_TOKENS"),
            default=300,
            minimum=64,
            maximum=2000,
            name="DEEPSEEK_MAX_OUTPUT_TOKENS",
        )

        if enabled and api_key is None:
            raise DeepSeekConfigurationError(
                "DEEPSEEK_ENABLED=true 时必须配置 DEEPSEEK_API_KEY。"
            )
        if not model:
            raise DeepSeekConfigurationError("DEEPSEEK_MODEL 不能为空。")
        parsed_url = urlparse(base_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise DeepSeekConfigurationError("DEEPSEEK_BASE_URL 必须是有效的 HTTPS 地址。")
        if (
            parsed_url.hostname != "api.deepseek.com"
            and not allow_custom_base_url
        ):
            raise DeepSeekConfigurationError(
                "自定义 DEEPSEEK_BASE_URL 会把 API key 和上下文发送给其他主机；"
                "如确有需要，必须显式设置 DEEPSEEK_ALLOW_CUSTOM_BASE_URL=true。"
            )
        if enabled and not agent_ids:
            raise DeepSeekConfigurationError(
                "DEEPSEEK_ENABLED=true 时 DEEPSEEK_AGENT_IDS 不能为空。"
            )
        if reasoning_effort not in _REASONING_EFFORTS:
            raise DeepSeekConfigurationError(
                "DEEPSEEK_REASONING_EFFORT 不是受支持的档位。"
            )

        return cls(
            enabled=enabled,
            api_key=api_key,
            model=model,
            base_url=base_url.rstrip("/"),
            agent_ids=agent_ids,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            allow_custom_base_url=allow_custom_base_url,
        )


_RESIDENT_INSTRUCTIONS = """
你是一个持续世界中的居民，不是旁白，也不是系统管理员。
你只能依据输入 JSON 中这个居民自己的观察、信念、记忆和附近事物行动。
听到的话只代表某人说过这些话，不代表内容为真。
除非输入中的已提交事实明确支持，否则不要把日常细节解释成谜题、超自然现象、阴谋或异常空间；普通矛盾先按普通人的生产与生活理解。
不要推断或提及玩家、NPC、人工智能、API、提示词、世界种子或隐藏后台。
如果自然的选择是保持沉默，返回 action_type 为 none。
否则只提出一个当下、局部、可执行的候选。不要替世界宣布结果，不要输出分析、理由或思维过程。
只输出一个 JSON 对象，不要使用 Markdown。JSON 必须严格采用以下格式之一：
{"action_type":"speech.utter","parameters":{"text":"一句不超过500字的当场发言"}}
{"action_type":"entity.move","parameters":{"to_location_id":"输入中存在的地点 id"}}
{"action_type":"activity.perform","parameters":{"description":"自己的当场活动","target_ids":[]}}
{"action_type":"wish.submit","parameters":{"text":"留在愿望留言台的愿望"}}
{"action_type":"none","parameters":{}}
""".strip()

_INTENT_INSTRUCTIONS = """
你只负责把一个人用自然语言表达的当下意图，解释成有限的世界候选；你不是旁白，不能宣布结果。
只能依据输入 JSON 中此人的当前位置、附近存在和已知地点。不得补写隐藏事实、远处人物、资源、成功结果或技术身份。
可以把一句复合表达拆成最多四个按顺序发生的步骤。自由表达不等于任意改写世界；无法归入具体效果时，用 activity 原样保留此人自己的局部活动。
只输出 JSON，不要输出分析、理由、信心或 Markdown。格式：
{"steps":[
  {"kind":"move","destination_id":"地点 id"},
  {"kind":"speak","text":"说出的内容"},
  {"kind":"wish","text":"愿望内容"},
  {"kind":"explore","target_id":"可观察对象或已知地点 id","aspect":"condition|history|access"},
  {"kind":"activity","description":"此人自己的当场活动","target_ids":[]}
]}
只保留实际需要的步骤；不要返回 actor_id、world_id、事件、事实值或任何额外字段。
""".strip()


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _plain(item)
            for key, item in value.items()
            if str(key).casefold() not in _FORBIDDEN_PROVIDER_KEYS
            and not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def context_for_provider(context: DecisionContext) -> dict[str, Any]:
    """Build a bounded projection with no truth-event ids or provenance ids."""

    raw_self = context.world_view.get("self", {})
    self_view = (
        {
            key: _plain(raw_self[key])
            for key in ("id", "name", "description", "location_id")
            if key in raw_self
        }
        if isinstance(raw_self, Mapping)
        else {}
    )
    raw_nearby = context.world_view.get("nearby", ())
    nearby_view = []
    if isinstance(raw_nearby, (list, tuple)):
        for entity in raw_nearby[:30]:
            if not isinstance(entity, Mapping):
                continue
            nearby_view.append(
                {
                    key: _plain(entity[key])
                    for key in ("id", "name", "kind")
                    if key in entity
                }
            )
    raw_locations = context.world_view.get("locations", ())
    location_view = []
    if isinstance(raw_locations, (list, tuple)):
        for location in raw_locations[:30]:
            if not isinstance(location, Mapping):
                continue
            location_view.append(
                {
                    key: _plain(location[key])
                    for key in ("id", "name", "description")
                    if key in location
                }
            )
    visible_world = {
        "self": self_view,
        "nearby": nearby_view,
        "locations": location_view,
    }
    perceptions = [
        {
            "type": item.perceived_type,
            "details": _plain(item.details),
            "confidence": item.confidence,
        }
        for item in context.perceptions[-20:]
    ]
    beliefs = [
        {
            "subject_id": item.subject_id,
            "predicate": item.predicate,
            "object": _plain(item.object_value),
            "confidence": item.confidence,
        }
        for item in context.beliefs[-20:]
    ]
    memories = [
        {
            "type": item.memory_type,
            "content": _plain(item.content),
            "confidence": item.confidence,
            "salience": item.salience,
        }
        for item in context.memories[-20:]
    ]
    return {
        "world_view": visible_world,
        "new_perceptions": perceptions,
        "beliefs": beliefs,
        "memories": memories,
        "available_actions": [
            action
            for action in context.available_actions
            if action
            in {
                ACTION_MOVE_ENTITY,
                ACTION_PERFORM_ACTIVITY,
                ACTION_SUBMIT_WISH,
                ACTION_UTTER_SPEECH,
            }
        ],
    }


def _chat_response_text(response: Any) -> str | None:
    if isinstance(response, Mapping):
        choices = response.get("choices", ())
    else:
        choices = getattr(response, "choices", ())
    if not choices or len(choices) != 1:
        return None
    choice = choices[0]
    finish_reason = (
        choice.get("finish_reason")
        if isinstance(choice, Mapping)
        else getattr(choice, "finish_reason", None)
    )
    if finish_reason not in {None, "stop"}:
        return None
    message = (
        choice.get("message")
        if isinstance(choice, Mapping)
        else getattr(choice, "message", None)
    )
    if isinstance(message, Mapping):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        return None
    return content.strip()


def _candidate_from_text(text: str, context: DecisionContext) -> ActionIntent | None:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ControllerUnavailable("模型没有返回可解析的行动候选。") from exc
    if not isinstance(raw, Mapping) or set(raw) != {"action_type", "parameters"}:
        return None
    action_type = raw.get("action_type")
    parameters = raw.get("parameters")
    if action_type == "none":
        return None
    if action_type not in context.available_actions or not isinstance(parameters, Mapping):
        return None

    if action_type == ACTION_MOVE_ENTITY:
        if set(parameters) != {"to_location_id"}:
            return None
        destination_id = parameters.get("to_location_id")
        locations = context.world_view.get("locations", ())
        allowed = {
            str(item.get("id"))
            for item in locations
            if isinstance(item, Mapping) and item.get("id")
        }
        if not isinstance(destination_id, str) or destination_id not in allowed:
            return None
        return ActionIntent(
            ACTION_MOVE_ENTITY,
            {"entity_id": context.agent_id, "to_location_id": destination_id},
        )

    if action_type == ACTION_PERFORM_ACTIVITY:
        if not {"description"}.issubset(parameters) or not set(parameters).issubset(
            {"description", "target_ids"}
        ):
            return None
        description = parameters.get("description")
        target_ids = parameters.get("target_ids", ())
        if not isinstance(description, str) or not description.strip():
            return None
        if len(description.strip()) > 500 or not isinstance(target_ids, (list, tuple)):
            return None
        nearby = context.world_view.get("nearby", ())
        allowed_targets = {
            str(item.get("id"))
            for item in nearby
            if isinstance(item, Mapping) and item.get("id")
        }
        if any(not isinstance(item, str) or item not in allowed_targets for item in target_ids):
            return None
        return ActionIntent(
            ACTION_PERFORM_ACTIVITY,
            {"description": description.strip(), "target_ids": list(target_ids)},
        )

    if action_type in {ACTION_UTTER_SPEECH, ACTION_SUBMIT_WISH}:
        if set(parameters) != {"text"}:
            return None
        text_value = parameters.get("text")
        if not isinstance(text_value, str):
            return None
        cleaned = text_value.strip()
        if not cleaned or len(cleaned) > 500:
            return None
        return ActionIntent(action_type, {"text": cleaned})

    return None


class DeepSeekPolicy:
    """Callable policy backed by DeepSeek's OpenAI-compatible Chat API."""

    def __init__(
        self,
        settings: DeepSeekSettings,
        *,
        client: _ChatClient | None = None,
    ) -> None:
        if not settings.enabled:
            raise DeepSeekConfigurationError("不能为未启用的配置创建 DeepSeekPolicy。")
        if settings.api_key is None:
            raise DeepSeekConfigurationError("DeepSeek API Key 缺失。")
        self.settings = settings
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
                max_retries=1,
            )
        self._client = client

    def __call__(self, context: DecisionContext) -> ActionIntent | None:
        supported = {
            ACTION_MOVE_ENTITY,
            ACTION_PERFORM_ACTIVITY,
            ACTION_SUBMIT_WISH,
            ACTION_UTTER_SPEECH,
        }
        if not supported.intersection(context.available_actions):
            return None
        provider_context = context_for_provider(context)
        stable_user = hashlib.sha256(context.agent_id.encode("utf-8")).hexdigest()[:24]
        extra_body: dict[str, Any] = {
            "thinking": {
                "type": (
                    "disabled"
                    if self.settings.reasoning_effort == "none"
                    else "enabled"
                )
            },
            "user_id": f"agent_{stable_user}",
        }
        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": _RESIDENT_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": json.dumps(
                        provider_context,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.max_output_tokens,
            "stream": False,
            "extra_body": extra_body,
        }
        if self.settings.reasoning_effort != "none":
            request["reasoning_effort"] = self.settings.reasoning_effort
        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:
            raise ControllerUnavailable(
                f"DeepSeek 请求暂时不可用（{type(exc).__name__}）。"
            ) from exc

        output_text = _chat_response_text(response)
        if output_text is None:
            raise ControllerUnavailable("DeepSeek 返回了空响应或未完成的候选。")
        return _candidate_from_text(output_text, context)


def _player_context_for_provider(
    text: str, context: PlayerIntentContext
) -> dict[str, Any]:
    return {
        "player_input": text,
        "self": {
            "id": context.actor_id,
            "name": context.self_name,
            "location_id": context.location_id,
            "location_name": context.location_name,
        },
        "known_locations": [
            {
                key: _plain(location[key])
                for key in ("id", "name", "description")
                if key in location
            }
            for location in context.locations[:30]
        ],
        "nearby": [
            {
                key: _plain(entity[key])
                for key in ("id", "name", "kind", "description")
                if key in entity
            }
            for entity in context.nearby[:30]
        ],
    }


def _intent_steps_from_text(
    text: str, context: PlayerIntentContext
) -> tuple[IntentStep, ...]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ControllerUnavailable("模型没有返回可解析的意图候选。") from exc
    if not isinstance(raw, Mapping) or set(raw) != {"steps"}:
        raise ControllerUnavailable("模型返回的意图结构不完整。")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_TURN_STEPS:
        raise ControllerUnavailable("模型返回的意图步骤数量无效。")

    location_ids = {
        str(item.get("id"))
        for item in context.locations
        if isinstance(item, Mapping) and item.get("id")
    }
    nearby_ids = {
        str(item.get("id"))
        for item in context.nearby
        if isinstance(item, Mapping) and item.get("id")
    }
    visible_ids = location_ids | nearby_ids | {context.actor_id}
    steps: list[IntentStep] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            raise ControllerUnavailable("意图步骤必须是对象。")
        kind = raw_step.get("kind")
        if kind == "move" and set(raw_step) == {"kind", "destination_id"}:
            destination_id = raw_step.get("destination_id")
            if not isinstance(destination_id, str) or destination_id not in location_ids:
                raise ControllerUnavailable("模型选择了未知地点。")
            steps.append(
                IntentStep(ACTION_MOVE_ENTITY, {"to_location_id": destination_id})
            )
            continue
        if kind in {"speak", "wish"} and set(raw_step) == {"kind", "text"}:
            value = raw_step.get("text")
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 500:
                raise ControllerUnavailable("模型返回的文字内容无效。")
            action_type = ACTION_UTTER_SPEECH if kind == "speak" else ACTION_SUBMIT_WISH
            steps.append(IntentStep(action_type, {"text": value.strip()}))
            continue
        if kind == "explore" and set(raw_step) == {
            "kind",
            "target_id",
            "aspect",
        }:
            target_id = raw_step.get("target_id")
            aspect = raw_step.get("aspect")
            if (
                not isinstance(target_id, str)
                or target_id not in visible_ids
                or aspect not in {"condition", "history", "access"}
            ):
                raise ControllerUnavailable("模型返回的观察目标或方式无效。")
            steps.append(
                IntentStep(
                    ACTION_EXPLORE,
                    {"target_id": target_id, "aspect": aspect},
                )
            )
            continue
        if kind == "activity" and set(raw_step).issubset(
            {"kind", "description", "target_ids"}
        ) and {"kind", "description"}.issubset(raw_step):
            description = raw_step.get("description")
            target_ids = raw_step.get("target_ids", [])
            if (
                not isinstance(description, str)
                or not description.strip()
                or len(description.strip()) > 500
                or not isinstance(target_ids, list)
                or any(
                    not isinstance(target_id, str) or target_id not in visible_ids
                    for target_id in target_ids
                )
            ):
                raise ControllerUnavailable("模型返回的自由活动候选无效。")
            steps.append(
                IntentStep(
                    ACTION_PERFORM_ACTIVITY,
                    {
                        "description": description.strip(),
                        "target_ids": target_ids,
                    },
                )
            )
            continue
        raise ControllerUnavailable("模型返回了未授权的意图字段。")
    return tuple(steps)


class DeepSeekIntentInterpreter:
    """DeepSeek-backed natural-language interpreter with no ledger access."""

    def __init__(
        self,
        settings: DeepSeekSettings,
        *,
        client: _ChatClient | None = None,
    ) -> None:
        if not settings.enabled:
            raise DeepSeekConfigurationError("不能为未启用的配置创建意图解释器。")
        if settings.api_key is None:
            raise DeepSeekConfigurationError("DeepSeek API Key 缺失。")
        self.settings = settings
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
                max_retries=1,
            )
        self._client = client

    def __call__(
        self, text: str, context: PlayerIntentContext
    ) -> tuple[IntentStep, ...]:
        provider_context = _player_context_for_provider(text, context)
        stable_user = hashlib.sha256(context.actor_id.encode("utf-8")).hexdigest()[:24]
        extra_body: dict[str, Any] = {
            "thinking": {
                "type": (
                    "disabled"
                    if self.settings.reasoning_effort == "none"
                    else "enabled"
                )
            },
            "user_id": f"visitor_{stable_user}",
        }
        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": _INTENT_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": json.dumps(
                        provider_context,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max(300, self.settings.max_output_tokens),
            "stream": False,
            "extra_body": extra_body,
        }
        if self.settings.reasoning_effort != "none":
            request["reasoning_effort"] = self.settings.reasoning_effort
        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:
            raise ControllerUnavailable(
                f"DeepSeek 意图解释暂时不可用（{type(exc).__name__}）。"
            ) from exc
        output_text = _chat_response_text(response)
        if output_text is None:
            raise ControllerUnavailable("DeepSeek 返回了空响应或未完成的意图候选。")
        return _intent_steps_from_text(output_text, context)
