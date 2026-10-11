# Nexa Guardian

简体中文 | [English](README.en.md) · [首次安装](docs/BOOTSTRAP.zh-CN.md) · [运维](docs/OPERATIONS.zh-CN.md) · [贡献](CONTRIBUTING.md) · [安全](SECURITY.md)

一个自己部署的 Telegram 群管理机器人：用你选择的大模型识别广告和刷屏，自动删除、警告、封禁，并提供管理员私聊管理。

- **模型自选**：任何兼容 OpenAI 接口的服务都能用，接口地址、模型、接口风格都在配置里填。
- **身份自定**：机器人叫什么、服务哪个社区、哪些是你自己的官方站点，全部由配置决定。
- **不存原文**：AI 判断不保存消息正文、图片、处罚原因或模型推理。
- **数据自持**：一个 SQLite 文件，自带一致性备份；以非 root 用户运行。

## 它做什么

- 链接、转发、联系方式、混淆字符、刷屏等只作为“可疑”预检；可疑消息交给模型判断，正常聊天不打扰。
- 文字、图片和视频封面一起审核。图片取够用的最小预览，下载有大小、并发和超时限制；下载失败时只用文字继续。
- 广告置信度达到阈值就删除并警告；警告累计到上限自动封禁。
- 模型判定为广告的稳定短语会被学习，之后同样的广告不用再调用模型。
- 只有可疑消息会显示“正在审核…”，结果原地更新；群里机器人的回复约 30 秒后自动清理。
- 管理员可以私聊机器人查看统计、暂停 AI、清理群历史、预览并移出长期不活跃成员。
- `DRY_RUN` 观察模式、群白名单、机器人身份核验、可选的日志群。

## 开始之前

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建机器人，记下 Token。Token 等同机器人的完整控制权，只放在服务器的配置文件里。
2. 把机器人加进群并设为管理员，只给它需要的权限：**删除消息**、**封禁成员**。
3. 准备两个数字 ID：群的 ID（负数，形如 `-1001234567890`）和你自己的用户 ID。
4. 想用 AI 审核，再准备一个兼容 OpenAI 接口的模型服务：接口地址、API Key 和模型 ID。不开 AI 也能运行，此时只用关键词、链接等规则过滤。

## 用 Docker 运行

需要 Docker Engine 和 Compose 插件。

```bash
git clone https://github.com/Tumblr-code/NexaAssistantRobot.git
cd NexaAssistantRobot
cp .env.example .env
```

编辑 `.env`，至少填这几项：

```dotenv
BOT_TOKEN=从 BotFather 拿到的 Token
ALLOWED_CHAT_IDS=-1001234567890
AUTHORIZED_ADMIN_IDS=你的数字 ID
```

然后启动并看日志：

```bash
docker compose up -d --build
docker compose logs -f guardian
```

在群里发 `/status` 确认它在工作。改了 `.env` 之后执行 `docker compose up -d --force-recreate guardian`。数据在命名卷 `guardian-data` 里，升级时不要删除它。

建议第一次先设 `DRY_RUN=true` 观察一段时间：机器人只记录会命中哪些消息，不删除、不警告、不封禁（手工管理员命令仍会执行）。

想用 systemd 直接装在主机上，按[首次安装指南](docs/BOOTSTRAP.zh-CN.md)操作。

## 接入你的模型

在 `.env` 里填：

```dotenv
AI_ENABLED=true
OPENAI_BASE_URL=https://你的服务商地址
OPENAI_API_KEY=你的密钥
AI_MODEL=模型 ID
```

多数服务只需要再确认下面两项：

| 变量 | 可选值 | 怎么选 |
|---|---|---|
| `AI_API_STYLE` | `responses`（默认）、`chat_completions` | 服务商支持 OpenAI Responses API 就用默认值；只有 `/chat/completions` 的选后者 |
| `AI_STRUCTURED_OUTPUT` | `json_schema`（默认）、`json_object` | 模型不支持严格 JSON Schema 输出时改成 `json_object` |

其余可调项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AI_REASONING_EFFORT` | `none` | 思考强度：`none`、`minimal`、`low`、`medium`、`high`；留空则不发送这个参数 |
| `AI_SERVICE_TIER` | `default` | 服务等级参数；留空则不发送 |
| `AI_STRICT_RESPONSE` | `false` | 设为 `true` 时要求服务端原样回显模型、服务等级和思考强度，不一致就判为失败 |
| `AI_MODEL_LABEL` | 空 | 展示给群成员的模型名称；留空显示“AI” |

接口地址写域名或以 `/v1` 结尾都可以，程序会补全路径；必须是 `https://`，本机服务（例如 Ollama）可以用 `http://localhost`。

**先探测再上线。** 下面的命令只发送一段固定的无害文字和一张 1×1 的测试图片，不读取也不发送任何群消息，用来确认你的模型支持所选的接口风格、结构化输出和图片输入：

```bash
uv run nexa-guardian-probe --env-file .env
```

探测会真实调用模型，可能消耗少量额度。探测失败时先不要开启 `AI_ENABLED`：换一个模型，或者调整 `AI_API_STYLE` 和 `AI_STRUCTURED_OUTPUT` 后再试。

对模型的要求：能按指令只返回 JSON，并且支持图片输入（探测会一并验证）。推荐使用响应快、便宜的小模型，单条消息的总预算默认 12 秒。

## 设置身份和官方站点

全部可选，不填就是一个中性的 “Guardian”。

```dotenv
BRAND_NAME=Example Guardian
COMMUNITY_NAME=Example
OFFICIAL_SITES=Example 商城|https://store.example.com,Example 帮助中心|https://help.example.com
AI_MODEL_LABEL=GPT
```

- `BRAND_NAME`、`COMMUNITY_NAME` 会写进审核提示词、`/help` 和机器人资料。
- `OFFICIAL_SITES` 里的域名被视为你自己的站点：精确匹配的链接本身不算外链广告；`store.example.com.evil.example` 这类仿冒域名仍会进入审核。只想放行某些域名而不想写进提示词，用 `ALLOWED_DOMAINS`（它同时放行子域名）。
- 机器人启动后会把名称、简介和介绍同步成按上面生成的内容。想自己在 BotFather 里维护资料，设 `SYNC_BOT_PROFILE=false`。
- 警告上限用 `WARN_LIMIT`（默认 3），文案里的次数会跟着变。

## 命令

菜单只有四个入口：

| 命令 | 菜单说明 |
|---|---|
| `/help` | 查看极简使用说明 |
| `/status` | 查看运行状态与管理统计 |
| `/del` | 删除消息、群历史或成员 |
| `/ban` | 回复消息或填写用户 ID 封禁 |

常用子命令：

```text
/status                       运行状态与管理统计
/status ai                    私聊查看 AI 审核详情
/status ai pause|resume       私聊暂停或恢复 AI 审核
/status ai words              私聊查看已启用的 AI 特征
/status ai del 特征ID         私聊停用误学特征
/del                          回复消息后删除；私聊可填写群消息 ID
/del all                      清理全部群历史
/del members                  私聊预览长期不活跃成员
/del members 确认码 confirm   确认移出本次候选成员
/ban                          回复成员消息后封禁；私聊可填写用户 ID
```

- 管理命令只接受 `AUTHORIZED_ADMIN_IDS` 里的用户，并且会核验他在 Telegram 里的实际权限：没有“封禁成员”权限的管理员不能借机器人封人。私聊管理要求名单里恰好一个人。
- 开了“匿名管理员”时 Telegram 不提供真实用户 ID，执行命令前需要暂时关闭匿名。
- `/del all` 和 `/del members` 需要管理员本人的 Telegram 用户会话（MTProto）：配置 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH` 并在服务器上交互登录一次，步骤见[首次安装指南](docs/BOOTSTRAP.zh-CN.md)。没配置时这两个命令会拒绝执行。
- `/del members` 先给出预览和确认码，确认后才移出。

## 常用配置

完整清单和注释见 [.env.example](.env.example)。

| 变量 | 默认 | 作用 |
|---|---:|---|
| `ALLOWED_CHAT_IDS` | 空 | 要管理的群的数字 ID；为空时不自动管理任何群 |
| `AUTHORIZED_ADMIN_IDS` | 空 | 可以执行管理命令的用户数字 ID |
| `EXPECTED_BOT_ID` / `EXPECTED_BOT_USERNAME` | 空 | 启动时核验机器人身份，防止误用 Token |
| `DRY_RUN` | `false` | 只记录自动命中，不处置 |
| `WARN_LIMIT` | `3` | 自动封禁前的累计警告次数 |
| `AI_AD_THRESHOLD` | `0.92` | 删除并警告的最低广告置信度 |
| `AI_LEARN_THRESHOLD` | `0.97` | 自动启用学习特征的最低置信度 |
| `AI_FAILURE_DELETE` | `true` | 模型调用失败时删除该可疑消息，但不警告、不学习 |
| `AI_TIMEOUT_SECONDS` | `12` | 单条消息审核的总预算，包含排队和一次重试 |
| `NOTICE_TTL_SECONDS` | `30` | 群里机器人回复保留的秒数 |
| `BLOCK_EXTERNAL_LINKS` | `true` | 外链作为可疑信号；不开 AI 时直接过滤 |
| `ALLOWED_DOMAINS` | 空 | 放行的域名及其子域名 |
| `LOG_CHAT_ID` | 空 | 可选，集中接收管理日志的群或频道 |
| `DATABASE_PATH` | `data/guardian.db` | SQLite 数据库位置 |

机器人用长轮询接收消息。启动时如果发现这个 Token 已经配置了 webhook，程序会拒绝启动，**不会替你删除 webhook**，以免切断正在使用它的其他服务。

## 隐私

- 发给模型的只有待审核的消息文字、链接的域名、本地风险信号的名称，以及（如有）一张图片预览。用户标识经过不可逆散列后才作为安全标识发送。
- 数据库里的 AI 判断记录不含消息原文、图片、处罚原因和模型推理；学习到的只是广告里稳定的短语，不含网址、账号、电话等。
- 判断记录默认保留 30 天（`AI_DECISION_RETENTION_DAYS`）。

## 开发

要求 Python 3.10 以上，使用 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pyright src
uv run pytest --cov=src/nexa_guardian --cov-fail-under=75
uv build
python scripts/build_release.py
```

测试只用模拟对象和临时数据，不连接真实的 Telegram 或模型。最后两条在 `dist/` 生成 wheel、部署包和校验文件。本地直接运行：`uv run nexa-guardian`。

## 文档

- [首次安装（systemd）](docs/BOOTSTRAP.zh-CN.md)
- [运维：升级、回退、备份、排查](docs/OPERATIONS.zh-CN.md)
- [升级工具 release.sh 说明](deploy/RELEASE.md)

## 许可

MIT，见 [LICENSE](LICENSE)。使用 [aiogram](https://github.com/aiogram/aiogram) 3 和 Telethon。
