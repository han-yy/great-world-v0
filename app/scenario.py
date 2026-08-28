"""Deterministic v0 scenario content.

Controller labels in this file are runtime configuration.  They are removed
before any observer-scoped world view is built.
"""

from __future__ import annotations


SCENARIO_ID = "community-mall-v0"
SCENARIO_NAME = "白榆社区商业中心"

LOCATIONS = (
    {
        "entity_id": "place:atrium",
        "name": "中庭",
        "description": "新铺的浅色地砖还有一点石灰味。中央是一座没有铭牌的许愿池。",
        "archetype": "place",
    },
    {
        "entity_id": "place:cafe",
        "name": "折页咖啡",
        "description": "落地窗面对中庭，开业菜单只写了六种饮品。",
        "archetype": "place",
    },
    {
        "entity_id": "place:grocery",
        "name": "四时杂货",
        "description": "货架还没有完全摆满，收银台边放着几束白榆枝。",
        "archetype": "place",
    },
    {
        "entity_id": "place:workshop",
        "name": "小祝修造所",
        "description": "门口散着木屑和金属零件，墙上留着一块空招牌。",
        "archetype": "place",
    },
    {
        "entity_id": "place:corridor",
        "name": "后勤走廊",
        "description": "这条窄走廊通往尚未开放的区域，顶灯偶尔闪一下。",
        "archetype": "place",
    },
)

OBJECTS = (
    {
        "entity_id": "object:wish_pool",
        "name": "许愿池",
        "description": "水很浅，投入其中的文字会在水面停留一段时间。",
        "archetype": "object",
        "location_id": "place:atrium",
    },
)

PLAYER_SLOTS = tuple(
    {
        "entity_id": f"visitor:{index}",
        "name": f"来客{label}",
        "description": "刚来到这里的人。",
        "archetype": "resident",
        "location_id": "place:atrium",
        "policy_id": "policy:visitor",
    }
    for index, label in enumerate("一二三四五", start=1)
)

RESIDENTS = (
    {
        "entity_id": "resident:linqiao",
        "name": "林乔",
        "description": "折页咖啡的店主，习惯先听完再说话。",
        "archetype": "resident",
        "location_id": "place:cafe",
        "policy_id": "policy:shopkeeper",
    },
    {
        "entity_id": "resident:meiyu",
        "name": "梅雨",
        "description": "正在给四时杂货的每一格货架编号。",
        "archetype": "resident",
        "location_id": "place:grocery",
        "policy_id": "policy:shopkeeper",
    },
    {
        "entity_id": "resident:laozhu",
        "name": "老祝",
        "description": "修造所主人，愿意研究任何能拆开的东西。",
        "archetype": "resident",
        "location_id": "place:workshop",
        "policy_id": "policy:maker",
    },
    {
        "entity_id": "resident:qiaoan",
        "name": "乔安",
        "description": "负责公共区域的巡查与协调。",
        "archetype": "resident",
        "location_id": "place:atrium",
        "policy_id": "policy:steward",
    },
    {
        "entity_id": "resident:chihe",
        "name": "池禾",
        "description": "常带着速写本，似乎在记录建筑里的变化。",
        "archetype": "resident",
        "location_id": "place:corridor",
        "policy_id": "policy:observer",
    },
)

CHILD = {
    "entity_id": "child:one",
    "name": "孩子",
    "description": "ta 还没有固定身体。此刻，ta 通过许愿池边的声音与微弱灯光感知中庭。",
    "archetype": "child",
    "location_id": "place:atrium",
    "policy_id": "policy:child-v0",
    "capabilities": ("perceive", "remember", "communicate"),
    "memory": (),
    "knowledge": (),
    "skills": (),
}

ALL_ENTITIES = LOCATIONS + OBJECTS + PLAYER_SLOTS + RESIDENTS + (CHILD,)
BOOTSTRAP_HEAD_SEQ = 1 + len(ALL_ENTITIES) + 3  # world.created + entities + child capabilities

# Reality-layer bindings.  These are never serialized into Entity events or
# observer views.  Policy says *how the character acts*; controller says which
# technical adapter currently supplies its candidates.
CONTROLLER_BINDINGS = {
    **{entity["entity_id"]: "human" for entity in PLAYER_SLOTS},
    "resident:linqiao": "scripted_ai",
    "resident:meiyu": "scripted_ai",
    "resident:laozhu": "scripted_ai",
    "resident:qiaoan": "delegate",
    "resident:chihe": "scripted_ai",
    "child:one": "child_selector",
}


CAPABILITY_LABELS = {
    "perceive": "感知",
    "remember": "记忆",
    "communicate": "交流",
    "investigate": "调查",
    "coordinate": "协调",
    "move": "移动",
    "build": "制造",
}


LATENT_ASPECTS = {
    "condition": {
        "values": ("保养良好", "有近期使用痕迹", "存在轻微故障", "看起来从未启用"),
        "weights": (0.30, 0.30, 0.25, 0.15),
    },
    "history": {
        "values": ("留下了很短的旧记录", "没有可辨认的旧痕迹", "有一处被擦去的标记"),
        "weights": (0.35, 0.45, 0.20),
    },
    "access": {
        "values": ("入口目前锁着", "入口可以打开", "入口卡住了，需要工具"),
        "weights": (0.50, 0.25, 0.25),
    },
}
