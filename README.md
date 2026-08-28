# 大世界 v0

一个可以在本机运行的最小世界：行动只能经 World Kernel 生效；历史可回放、可分叉；每个角色只能得到属于自己的观察与记忆。

当前场景是刚开业的“白榆社区商业中心”，包含 5 个来客角色、5 个初始居民、1 个没有固定人形的孩子，以及一个公开许愿池。

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

1. 进入中庭，在五个地点间移动。
2. 对同地存在说话；不在现场的角色听不到内容。
3. 向许愿池提交愿望，让孩子逐条评估后自主选择或拒绝。
4. 探索对象；隐藏事实由固定 seed 和首次探索上下文确定并冻结。
5. 从当前事件序号创建分支，在不改动父世界的前提下继续行动。

`让世界回应一次` 只唤醒收到新体验的角色，每个角色每批最多提出一个行动；没有新事件时不会持续运行模型或“思考”。不配置 API key 时，世界仍可用确定性脚本完整演示。

要启用 DeepSeek，复制 `.env.example` 为 `.env`，只在 `.env` 中填写自己的 key，并改为 `DEEPSEEK_ENABLED=true`：

```bash
uv run --env-file .env uvicorn app.api:app --reload
```

默认只有咖啡店主林乔使用 `deepseek-v4-pro`，其他居民与孩子仍使用可复现的本地策略。模型只提出一次受限的发言候选；服务端重新校验，随后仍由同一个 Kernel 决定是否写入历史。

## 3. 关键边界

- 世界真相只存在于不可变事件账本与其投影中。
- perception、belief、memory 分表保存，并保留来源与置信度。
- human / AI / delegate 绑定只在现实运行层，玩家视图不返回技术身份。
- 所有世界对象都是 `Entity`；`policy_id` 非空才可成为行动者。
- 网页没有 raw state、world seed、全量 ledger 或他人 cognition 接口。

详细设计见 [项目原则](docs/principles.md) 和 [v0 技术规格 A–I](docs/technical-spec.md)。

## 4. 验证

```bash
uv run pytest -q
```

当前测试覆盖：事件哈希与不可变性、乐观并发、回放与分叉、观察隔离、speech 不升级为真相、controller 边界、DeepSeek 上下文裁剪与失败关闭、愿望选择、capability graph、latent freeze、知情同意和完整网页 API 闭环。

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 5. 代码入口

- `world/kernel.py`：唯一因果写入口与 action 白名单。
- `world/event_store.py`：SQLite 追加账本、哈希链、祖先前缀分叉。
- `world/perception.py`：truth → perception → belief → memory。
- `app/service.py`：参与、可见性、惰性响应、孩子选择与探索编排。
- `app/llm.py`：DeepSeek 配置、上下文裁剪、结构化候选与失败关闭。
- `app/api.py` / `app/web/`：受限 API 与无框架网页。

## 6. 部署到互联网

仓库已包含 Railway 可自动识别的 `main.py` 入口和可选邀请码门槛。完整步骤、Dashboard 设置、持久磁盘和 API 介入位置见 [部署指南](docs/deployment.md)。当前版本只适合小规模、受邀测试；不要把链接公开传播。生产试验前仍需补强身份验证、速率限制、退出/删除流程、举报处置、数据保留策略和伦理审查。
