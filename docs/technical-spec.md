# 大世界 v0 技术规格

状态：v0 实现基线
运行目标：单机、本地优先、单进程 FastAPI + SQLite + 原生网页；LLM 为可替换的外部 API，缺少 API key 时仍可用脚本策略运行。

## 0. 范围、术语与硬性不变量

v0 的场景是仍在正常生产和生活的“千禧钢城 · 曙光钢铁联合厂”。默认引导数据包含厂区、曙光家属区、厂职工医院、曙光子弟学校、百货商店、餐厅、食堂与沿街店铺，以及约 5 个真人角色、5 个初始虚拟居民和 1 个孩子。这里的“真人角色”表示现实层可绑定真人 controller 的世界实体，不表示世界界面会公开该绑定。

场景主题为“千禧梦核”，但其来源是约 2000 年前后的日常材料、设备和公共生活：马赛克、荧光灯箱、点阵屏、显像管电视、磁带、VCD、广播、换班和家属区生活。初始状态采用普通现实主义，不预置废弃钢厂、阴谋、身份谜题或超自然答案；这只约束 genesis，不限制未来走向，开放性来自参与者和居民在同一因果世界中的后续行动。world metadata 以 `initial_state_mode=ordinary-realism` 与 `future_mode=emergent-open-ended` 记录这一边界。

以下不变量优先于任何界面或叙事需求：

- **T1 三层隔离**：世界真相、个体体验、个体信念使用不同模型和读取边界。玩家接口绝不返回完整 `WorldState` 或原始账本。
- **T2 单一写入者**：只有 `WorldKernel.submit()` 能把候选变成世界事件；Controller、LLM、网页和 cognition 模块均无状态写权限。
- **T3 历史可重建**：删除所有投影后，仅凭世界元数据和事件流即可重建相同状态；事件带哈希链。
- **T4 分叉不回写**：子世界共享父世界指定前缀，之后独立；父世界永不因子世界改变。
- **T5 Entity 统一**：所有世界对象都是 Entity；只有 `policy_id` 非空且可解析的 Entity 可作为行动者。
- **T6 技术身份隔离**：human / AI / delegate 只存在于受保护的现实运行层，不进入玩家可观测事件和世界内称谓。
- **T7 隐藏事实不可迎合**：latent 输入中递归禁止 wish / goal 字段；相同 seed 和规范化探索上下文得到相同结果，首次结果由事件冻结。
- **T8 离线惰性执行**：delegate 仅由事件批次唤醒，每批至多提交一个候选；不运行连续思维链。

v0 明确不做：3D、模型训练、向量数据库、连续意识模拟、公开互联网浏览、真实支付、自动对外发信、不可逆现实操作和面向未成年人或医疗场景的生产部署。

---

## A. 项目目录结构

```text
.
├── README.md                   # 安装、启动、演示和架构摘要
├── main.py                     # Railpack / Uvicorn 根入口
├── pyproject.toml              # Python 版本、依赖与 pytest 配置
├── requirements.txt           # FastAPI / Uvicorn 等最小运行依赖
├── requirements-dev.txt       # pytest 等开发依赖
├── app/
│   ├── __init__.py
│   ├── api.py                  # FastAPI 入口：uvicorn app.api:app
│   ├── llm.py                  # DeepSeek observer-scoped 候选适配器
│   ├── runtime.py              # 现实层 consent / membership / delegate cursor
│   ├── epistemic_store.py      # perception / belief / memory 的隔离持久层
│   ├── scenario.py             # 千禧钢铁厂城实体、场景版本与现实层 controller binding
│   ├── service.py              # observer-scoped 用例编排；不绕过 Kernel
│   └── web/
│       ├── index.html          # 知情同意与世界视图
│       ├── app.js              # 只调用受限 API
│       └── styles.css
├── world/
│   ├── __init__.py
│   ├── models.py               # Entity、WorldEvent、ActionProposal 等纯模型
│   ├── event_store.py          # SQLite 追加、读取、哈希校验、分叉元数据
│   ├── state.py                # WorldState 投影与 replay
│   ├── kernel.py               # 白名单验证和唯一状态转移入口
│   ├── perception.py           # Event -> observer-scoped perception/belief/memory
│   ├── controllers.py          # Human / AI / Delegate Controller 接口
│   ├── child.py                # 愿望选择、发展先验、capability graph
│   └── latent.py               # seed + 探索上下文的隐藏事实冻结候选
├── tests/
│   ├── test_kernel.py          # 回放、并发、哈希、分叉、事件约束
│   ├── test_cognition.py       # 三层泄漏、信念证据、controller 惰性
│   ├── test_llm.py             # DeepSeek 配置、裁剪与结构化候选
│   ├── test_service_llm.py     # 批次幂等、失败与并发冲突
│   ├── test_runtime.py         # 现实层 consent / membership 与 cursor
│   └── test_api.py             # 同意、observer view、行动与错误边界
├── docs/
│   ├── deployment.md
│   ├── principles.md
│   └── technical-spec.md
└── data/                       # 运行时 SQLite；不提交个人数据
```

依赖方向固定为：`app -> world`；`kernel -> models/state/event_store`；cognition、controller、child、latent 可以创建候选或派生记录，但不能引用 `app`，也不能直接执行 SQL 写入世界事件。

---

## B. 核心数据模型和事件 schema

### B1. 四个数据域

| 数据域 | 权威含义 | 写入者 | 普通 Agent 可读范围 |
|---|---|---|---|
| `WorldState / WorldEvent` | 世界实际发生了什么 | 仅 World Kernel | 不可直接读取，只经 perception |
| `PerceptionRecord` | 某个观察者收到什么 | perception 管道 | 仅对应观察者 |
| `BeliefRecord / MemoryRecord` | 某个实体如何理解和记住体验 | cognition 管道 | 仅该实体及明确授权组件 |
| consent / session / controller binding | 现实身份、同意、控制方式 | app 现实运行层 | 世界内不可读取 |

后三者中，信念“存在”本身可以是系统记录，但信念内容不因此成为世界真相。Controller 的自然语言理由同理：它最多是该实体提交或陈述的理由，不是可验证的内心事实。

### B2. Entity 与 Agent

v0 的统一模型为：

```text
Entity
  id: str                    # 世界内稳定 UUID/slug
  name: str                  # 世界内显示名，不编码技术身份
  kind: str                  # person/location/object/org/child/...，仅作组件提示
  policy_id: str | null      # 非空且在策略注册表可解析时才是 Agent
  location_id: str | null
  attributes: JSON object    # 小型、schema 可验证的组件集合
```

`kind == "person"` 不能推出 Entity 是 Agent；`kind != "person"` 也不能推出它不是 Agent。行动资格的唯一判据是有效 `policy_id`，行动本身还要通过能力、位置、资源和安全检查。

`policy_id` 不得取值为 `human`、`npc`、`ai` 或 `delegate`。现实层另存 `ControllerBinding(entity_id, controller_type, principal/session, delegate_limits)`，不投影到世界视图。

愿望、话语、目标、能力和已冻结隐藏事实在底层同样投影为不同 `kind` 的 Entity；`wishes`、`utterances`、`capabilities` 等只是按 kind 建立的便利索引，不能发展成第二套对象基类。事件 envelope 与私有 cognition record 是对发生过程和认识过程的记录，不是世界中的对象，因此不伪装成 Entity。

### B3. Event envelope

所有世界变化使用不可变 `WorldEvent`：

```json
{
  "world_id": "world-uuid",
  "seq": 12,
  "event_id": "event-uuid",
  "event_type": "entity.moved",
  "payload": {
    "entity_id": "resident-1",
    "from_location_id": "lobby",
    "to_location_id": "cafe"
  },
  "occurred_at": "2026-08-28T12:00:00Z",
  "actor_id": "resident-1",
  "proposal_id": "proposal-uuid",
  "prev_hash": "<64 hex chars>",
  "event_hash": "<64 hex chars>"
}
```

`event_hash = SHA-256(canonical_json(上述除 event_hash 外的字段))`。根事件的 `prev_hash` 是 64 个 `0`；后续事件引用上一事件的 `event_hash`。`occurred_at` 用于审计和显示，不可作为未记录随机结果的来源。

事件表至少以 `(world_id, seq)` 和 `event_id` 唯一。追加在单一 SQLite 事务中执行，并使用 `expected_seq` 做乐观并发；不匹配时整批拒绝，客户端刷新 observer view 后重新提议，不能静默覆盖。

### B4. v0 事件 payload

| `event_type` | 必填 payload | Kernel 不变量 |
|---|---|---|
| `world.created` | `world_id, name, seed, metadata` | 每个根世界仅一次；seed 创建后不可改 |
| `entity.created` | `entity{id,name,kind,policy_id,location_id,attributes}` | id 唯一；location 若有必须存在；policy 必须可解析或为空 |
| `entity.moved` | `entity_id, from_location_id, to_location_id` | 行动者存在且可行动；from 等于当前投影；目标位置存在且可达 |
| `activity.performed` | `activity_id, actor_id, description, location_id, target_ids` | 只记录行动者在当前可达场景中实际做过的事；不能借描述改写隐藏事实或远处状态 |
| `speech.uttered` | `utterance_id, speaker_id, text, target_ids, location_id` | 记录的事实仅是“说出了这些字”；不把字面内容升级为真相 |
| `wish.submitted` | `wish_id, submitted_by, text` | 作者存在；文本有长度限制；愿望保持独立，不生成聚合向量 |
| `child.goal_selected` | `goal_id, child_id, description, source_wish_ids, rationale` | child 有 policy；来源愿望存在或明确为空；理由是声明而非全知解释 |
| `capability.unlocked` | `entity_id, capability_id, name, description, prerequisite_ids, evidence_event_ids` | 所有前置节点已解锁；证据事件存在；重复解锁拒绝 |
| `latent.fact_frozen` | `fact_id, key, value, scope, exploration_context_hash, determinism_key` | key/scope 唯一；hash 与 seed/上下文相符；已有值永不重抽 |

事件类型和参数由 Kernel 显式白名单管理。未知字段默认拒绝，而非写入 `attributes`。schema 演进必须增加版本迁移或新事件类型，不能就地改旧事件语义。

### B5. Proposal 与投影

```text
ActionProposal
  action_type: str
  actor_id: str
  parameters: JSON object
  proposal_id: str
  observed_seq: int | null
  submitted_at: datetime
```

`ActionProposal` 不是事件，也不能作为“事情已发生”的证据。Kernel 验证后创建一个或多个只含确定结果的 `EventDraft`，event store 再分配序号、时间与哈希。

`WorldState` 至少投影 `world_id/name/seed/seq`、`entities`、位置关系、utterances、wishes、child goals、capabilities 和 latent facts。投影是纯函数：`replay(world_id, ordered_events) -> WorldState`；禁止读取网络、LLM、当前时间或未记录随机数。

### B6. SQLite 与分叉元数据

`SQLiteEventStore(path)` 提供：

```text
create_world / get_world / fork_world
append / load_events / head
```

世界元数据保存 `world_id, seed, parent_world_id, fork_seq, head_seq, created_at`。子世界的 `load_events` 递归读取祖先至 `fork_seq` 的前缀，再拼接子世界本地事件；子事件序号从 `fork_seq + 1` 继续。子世界首个本地事件的 `prev_hash` 引用所继承前缀的末尾哈希。

快照可以在数据量变大后加入，但必须能删除并从事件重建。v0 不需要 Kafka、Redis、PostgreSQL 或向量数据库；SQLite 使用短事务，单进程写入即可。

---

## C. World Kernel 状态转移设计

### C1. 唯一提交路径

```text
Controller / UI / LLM
        │ ActionProposal
        ▼
WorldKernel.submit(world_id, proposal, expected_seq)
  1. 读取并校验世界/分支 head
  2. replay 得到权威 WorldState
  3. 校验 actor 的 policy、位置、能力、资源与 action schema
  4. 如行动涉及 latent，先从固定上下文产生冻结事件候选
  5. 纯确定性 transition 生成 EventDraft[]
  6. event store 在一个事务中校验 head、追加并建立哈希链
  7. 返回已提交 WorldEvent[]；之后才派生各观察者体验
```

无效候选返回结构化错误（位置、原因、可修复方式），不产生世界事件。若需要安全审计，失败尝试写现实运行层日志，不能污染世界历史或被普通角色观察。

### C2. 确定性规则

- transition 函数只接受当前 `WorldState + ActionProposal`，不直接调用 LLM。
- 需要随机结果时，用 seed、规则版本、规范化因果上下文生成，并把结果写入事件。
- 移动事件中的 `from_location_id` 由当前状态核验，不能相信 Controller。
- 说话事件只证明行为发生；真假判断留给每个观察者的 belief 管道。
- capability 不能由模型自报解锁；模型只能提交包含证据引用的候选。
- 多事件动作要么全部追加，要么全部失败。
- 相同 `proposal_id` 应实现幂等：已成功提交则返回原结果，不重复行动。

### C3. 回放与分叉验收

至少通过以下测试：

1. 同一事件流回放两次得到字节级等价的规范化状态。
2. 任意事件 payload 被修改后，哈希链校验失败并指出首个坏序号。
3. 两个候选使用同一 `expected_seq` 时只有一个成功，另一个得到冲突。
4. 在序号 N 分叉后，父/子在 N 的状态相同；子提交 N+1 不改变父 head。
5. 删除投影缓存后，根世界和子世界都能仅凭账本恢复。

---

## D. perception / belief / memory 管道

### D1. 数据流

```text
已提交 WorldEvent
  -> server-side visibility(event, observer, WorldState)
  -> perceive_event(event, observer_id, observable=True)
  -> PerceptionRecord
  -> beliefs_from_perception(...)
  -> BeliefRecord[]
  -> memory_from_belief(...)
  -> MemoryRecord | null
  -> entity-scoped retrieval
  -> DecisionContext
```

`observable` 必须由服务端根据同地位置、目标对象、感官/通信能力和访问控制计算；客户端不得通过传 `true` 获得事件。v0 可以先实现“同一 location 或明确 target 可见”，但函数边界必须保留，不能把完整事件流交给浏览器再隐藏 DOM。

### D2. 记录字段与语义

- `PerceptionRecord(perception_id, observer_id, source_event_id, perceived_type, details, confidence, observed_at, source_seq)`：只携带被允许字段，不保存一份可反序列化的原始隐藏 payload。
- `BeliefRecord(belief_id, holder_id, subject_id, predicate, object_value, confidence, provenance, formed_at)`：Belief 可以错误或互相矛盾，不能反向覆盖 `WorldState`。
- `MemoryRecord(memory_id, owner_id, memory_type, content, confidence, provenance, encoded_at, salience)`：检索永远先按 owner 过滤。
- 每次跨层派生都附 `ProvenanceRef(source_kind, source_id, relation)`；缺来源 id 的记录拒绝形成，而不是补写一个猜测来源。
- `knowledge` 是经证据组织的 belief/memory；`skill` 是可执行程序或熟练度；`capability` 是 Kernel 承认的行动许可。三者不得混为一个模型生成的标签。

特别规则：听到 `speech.uttered("明天全厂停工")` 后，v0 只产生“说话者说过这句话”的高置信 belief；“明天全厂停工”最多成为低置信、待验证命题，绝不自动成为世界真相。

### D3. 隔离验收

- 不在现场且未被指定为 target 的观察者得不到说话内容。
- belief 更新只引用该实体拥有的 perception。
- memory 检索 A 永不返回 owner B 的私有记录。
- `latent.fact_frozen` 不直接产生普通玩家 perception；只有后续探索/发现事件可暴露相应部分。
- 玩家 API 的响应中不出现 world seed、controller type、全量事件、其他角色私有 belief/memory 或未发现 latent value。

---

## E. Agent Controller 接口（human / AI / delegate）

### E1. 统一接口

```text
Controller.propose(context: DecisionContext) -> ActionProposal | null
```

`DecisionContext` 为不可变、observer-scoped 数据，只包含：当前实体 id、它获得的新 perceptions、可检索的自身 beliefs/memories、Kernel 公布给它的可用 actions/capabilities、观察到的 world seq，以及有限触发事件。它不包含原始 `WorldState`、world seed、他人 controller 类型或未发现 latent facts。

任何 Controller 的返回值都走同一个 `WorldKernel.submit()`；不存在 AI 专用的可信写入通道。

### E2. 三类实现

| Controller | 唤醒方式 | v0 行为 | 边界 |
|---|---|---|---|
| `HumanController` | 玩家提交一段自然语言 | observer-scoped interpreter 把表达译成有序候选，交给 Kernel | 页面不要求玩家选择“移动/发言/行动”；服务端绑定 actor，不能相信客户端自填 actor id |
| `ScriptedAIController` | 本轮产生的相关事件 | 根据有限 context 调用确定性策略或 DeepSeek adapter | LLM JSON 需 schema 校验；解释字段不能成为事件结果 |
| `DelegateController` | 仅非空 `trigger_events`、直接互动、到期世界事件或重新上线交接 | 根据本人预授权偏好代理；同一事件 batch 最多一项行动 | 无触发即 `null`；过滤 chain-of-thought/隐藏推理字段；有额度和动作白名单 |

玩家的主入口是一次 `POST /turns`：服务端先解释这段自然语言，提交可成立的因果变化，再自动消费这一轮产生的有限事件批次并返回最终 observer view。玩家不需要先“确认行动”再另点一次“让世界回应”。没有相关变化时不唤醒 controller，成本为零；旧 `/actions` 与 `/advance` 仅保留为兼容接口，不出现在玩家界面。

### E3. 模型适配器

v0 已实现 `DeepSeekIntentInterpreter` 与 `DeepSeekPolicy`，默认模型为 `deepseek-v4-pro`。前者把玩家的自由表达译成有序候选，后者为被相关事件唤醒的居民提出回应；两者都只接收 observer-scoped 的有限上下文。居民可以提出移动、在场活动、许愿、说话或保持沉默，玩家无法在界面上看见这些底层分类。JSON 合法不等于候选可信，服务端仍会按本地 schema 验证类型、参数、长度、角色身份、局部可达性和 `observed_seq`，最终只有 Kernel 能写入世界。

模型供应商是现实运行层的可替换边界。以后接入 OpenAI API 时，只新增或替换 `app/llm.py` 中的 provider adapter，并在 `app/api.py` 注入相应配置；`DecisionContext`、`ActionProposal` 和本地验证契约保持不变。不得为了迁移供应商修改 `WorldKernel`、历史事件 schema、event store、回放规则或 perception / belief / memory 数据含义。

超时、空响应、JSON 无效、越权 action 或 API 不可用时不写世界事件，也不得为了维持剧情而伪造成功。每个有界事件 batch 最多调用一次；SDK 只做一次有限重试，失败的 batch 会被消费，避免玩家反复点击形成无上限付费重试。API key 只从环境变量读取，不进数据库、事件、网页、prompt 或配置对象的字符串表示。

---

## F. wish pool 与 child goal selection

### F1. 愿望模型

每个 `wish.submitted` 独立保存 `wish_id / submitted_by / text / event_seq`。v0 不计算全局平均 embedding、不做 PCA、不按票数自动定目标，也不因文字相似就抹去作者和语境。

`score_wishes()` 对每个愿望逐条输出 `WishScore`，评分只使用孩子当前可观测的愿望、其 developmental priors、现有 knowledge/skills/capabilities 与资源边界。评分是决策辅助，不是世界真相。

### F2. 最低发展先验与自主性

v0 的 `DevelopmentalPriors` 使用四个显式权重与一个接纳阈值：

- `safety`：降低不可逆伤害，并在不确定时偏好可撤销试验；
- `autonomy`：尊重自身及其他主体的选择，不把愿望当命令；
- `curiosity_learning`：偏好能增加可检验知识与技能的步骤；
- `social_care`：考虑社区关系、照料和协作后果；
- `minimum_acceptance`：所有愿望低于阈值时自主拒绝，而不是被迫选择。

目标连续性由既有 goal/memory 输入和事件账本保障；认识论谦逊由 perception/belief 分层保障；可行性由 Kernel 的 capability/resource 校验保障。这样它们仍是硬约束，但不伪装成 v0 代码里尚不存在的额外评分轴。

孩子的 policy 可以选择一个愿望、拒绝全部、暂缓、或提出关联多个愿望的新目标。即使 v0 的确定性 fallback 只选择逐项得分最高的安全愿望，数据模型也必须允许 `source_wish_ids=[]/多个` 和“拒绝/暂缓”；相同分数使用 `seed + child_id + wish_id` 的稳定 hash 打破平局，而非依赖列表顺序。

`child.goal_selected` 保存 description、来源愿望与孩子对外给出的 rationale。LLM 可提出候选，但 Kernel 只接受 schema 合法、来源可追溯、没有被禁止现实副作用的目标事件。

### F3. capability graph

```text
Capability
  capability_id / name / description
  prerequisite_ids[]

ChildDevelopmentState
  child_id
  memory_ids[]
  knowledge{}
  skills{name -> level}
  capability_ids{}
```

`CapabilityGraph` 必须拒绝循环或缺失的前置节点。孩子的发展状态分别保存 `memory / knowledge / skills / capabilities`；只有 Kernel 接受并写入带 `evidence_event_ids` 的 `capability.unlocked`，事件投影才能改变最后一项。资源条件、安全约束和知识/技能证据在 v0 由 Kernel 校验；未来可在不破坏已有节点语义的 schema 版本中将其提升为 capability 的显式字段。

初始能力仅包括最低限度的观察、记忆、表达候选和选择目标能力，不假定人形移动或抓取。若孩子后来借助轮子、公司、账户、居民协作或法律身份获得行动范围，应表现为新节点及其依赖/证据，而不是修改一句角色设定。

---

## G. latent reality 机制

### G1. 解析协议

`ExplorationContext.from_kernel(...)` 从已提交状态构造规范化上下文，例如规则版本、地点、被探索对象、探索方法、行动者具备的感知能力和必要的因果状态摘要。上下文递归拒绝键名或数据源中的 `wish`、`wishes`、`goal`、`desired_result`、模型回答和 UI 提示词。

```text
context_hash   = SHA-256(canonical_json(exploration_context))
determinism_key = SHA-256(world_seed + resolver_version + scope + key + context_hash)
value           = fixed_rule_table(determinism_key, declared_domain)
```

`exploration_id` 与当前账本 `observed_seq` 只用于请求追踪和并发控制，不进入 `context_hash`；否则无关愿望或对话仅通过推进序号就能改变隐藏事实。进入 hash 的只能是地点、方法、行动者相关能力和必要因果状态。

v0 使用固定规则表/有限候选域映射 hash，不用 LLM 临场编造隐藏真相。`LatentRealityResolver.propose_resolution()` 只返回冻结候选；Kernel 再检查该 `scope + key` 尚未存在、hash 可复算、候选域合法，随后追加 `latent.fact_frozen`。Resolver 不能直接写 store。

### G2. 冻结、发现与分叉

- 某 key 已冻结：任何再次探索返回同一事实，不新增事件、不重抽。
- 未冻结但上下文相同：相同 seed 和 resolver version 得到相同候选，与请求顺序和当前愿望无关。
- 分叉点之前已冻结：子世界继承结果。
- 分叉后未冻结：若两个分支的规范化因果上下文相同，则结果相同；上下文因真实事件不同而不同，结果才可不同。
- 冻结不等于发现：`latent.fact_frozen` 对普通角色不可见；探索成功后另由可观测事件呈现允许部分。
- resolver 升级不能重算旧事实；版本写入 determinism key，新版本只作用于尚未冻结的新 namespace/key。

需要语义丰富的隐藏规律时，应在世界创建阶段给出版本化规则模板和候选域，或先冻结机器可验证的结构再由 LLM 生成不改变语义的表面叙述。

---

## H. 安全与知情同意边界

### H1. 两层体验

现实层在进入世界前展示并记录版本化同意：

1. 环境包含 AI 生成或控制的角色/内容，真人离线时可能由自动系统代理；互动对象的具体类型不会在世界内逐一标注。
2. 行动与对话会进入持久事件/体验记录；说明用途、保存期、导出/退出/删除或去标识化方式。
3. 启用外部语言模型时，角色自身可观察到的有限上下文会发送给已配置的供应商；完整账本、隐藏事实、world seed 和其他角色私人认知不会发送。
4. 这是实验性虚构社会环境，不是医疗、法律、金融服务；世界内承诺不自动产生现实效力，并应提供退出、举报、屏蔽和联系渠道。

世界内不显示技术身份标签，但允许角色只根据可观察行为形成信念。该模糊性不能扩展到现实收费、数据用途、AI 参与、真实危险或外部操作。

### H2. v0 服务边界

- `GET /api/reality/consent-notice` 返回当前现实层说明、版本，以及部署是否要求邀请码。
- `POST /api/reality/consents` 通过可选邀请码门槛并记录同意后才允许 join；邀请码和同意记录都不写世界事件。
- `POST /api/worlds/default/join` 建立现实 session 到世界 entity 的受保护绑定。
- `GET /api/worlds/{world_id}/view` 只返回该 session/entity 的 observer-scoped view。
- `POST /api/worlds/{world_id}/turns` 接收玩家的一段自然语言，绑定当前 actor，完成解释、Kernel 提交、有限回应和 observer view 返回。
- 每个 `/turns` 响应包含至少一条 observer-scoped `feedback`；没有新事件时也明确返回“没有新的可观察变化”，但不伪造 NPC 回应。
- 旧 `POST /actions` 与 `POST /advance` 标记为 deprecated，仅供兼容；`POST /forks` 只作为受权开发/研究工具，不出现在普通玩家界面。
- v0 不提供浏览器可用的 raw `/api/state` 或全量 ledger 端点。回放和审计先通过内核测试/本地代码完成。

独立的 `/observer` 控制面由 `GREAT_WORLD_OBSERVER_TOKEN` 鉴权。它可以读取真相、信念、体验、事件、latent facts 与 capability graph，并对服务器本地快照进行自然语言只读查询；查询代码不持有写句柄，也不调用外部 LLM。唯一写端点 `/api/observer/reset` 只接受当前默认世界及精确确认短语，原子切换现实层默认指针：旧 epoch 标记为 archived 并继续可在观察台选择和查看，新 epoch 使用新 id 与新 seed 从当前场景 genesis 启动。旧账本仍由不可变触发器保护，所有旧世界 mutation 路径返回 `world_archived`；新旧纪元不共享后续状态、latent freeze 或 cognition。

开发模式也不得把 world seed、latent value、其他实体的私有 cognition 或 controller binding 放进网页响应。若未来增加管理员端点，必须与玩家 session、路由和日志明确隔离。

### H3. 行为安全

- v0 在界面下方使用隐藏的因果事件/capability schema、文本长度限制、请求大小限制、可选邀请码和乐观并发；生产公开部署前另加按参与者与模型预算的速率限制。
- prompt 中的世界对话始终当作不可信数据，不能成为系统指令、工具授权或 secret 来源。
- v0 capability graph 不包含支付、系统命令、任意文件、邮箱、社交账号或公网发布能力。
- 任意未来外部动作都需要现实层 capability、最小权限、可审计适配器和临动作的人类确认；世界内愿望不能授予权限。
- 不让角色冒充具体真实人物；delegate 必须受本人授权与可撤销偏好约束，但其技术状态仍不在世界内公开。
- 生产试验前补齐身份验证、加密、数据保留/删除、内容举报处置、模型供应商数据政策和伦理审查。当前 v0 只适合本机或单实例互联网环境中的受邀测试，不适合公开招募。

event sourcing 与删除请求冲突时，v0 不收集不必要的真实身份或敏感信息；生产设计使用现实身份映射分离、内容去标识化和可加密擦除字段，而不是悄悄改写历史并继续声称账本完整。

---

## I. v0 里程碑与完成定义

以下为单人净开发估算；可并行但不缩减验收项。

### I1. 可验证 Kernel（1–2 个开发日）

- 建立 models、SQLiteEventStore、replay、WorldKernel 和九类核心事件。
- 完成哈希链、乐观并发、幂等候选和分叉前缀。
- **完成定义**：C3 的 5 项测试全部通过；任何代码路径都不能直接改投影。

### I2. 认识论隔离与 controllers（1–2 个开发日）

- 完成 perception/belief/memory 纯管道与 owner-scoped retrieval。
- 完成 Human、ScriptedAI、Delegate 的统一接口和事件驱动惰性约束。
- **完成定义**：不在场者、其他 owner、delegate 无触发三类测试均返回空；Controller 只能得到 Proposal。

### I3. 孩子、愿望与 latent reality（1–2 个开发日）

- 建立 developmental priors、逐愿望评分、可拒绝的目标模型与 capability graph。
- 完成 ExplorationContext 禁止字段、确定性解析和首次冻结。
- **完成定义**：愿望顺序变化不改变稳定选择；加入“想要某结果”的愿望不改变同一 latent key；前置能力缺失时无法解锁。

### I4. 最低成本网页闭环（1 个开发日）

- FastAPI 提供 consent、join、observer view、turns 和受控 forks；原生网页只有一个自然语言入口，一次提交会自行完成合适的世界变化、有限居民回应，并显示玩家此刻可见的结果。
- 提供独立的只读可视化观察台与自然语言查询；唯一写操作把旧 epoch 封存并以新 seed 创建下一 epoch。
- 启动时生成仍在运转的千禧钢铁厂城演示世界，包含生产区、家属区、医院、学校、商店和餐饮设施；无 LLM key 时使用脚本 controller。
- **完成定义**：新环境按 README 在 10 分钟内启动；浏览器不收到 raw state/seed/controller type；一次自然表达后能看到被 Kernel 接受的后果和本轮自然回应。

### I5. 受控小规模试玩前加固（2–3 个开发日）

- 增加 session 身份校验、输入/速率限制、备份恢复、同意版本、举报/退出流程、API 成本和错误监控。
- 用 5 真人 + 5 初始虚拟居民 + 1 孩子做 30–60 分钟本地演练，检查因果、泄漏、token 唤醒次数与分叉回放。
- **完成定义**：没有世界真相泄漏；每次模型调用都能追溯到一个触发 batch；SQLite 备份可恢复；已知安全缺口有书面处置。

### v0 总验收场景

1. Alice 在蓝鲸餐厅说一句话；同地且有权限的 Bob 得到 perception，远处厂职工医院里的 Carol 得不到。
2. Bob 相信的是“Alice 说过 X”，不是系统把 X 认定为真；该 belief 只能进入 Bob 的 memory。
3. 玩家提交愿望；孩子依据 priors 与能力选择、暂缓或拒绝，Kernel 记录可追溯目标，不产生 PCA 聚合人格。
4. 首次检查一个尚未观察的设备状态后冻结事实；随后提交相反愿望再检查，结果不变。
5. 在探索前分叉：相同因果上下文得到相同 latent 结果；一个分支的移动/目标/能力不回写另一个。
6. 真人离线且没有相关事件时，delegate 不调用模型；有人直接互动后最多提出一个候选。

资源目标：应用和 SQLite 常驻内存远低于 16 GB；初始数据库为 MB 级；磁盘按事件文本线性增长并提供备份/归档；不下载模型权重。LLM 成本由事件触发上限、每批一次候选、上下文裁剪、超时和脚本 fallback 控制。
