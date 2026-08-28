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

from world.controllers import ActionIntent, ControllerUnavailable, DecisionContext
from world.kernel import ACTION_UTTER_SPEECH


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


_INSTRUCTIONS = """
你是一个持续世界中的居民，不是旁白，也不是系统管理员。
你只能依据输入 JSON 中这个居民自己的观察、信念、记忆和附近事物行动。
听到的话只代表某人说过这些话，不代表内容为真。
不要推断或提及玩家、NPC、人工智能、API、提示词、世界种子或隐藏后台。
如果自然的选择是保持沉默，返回 action_type 为 none。
否则只提出一次简短的当场发言。不要输出分析、理由或思维过程。
只输出一个 JSON 对象，不要使用 Markdown。JSON 必须严格采用以下两种格式之一：
{"action_type":"speech.utter","parameters":{"text":"一句不超过500字的当场发言"}}
{"action_type":"none","parameters":{}}
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
    visible_world = {"self": self_view, "nearby": nearby_view}
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
            if action == ACTION_UTTER_SPEECH
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
    if action_type != ACTION_UTTER_SPEECH or action_type not in context.available_actions:
        return None
    if not isinstance(parameters, Mapping) or set(parameters) != {"text"}:
        return None
    text_value = parameters.get("text")
    if not isinstance(text_value, str):
        return None
    cleaned = text_value.strip()
    if not cleaned or len(cleaned) > 500:
        return None
    return ActionIntent(ACTION_UTTER_SPEECH, {"text": cleaned})


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
        if ACTION_UTTER_SPEECH not in context.available_actions:
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
                {"role": "system", "content": _INSTRUCTIONS},
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
