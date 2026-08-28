"""Read-only natural-language summaries for the observer console.

The query engine receives an already-built snapshot and has no reference to the
kernel, event store, runtime store, or any mutation callback.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


EVENT_LABELS = {
    "world.created": "世界开始",
    "entity.created": "实体出现",
    "entity.moved": "位置变化",
    "activity.performed": "在场活动",
    "speech.uttered": "说话",
    "wish.submitted": "愿望出现",
    "child.goal_selected": "孩子选择目标",
    "capability.unlocked": "能力形成",
    "latent.fact_frozen": "隐藏事实冻结",
}


def _entity_name(snapshot: Mapping[str, Any], entity_id: Any) -> str:
    wanted = str(entity_id or "")
    for entity in snapshot.get("truth", {}).get("entities", ()):  # type: ignore[union-attr]
        if str(entity.get("id")) == wanted:
            return str(entity.get("name") or wanted)
    return wanted or "未知实体"


def _event_line(snapshot: Mapping[str, Any], event: Mapping[str, Any]) -> str:
    actor = _entity_name(snapshot, event.get("actor_id")) if event.get("actor_id") else "系统"
    label = EVENT_LABELS.get(str(event.get("event_type")), str(event.get("event_type")))
    return f"#{event.get('seq')} · {label} · {actor}"


def answer_observer_question(
    snapshot: Mapping[str, Any], question: str
) -> dict[str, Any]:
    """Answer common free-form questions without gaining write authority."""

    cleaned = question.strip()
    if not cleaned:
        raise ValueError("观察问题不能为空。")
    normalized = cleaned.casefold()
    truth = snapshot.get("truth", {})
    cognition = snapshot.get("cognition", {})
    events = list(truth.get("events", ()))
    entities = list(truth.get("entities", ()))
    evidence: list[str] = []

    mentioned = next(
        (
            entity
            for entity in entities
            if str(entity.get("name", "")) in cleaned
            or str(entity.get("id", "")).casefold() in normalized
        ),
        None,
    )

    if any(word in normalized for word in ("隐藏", "潜在", "latent", "秘密", "规律")):
        facts = list(truth.get("latent_facts", ()))
        if not facts:
            answer = "当前 epoch 还没有冻结任何隐藏事实；这不代表隐藏规律不存在，只代表尚未被探索触发。"
        else:
            lines = [
                f"{item.get('key')}：{item.get('value')}（范围 {item.get('scope')}）"
                for item in facts[-12:]
            ]
            answer = "已经冻结的隐藏事实有：\n" + "\n".join(lines)
            evidence.extend(str(item.get("fact_id")) for item in facts[-12:])
        scope = "world_truth"
    elif any(word in normalized for word in ("孩子", "能力", "目标", "成长")):
        child = truth.get("child", {})
        goal = child.get("goal") or "尚未选择目标"
        capabilities = "、".join(child.get("capabilities", ())) or "尚无能力节点"
        answer = f"孩子当前目标：{goal}。\n已经形成的能力：{capabilities}。"
        evidence.extend(str(item) for item in child.get("evidence_event_ids", ()))
        scope = "world_truth"
    elif mentioned is not None and any(
        word in normalized for word in ("相信", "信念", "记忆", "观察", "感知", "为什么")
    ):
        entity_id = str(mentioned.get("id"))
        records = cognition.get(entity_id, {})
        beliefs = list(records.get("beliefs", ()))
        memories = list(records.get("memories", ()))
        perceptions = list(records.get("perceptions", ()))
        answer = (
            f"{mentioned.get('name')}目前有 {len(beliefs)} 条信念、"
            f"{len(memories)} 条记忆和 {len(perceptions)} 条可观察体验。"
        )
        if beliefs:
            latest = beliefs[-1]
            answer += (
                f"\n最近一条信念是：{latest.get('predicate')} = "
                f"{latest.get('object_value')}，置信度 {latest.get('confidence')}。"
            )
            evidence.append(str(latest.get("belief_id")))
        if memories:
            latest_memory = memories[-1]
            answer += f"\n最近一条记忆：{latest_memory.get('content')}。"
            evidence.append(str(latest_memory.get("memory_id")))
        scope = "individual_cognition"
    elif any(word in normalized for word in ("谁", "人物", "居民", "实体", "位置")):
        agents = [entity for entity in entities if entity.get("is_agent")]
        lines = [
            f"{entity.get('name')}：{_entity_name(snapshot, entity.get('location_id'))}"
            for entity in agents
        ]
        answer = f"当前共有 {len(agents)} 个具有 policy 的实体：\n" + "\n".join(lines)
        evidence.extend(str(entity.get("id")) for entity in agents)
        scope = "world_truth"
    elif any(word in normalized for word in ("发生", "事件", "最近", "刚才", "历史")):
        recent = events[-10:]
        if recent:
            answer = "最近的世界事件是：\n" + "\n".join(
                _event_line(snapshot, event) for event in recent
            )
            evidence.extend(str(event.get("event_id")) for event in recent)
        else:
            answer = "这个世界还没有事件。"
        scope = "event_ledger"
    else:
        world = snapshot.get("world", {})
        agents = [entity for entity in entities if entity.get("is_agent")]
        answer = (
            f"这是第 {world.get('epoch_index')} 个 epoch，当前序号为 {world.get('seq')}。"
            f"世界中有 {len(entities)} 个实体，其中 {len(agents)} 个具有 policy。"
            f"已冻结 {len(truth.get('latent_facts', ()))} 条隐藏事实。"
            "你可以继续询问最近发生了什么、某个人相信什么、孩子如何成长，或隐藏事实有哪些。"
        )
        scope = "overview"

    return {
        "answer": answer,
        "scope": scope,
        "evidence": evidence[:20],
        "read_only": True,
    }
