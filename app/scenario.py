"""Deterministic v0 scenario content.

Controller labels in this file are runtime configuration.  They are removed
before any observer-scoped world view is built.
"""

from __future__ import annotations


SCENARIO_ID = "millennium-steel-town-v0"
SCENARIO_NAME = "千禧钢城 · 曙光钢铁联合厂"
SCENARIO_THEME = "千禧梦核"
RULES_VERSION = "v0.2"
LATENT_RULE_VERSION = "millennium-steel-dreamcore-latent-v1"
INITIAL_STATE_MODE = "ordinary-realism"
FUTURE_MODE = "emergent-open-ended"
STARTING_LOCATION_ID = "place:factory_square"
DEFAULT_SOCIAL_LOCATION_ID = "place:restaurant"
ARRIVAL_SUMMARY = f"你来到了{SCENARIO_NAME}。"
ARRIVAL_DETAIL = (
    "厂前广场的红色点阵钟正在报时，乳白灯箱上写着“迎接新世纪”。"
    "换班的人走过公交站，高炉的低鸣、商店卷帘声和学校广播混在一起。"
)

LOCATIONS = (
    {
        "entity_id": STARTING_LOCATION_ID,
        "name": "厂前广场",
        "description": "浅蓝马赛克铺成的广场连着厂门和生活区。红色电子钟、乳白灯箱与喷泉都像刚为新世纪擦亮。",
        "archetype": "place",
    },
    {
        "entity_id": "place:blast_furnace",
        "name": "一号高炉观景台",
        "description": "安全栏杆外，巨大的炉体映着橙红炉光，银灰管道从淡蓝天空下穿过；这里按班次开放参观。",
        "archetype": "place",
    },
    {
        "entity_id": "place:rolling_mill",
        "name": "轧钢参观廊",
        "description": "玻璃隔墙把参观廊与机组分开。高窗把天光切成一格一格，钢板经过时脚下会有规律地轻震。",
        "archetype": "place",
    },
    {
        "entity_id": "place:family_quarters",
        "name": "曙光家属区",
        "description": "六层住宅楼围着水泥乒乓球台和褪色的彩色滑梯。阳台上晾着校服，窗外伸出密密的电视天线。",
        "archetype": "place",
    },
    {
        "entity_id": "place:hospital",
        "name": "厂职工医院",
        "description": "薄荷绿色墙裙一直通向门诊大厅，磨砂玻璃后传来推车声，候诊区的电视正在播放早间节目。",
        "archetype": "place",
    },
    {
        "entity_id": "place:school",
        "name": "曙光子弟学校",
        "description": "天蓝色栏杆围着红白跑道，教学楼侧墙画着宇宙飞船和齿轮，广播喇叭每天准点响起。",
        "archetype": "place",
    },
    {
        "entity_id": "place:department_store",
        "name": "千禧百货商店",
        "description": "玻璃柜台里摆着磁带、电子表、搪瓷杯和新到的随身听，彩电墙循环播放着同一段烟花。",
        "archetype": "place",
    },
    {
        "entity_id": DEFAULT_SOCIAL_LOCATION_ID,
        "name": "蓝鲸餐厅",
        "description": "蓝白招牌照着镀铬桌椅，菜单同时供应工友套餐、奶油蛋糕和装在高脚杯里的橘子汽水。",
        "archetype": "place",
    },
    {
        "entity_id": "place:canteen",
        "name": "第二食堂",
        "description": "白汽从取餐窗口升起来，黑板每天用粉笔写菜价；早班、常日班和夜班都有各自熟悉的饭点。",
        "archetype": "place",
    },
    {
        "entity_id": "place:market_street",
        "name": "彩虹商业街",
        "description": "照相馆、录像带店、药房、面包房、面馆和夜宵排档沿着一条有顶棚的街依次亮灯。",
        "archetype": "place",
    },
    {
        "entity_id": "place:photo_studio",
        "name": "蓝光音像照相馆",
        "description": "橱窗里摆着样片和磁带，店内可以拍证件照、冲洗胶卷，也能租到刚上架的 VCD。",
        "archetype": "place",
    },
    {
        "entity_id": "place:computer_room",
        "name": "新世纪电脑屋",
        "description": "米白色显示器排成两行，墙上贴着打字练习和上机登记表，放学后常有学生来做电子小报。",
        "archetype": "place",
    },
    {
        "entity_id": "place:culture_palace",
        "name": "工人文化宫",
        "description": "玻璃砖楼梯通向礼堂、舞厅和游戏室，门厅海报预告着新世纪联欢会与周末电影。",
        "archetype": "place",
    },
)

OBJECTS = (
    {
        "entity_id": "object:wish_pool",
        "name": "新世纪愿望留言台",
        "description": "喷泉旁的蓝白电子留言台会逐条保存居民想做成的事，并在公共信息屏上轮流显示。",
        "archetype": "object",
        "location_id": STARTING_LOCATION_ID,
    },
    {
        "entity_id": "object:factory_map",
        "name": "厂区总平面图",
        "description": "一块罩着有机玻璃的彩色地图，把生产区、家属区和生活设施画在同一张蓝图上。",
        "archetype": "object",
        "location_id": STARTING_LOCATION_ID,
    },
    {
        "entity_id": "object:information_screen",
        "name": "公共信息屏",
        "description": "厚重的显示器用蓝底白字滚动班车、电影、门诊和商店营业时间。",
        "archetype": "object",
        "location_id": STARTING_LOCATION_ID,
    },
    {
        "entity_id": "object:public_phone",
        "name": "磁卡公用电话",
        "description": "半透明蓝色电话罩里挂着一部按键电话，旁边贴着已经卷边的号码表。",
        "archetype": "object",
        "location_id": "place:family_quarters",
    },
)

PLAYER_SLOTS = tuple(
    {
        "entity_id": f"visitor:{index}",
        "name": f"来客{label}",
        "description": "刚从厂前路走进这座钢城的人。",
        "archetype": "resident",
        "location_id": STARTING_LOCATION_ID,
        "policy_id": "policy:visitor",
    }
    for index, label in enumerate("一二三四五", start=1)
)

RESIDENTS = (
    {
        "entity_id": "resident:linqiao",
        "name": "林乔",
        "description": "蓝鲸餐厅的值班经理，熟悉倒班工人、学生和家属们各自习惯坐的位置。",
        "archetype": "resident",
        "location_id": DEFAULT_SOCIAL_LOCATION_ID,
        "policy_id": "policy:shopkeeper",
    },
    {
        "entity_id": "resident:meiyu",
        "name": "梅雨",
        "description": "厂职工医院的护士，口袋里总放着一支蓝色圆珠笔和折好的值班表。",
        "archetype": "resident",
        "location_id": "place:hospital",
        "policy_id": "policy:caregiver",
    },
    {
        "entity_id": "resident:laozhu",
        "name": "老祝",
        "description": "在轧钢主厂房工作多年的设备员，能从振动和声音里听出机器是否正常。",
        "archetype": "resident",
        "location_id": "place:rolling_mill",
        "policy_id": "policy:maker",
    },
    {
        "entity_id": "resident:qiaoan",
        "name": "乔安",
        "description": "曙光子弟学校的自然课老师，也帮家属区组织周末活动。",
        "archetype": "resident",
        "location_id": "place:school",
        "policy_id": "policy:steward",
    },
    {
        "entity_id": "resident:chihe",
        "name": "池禾",
        "description": "工人文化宫的放映员，常替商店画海报，也会把居民借来的录像带仔细编号。",
        "archetype": "resident",
        "location_id": "place:culture_palace",
        "policy_id": "policy:observer",
    },
)

CHILD = {
    "entity_id": "child:one",
    "name": "孩子",
    "description": "ta 还没有固定身体。此刻，ta 借愿望留言台的麦克风、扬声器和公共信息屏上的蓝色光点感知厂前广场；这些只是暂时借用的界面。",
    "archetype": "child",
    "location_id": STARTING_LOCATION_ID,
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
        "values": (
            "白色瓷砖和金属边框被擦得发亮",
            "蓝色指示灯按正常工作节奏缓慢闪烁",
            "表面留下了近期使用和维修的痕迹",
            "有一处不影响安全的轻微故障",
        ),
        "weights": (0.30, 0.30, 0.25, 0.15),
    },
    "history": {
        "values": (
            "档案里有一条一九九九年前后的普通更新记录",
            "值班本只记录了日常使用和保养",
            "这里以前还有另一种日常用途，后来随生活区一起调整过",
        ),
        "weights": (0.35, 0.45, 0.20),
    },
    "access": {
        "values": (
            "目前公开可进入",
            "需要工作人员陪同进入",
            "现在不在开放时段，开放时间已经公示",
        ),
        "weights": (0.50, 0.25, 0.25),
    },
}
