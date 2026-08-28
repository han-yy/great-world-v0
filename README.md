# 大世界 v0

一个可以在本机运行的最小世界：人用自然语言生活，所有真实后果仍只能经 World Kernel 生效；历史可回放、可分叉；每个角色只能得到属于自己的观察与记忆。

当前场景是仍在正常生产和生活的“千禧钢城 · 曙光钢铁联合厂”：厂区与曙光家属区、厂职工医院、曙光子弟学校、千禧百货商店、蓝鲸餐厅、第二食堂和彩虹商业街共同组成一座厂城。初始世界包含 5 个来客角色、5 个初始居民、1 个没有固定人形的孩子，以及厂前广场上的新世纪愿望留言台。

“千禧梦核”在这里是一种日常质感，不是一条悬疑剧情：浅蓝马赛克、薄荷绿墙裙、乳白灯箱、点阵电子钟、录像带、磁带、显像管电视和学校广播都来自约 2000 年前后的普通生活。钢厂没有被废弃，居民也不是等待玩家解开的谜；任何异常或变化都必须由世界中真实发生的事件产生。

## 1. 本地启动（约 3 分钟）

需要 Python 3.11+。推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --extra dev --python 3.12
uv run uvicorn app.api:app --reload
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。首次进入会先显示现实层知情同意；本地数据写入 `data/great_world.sqlite3`。

若不用 uv：

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.api:app --reload
```

## 2. 现在能做什么

网页只有一个主要输入框。参与者可以直接写“我去蓝鲸餐厅坐坐，问问林乔今天推荐什么”，也可以描述散步、观察、聊天、许愿或其他当下活动；不需要先选择动作类别。

一次提交会完成：自然语言解释、有限步骤校验、Kernel 写入、个人体验派生，以及一轮相关居民的按需回应。内部仍保留移动、说话、活动、愿望和探索等可验证事件语义，但这些不会作为人的菜单出现在界面上。没有相关新体验时，居民不会持续运行模型或“思考”。

每次提交都会返回至少一条属于当前参与者的可观察反馈。它可以是环境变化、他人的自然反应、行动遇到的阻力，或者“眼前没有出现新变化”；系统不会强迫居民每次搭话，但也不会让一次表达无声消失。

隐藏事实仍只由固定 seed 和首次探索上下文确定并冻结；自然语言和当前愿望不能要求世界临时生成迎合结果。分叉能力保留在后端与开发工具中，不作为日常世界界面的主操作。

要启用 DeepSeek，复制 `.env.example` 为 `.env`，只在 `.env` 中填写自己的 key，并改为 `DEEPSEEK_ENABLED=true`：

```bash
uv run --env-file .env uvicorn app.api:app --reload
```

启用后，`deepseek-v4-pro` 有两个介入点：解释规则无法明确处理的玩家自然语言，以及为配置的居民提出一次局部回应候选。其他居民与孩子仍可使用可复现的本地策略。模型输出始终由服务端重新校验，随后仍由同一个 Kernel 决定是否写入历史。

当前外部模型适配器使用 DeepSeek。以后切换到 OpenAI API 时，只替换 `app/llm.py` 中的 provider adapter 及 `app/api.py` 的配置注入；`WorldKernel`、事件 schema、回放、perception / belief / memory 管道和场景状态都不随供应商改变。

## 3. 只读世界观察台

在 `.env` 中设置至少 24 字符的 `GREAT_WORLD_OBSERVER_TOKEN`，重启后打开 [http://127.0.0.1:8000/observer](http://127.0.0.1:8000/observer)。观察台可以可视化世界真相、个体信念、个体体验、事件账本、孩子成长和已冻结的隐藏事实，也可以用自然语言进行本地只读查询。

观察台没有局部修改、发言、控制 Agent 或注入剧情的接口。唯一写操作是整世界重置：当前 epoch 连同事件账本被封存为只读并继续可观察，新 epoch 以新的 world id 和新的 seed 从同一版本场景底稿启动；旧历史不会删除、改写或混入新纪元。观察台查询不调用外部 LLM，因此完整账本、world seed 和他人 cognition 不会被发送给模型供应商。

## 4. 关键边界

- 世界真相只存在于不可变事件账本与其投影中。
- perception、belief、memory 分表保存，并保留来源与置信度。
- human / AI / delegate 绑定只在现实运行层，玩家视图不返回技术身份。
- 所有世界对象都是 `Entity`；`policy_id` 非空才可成为行动者。
- 网页没有 raw state、world seed、全量 ledger 或他人 cognition 接口。

详细设计见 [项目原则](docs/principles.md) 和 [v0 技术规格 A–I](docs/technical-spec.md)。

## 5. 验证

```bash
uv run pytest -q
```

当前测试覆盖：事件哈希与不可变性、乐观并发、回放与分叉、观察隔离、speech 不升级为真相、controller 边界、DeepSeek 上下文裁剪与失败关闭、愿望选择、capability graph、latent freeze、必有反馈、只读观察查询、epoch 封存与新 seed 重置。

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 6. 代码入口

- `world/kernel.py`：唯一因果写入口与 action 白名单。
- `world/event_store.py`：SQLite 追加账本、哈希链、祖先前缀分叉。
- `world/perception.py`：truth → perception → belief → memory。
- `app/service.py`：参与、可见性、惰性响应、孩子选择与探索编排。
- `app/intents.py`：自然语言意图上下文、确定性解释与自由活动 fallback。
- `app/llm.py`：当前 DeepSeek 意图/居民候选适配器、上下文裁剪与失败关闭；未来 OpenAI API 也在这一供应商边界接入。
- `app/observer.py`：无写句柄的本地自然语言观察查询。
- `app/api.py` / `app/web/`：受限 API 与无框架网页。

## 7. 部署到互联网

仓库已包含 Railway 可自动识别的 `main.py` 入口和可选邀请码门槛。完整步骤、Dashboard 设置、持久磁盘和 API 介入位置见 [部署指南](docs/deployment.md)。当前版本只适合小规模、受邀测试；不要把链接公开传播。生产试验前仍需补强身份验证、速率限制、退出/删除流程、举报处置、数据保留策略和伦理审查。
