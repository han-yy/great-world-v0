# 大世界 v0：Railway 部署与 DeepSeek 接入

这套配置面向小规模、受邀测试：一个 FastAPI 进程、一份 SQLite 数据库、一个持久卷。不要横向扩容，也不要公开传播网址；当前 v0 尚未具备面向公众的账号、限流与治理能力。

## 1. API 在哪里介入

调用链固定为：

```text
角色获得新体验
  -> WorldService 构造该角色专属的 DecisionContext
  -> DeepSeekPolicy 只上传裁剪后的观察 / 信念 / 记忆
  -> deepseek-v4-pro 返回一项结构化行动候选或保持沉默
  -> 服务端绑定角色身份并再次校验
  -> WorldKernel 接受后才追加不可变事件
```

模型不能读取完整世界账本、world seed、隐藏事实或其他角色的私人 cognition，也不能直接写 SQLite。主要代码入口是 `app/llm.py`；注入发生在 `app/api.py`；事件批次与 Kernel 提交发生在 `app/service.py`。

默认仅 `resident:linqiao` 使用 DeepSeek。需要扩大到更多初始居民时，把 `DEEPSEEK_AGENT_IDS` 改为逗号分隔的列表：

```text
resident:linqiao,resident:meiyu,resident:laozhu,resident:qiaoan,resident:chihe
```

孩子的愿望选择暂时保留确定性 developmental priors，不交给外部模型，以便先验证因果与自主性边界。

## 2. 本地验证 DeepSeek

1. 在 [DeepSeek API Platform](https://www.deepseek.com/platform/) 创建 API key；不要把 key 发到聊天、浏览器代码或 Git 仓库。
2. 复制 `.env.example` 为 `.env`，填写 `DEEPSEEK_API_KEY`，并设 `DEEPSEEK_ENABLED=true`。
3. 启动：

```bash
uv sync --extra dev --python 3.12
uv run --env-file .env uvicorn app.api:app --reload
```

4. 进入世界后移动到咖啡店，对林乔说话，再点“让世界回应一次”。只有收到新体验时才产生一次模型请求。

`.env` 已被 `.gitignore` 排除。若模型超时、返回空内容、无效 JSON 或越权动作，该批次不会写入伪造事件。

## 3. Railway 上线

Railway 已弃用面向新服务的 `railway.toml`，因此这里使用当前支持的 Dashboard 流程；状态变化见 [Railway Infrastructure as Code 说明](https://docs.railway.com/infrastructure-as-code)。

1. 把当前仓库放入一个私有 GitHub 仓库；确认提交中没有 `.env`、SQLite 文件或 API key。
2. 在 Railway 新建 Project，选择 “Deploy from GitHub repo”。Railpack 会从根目录的 `main.py` 自动识别 FastAPI；在服务 Settings 中确认 Start Command 为 `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1`、Healthcheck Path 为 `/health`、Replicas 为 `1`。
3. 为服务添加 Volume，挂载路径填 `/app/data`。没有这个卷，重新部署时世界历史可能丢失。
4. 在服务的 Variables 中设置下列值，然后重新部署：

```text
GREAT_WORLD_DB=/app/data/great_world.sqlite3
GREAT_WORLD_ACCESS_CODE=<至少12字符的随机邀请码>
DEEPSEEK_ENABLED=true
DEEPSEEK_API_KEY=<只粘贴到 Railway 的 secret value>
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_ALLOW_CUSTOM_BASE_URL=false
DEEPSEEK_AGENT_IDS=resident:linqiao
DEEPSEEK_REASONING_EFFORT=none
DEEPSEEK_TIMEOUT_SECONDS=20
DEEPSEEK_MAX_OUTPUT_TOKENS=300
```

5. 在 Railway Networking 中生成 HTTPS 域名。先访问 `/health`，确认得到 `{"status":"ok","version":"0.1.0"}`，再把根网址只发给受邀测试者。

## 4. 上线边界

- 保持一个 replica、一个 worker；SQLite、进程内推进锁和单卷都依赖单写者部署。
- 设置一个难猜的邀请码，只发给受邀测试者；它保护新参与者入口，但不替代生产级账号系统和限流。
- DeepSeek key 只存在于本地 `.env` 或 Railway Variables；默认只允许发往 `api.deepseek.com`，自建网关必须显式开启并自行承担数据边界责任。
- 启用外部模型后，首次进入会展示新版知情说明：角色自己的有限上下文可能发送给模型供应商。
- 备份卷中的 `great_world.sqlite3` 及其 WAL 相关文件；备份前停止写入或使用 SQLite 在线备份机制。
若要公开招募而非小范围邀请，先完成真实身份验证、请求/模型预算限流、封禁举报、退出删除与数据保留政策。

## 5. 故障定位

| 现象 | 原因 | 修复 |
|---|---|---|
| 启动时报 API key 缺失 | 已启用 DeepSeek，但 secret 未设置 | 在 Railway Variables 设置 `DEEPSEEK_API_KEY` 后重部署 |
| 世界重部署后重置 | SQLite 写在临时文件系统 | 添加 `/app/data` Volume，并设置 `GREAT_WORLD_DB` |
| 林乔没有回应 | 没有新体验、模型失败或选择沉默 | 先在同一地点发言；再查看 Railway 日志中的 controller unavailable 提示 |
| 返回 401/402/429 | key 无效、余额不足或供应商限流 | 在 DeepSeek 控制台处理 key/余额，或降低触发频率 |
| 出现 SQLite 锁或历史冲突 | 启动了多个 worker/replica，或玩家同时推进 | 恢复为一个 worker/replica；冲突不会被自动改写 |
