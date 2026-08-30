# Nexa Guardian

一个独立、自托管的 Telegram 群管理机器人，使用 GPT-5.6 Sol 识别广告，并提供删除、警告、封禁和管理员私聊管理。它不会改动现有客服机器人，也不会自动接管未明确列入白名单的群。

Nexa Guardian 的身份是 Nexa 官方群管理审核员。固定的一方站点为：

- Nexa Store：<https://nexastore.cn>
- Nexa ModelPass：<https://openai.nexastore.cn>
- Nexa GiftPass：<https://premium.nexastore.cn>

上述精确域名本身不会被当作外部链接信号；相似域名、伪造后缀、钓鱼、冒充和附带其他广告证据的内容仍会进入审核。

生产服务位于 VM `192.168.0.35`，由 `nexa-guardian.service` 持久运行，目标为 `@NexaStoreGroup`。v0.4.1 使用五个极简命令，提供持久化关键词抽奖和可选的管理员 MTProto 全历史清理。

## 已实现

- GPT-5.6 Sol 自适应审核：无本地可疑信号的纯文字先用 `low`；可疑、刷屏、图片或视频封面直接用 `high`；`low` 只有在判为非广告且置信度不低于 `0.90` 时才直接放行，其余结果升级为 `high` 复核。
- AI 网关复用 keep-alive 连接；相同并发内容合并为一次请求，并把排队、重试纳入同一个总超时。判断记录拆分媒体、排队、网关、动作和端到端延迟，可在 `/aistats` 查看路径与 P50/P95。
- 图片从 Telegram 可用尺寸中选择达到目标边长的最小预览，视频只取封面；下载受独立并发、大小和 2 秒超时限制，失败时只用已有文字继续审核。
- 外链、转发、联系方式、混淆字符、刷屏及已学习特征仅作为可疑预检；正常聊天静默审核。
- 只有可疑消息会显示“正在审核…”；结果原地更新，群内机器人输出统一在约 30 秒后删除，授权管理员私聊回复永久保留。
- 每次成功广告判断都写入不含原文和用户标识的永久聚合记忆；安全短语进入候选库，高置信短语立即启用，中等置信短语重复命中 3 次后启用，后续可不调用 API 直接拦截。
- 违规累计警告，达到阈值后自动封禁并清零。
- 命令菜单固定为 `/help`、`/status`、`/del`、`/ban`、`/lottery`；其余旧命令静默停用。
- 群抽奖支持精确关键词报名、默认静默置顶、定时/手动开奖、取消、重抽和服务重启恢复；结束或取消时只解除本场抽奖卡片的置顶。
- `/del all` 可选使用唯一管理员的 Telegram 用户会话分批清理群历史；部署验收不会自动执行。
- 可选独立日志群；管理动作先与本地记录原子写入持久 outbox，再异步发送和失败重试，不阻塞删消息、警告或封禁。
- AI 判断不保存消息正文、图片、处罚原因或模型推理。
- `DRY_RUN` 观察模式、群 ID 白名单、在线 SQLite 备份、持久化临时消息删除队列、独立 Telegram 删除连接、systemd/Docker 非 root 运行。

## GitHub 调研取舍

- [GroupButler](https://github.com/group-butler/GroupButler) 的规则、反洪水和管理员缓存思路成熟，但主体是较旧的 Lua/Redis 技术栈。
- [Guardian 示例项目](https://github.com/RakinSV/Telegram-admin-app-project) 展示了现代 aiogram 3 的 CAPTCHA、链接白名单、反洪水和分级处置，但整个仓库远大于本项目所需范围。
- 本项目使用 [aiogram](https://github.com/aiogram/aiogram) 3.x，从零实现所需的小型功能集，没有复制上述项目代码。

Telegram 官方说明：群管理员机器人可以接收普通群消息；删除和限制成员仍要求对应管理员权限。参见 [Bots FAQ](https://core.telegram.org/bots/faq) 和 [Bot API](https://core.telegram.org/bots/api)。

## 1. 创建机器人

1. 在 Telegram 打开 `@BotFather`，执行 `/newbot`，保存 Token。
2. 把机器人加入目标群并设为管理员，只授予本项目所需权限：
   - 删除消息；
   - 封禁/限制成员。
   - 置顶消息。
3. 管理员机器人会收到普通群消息。若它不是管理员，则必须通过 `@BotFather` 的 `/setprivacy` 关闭隐私模式，并重新加入群，才能检查全部消息。

Token 等同机器人完整控制权，只放在服务器的 `.env`，不要发到聊天、提交到 Git 或写入截图。

## 2. 配置

```bash
cp .env.example .env
```

第一次只填写 `BOT_TOKEN`，保持 `ALLOWED_CHAT_IDS` 不匹配真实群。启动后由群管理员在群里执行：

```text
/chatid
/status
```

把 `/chatid` 返回的负数群 ID 填入 `.env`：

```dotenv
ALLOWED_CHAT_IDS=-1001234567890,-1009876543210
```

然后重启。`ALLOWED_CHAT_IDS` 为空或不包含该群时，机器人只响应 `/chatid` 和 `/status`，不会自动删消息或限制成员。

常用配置：

| 变量 | 程序默认值 | 作用 |
|---|---:|---|
| `EXPECTED_BOT_ID` | 空 | 启动时核验机器人数字 ID；生产必须显式配置 |
| `EXPECTED_BOT_USERNAME` | 空 | 启动时核验用户名（可不带 `@`）；生产必须显式配置 |
| `UPDATE_CONCURRENCY_LIMIT` | `32` | aiogram 同时处理的更新上限，避免无限创建任务 |
| `AI_ENABLED` | `false` | 启用 GPT-5.6 Sol AI 审核；启用前必须完成网关探测 |
| `OPENAI_BASE_URL` | 空 | 兼容网关基础地址，支持 `/v1/responses` |
| `OPENAI_API_KEY` | 空 | 兼容网关密钥，仅保存于服务器 `0600` 环境文件 |
| `AI_MODEL` | `gpt-5.6-sol` | 固定审核模型，不接受家族别名或自动降级 |
| `AI_REASONING_EFFORT` | `high` | 兼容及能力探测基准；固定为 `high` |
| `AI_NORMAL_REASONING_EFFORT` | 继承上项 | 普通纯文字首轮强度；生产固定为 `low` |
| `AI_SUSPICIOUS_REASONING_EFFORT` | 继承上项 | 可疑、刷屏和媒体消息强度；生产固定为 `high` |
| `AI_NORMAL_ACCEPT_CONFIDENCE` | `0.90` | 普通 `low` 非广告直接放行的最低置信度；其他结果升级 `high` |
| `AI_SERVICE_TIER` | `default` | 固定为当前兼容网关实际支持并回显的服务层 |
| `AI_AD_THRESHOLD` | `0.92` | 删除并警告的最低广告置信度 |
| `AI_LEARN_THRESHOLD` | `0.97` | 自动启用学习特征的最低置信度 |
| `AI_FAILURE_DELETE` | `true` | AI 故障时删除原消息，但不警告、不学习 |
| `AI_IMAGE_MAX_BYTES` | `5242880` | 单个媒体预览的硬上限；生产固定为 `1572864`（1.5 MiB） |
| `AI_IMAGE_TARGET_LONG_EDGE` | `1280` | 优先选择长边达到该值的最小 Telegram 图片预览 |
| `AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS` | `2` | 单个图片或视频封面下载超时 |
| `AI_MEDIA_CONCURRENCY` | `4` | 同时下载媒体预览的上限 |
| `NOTICE_TTL_SECONDS` | `30` | 群聊及日志群机器人输出的保留秒数；授权管理员私聊不清理 |
| `BLOCK_EXTERNAL_LINKS` | `true` | AI 模式仅用于可疑预检；旧模式直接过滤 |
| `AUTHORIZED_ADMIN_IDS` | 空 | 可执行手动命令的数字用户 ID；私聊管理要求恰好一个且仍需具备群权限 |
| `ALLOWED_DOMAINS` | 空 | 全局允许的域名及其子域名 |
| `BLOCKED_WORDS` | 空 | 仅供 AI 关闭时旧模式回滚使用 |
| `BLOCK_FORWARDS` | `false` | AI 模式仅用于转发可疑预检 |
| `BLOCK_CHANNEL_MENTIONS` | `false` | AI 模式仅用于推广账号可疑预检 |
| `WARN_LIMIT` | `3` | 自动封禁前的累计警告次数 |
| `DEFAULT_MUTE_SECONDS` | `3600` | 手动 `/mute` 的默认禁言秒数 |
| `DRY_RUN` | `false` | 只记录自动命中，不删除/警告/禁言；手动管理员命令仍执行 |
| `LOG_CHAT_ID` | 空 | 可选的集中管理日志群/频道 |
| `TELEGRAM_API_ID` | 空 | `/del all` 使用的 Telegram API ID；必须与 Hash 同时配置 |
| `TELEGRAM_API_HASH` | 空 | `/del all` 使用的 Telegram API Hash；仅存于 VM 受保护环境文件 |
| `TELEGRAM_SESSION_PATH` | `/var/lib/nexa-guardian/mtproto/admin` | 已交互登录的唯一管理员用户会话路径 |

兼容网关必须同时支持 Responses API、严格 JSON Schema、图片输入、`reasoning.effort=low/high` 和可核验的服务层。当前生产网关会把 `priority` 请求降为 `default`，因此程序明确固定并校验 `service_tier=default`，不会把降级伪装成 Priority。快速响应由连接复用、相同内容 singleflight、普通文字 `low`、8 秒端到端总超时、10 分钟缓存和已学习特征本地直拦实现。程序只接受响应中明确返回 `status=completed`、`model=gpt-5.6-sol`、本次请求对应的推理强度及 `service_tier=default` 的结果，缺失或不匹配均按审核服务故障处理。请求设置 `store=false`，只发送待判断文本或图片，不发送用户名、用户 ID、群名和聊天历史。普通文件和无预览贴纸不送入 AI。

生产发布额外要求显式配置并由发布探针校验：`EXPECTED_BOT_ID`、`EXPECTED_BOT_USERNAME`、`UPDATE_CONCURRENCY_LIMIT=32`、`AI_NORMAL_REASONING_EFFORT=low`、`AI_SUSPICIOUS_REASONING_EFFORT=high`、`AI_NORMAL_ACCEPT_CONFIDENCE=0.90`、`AI_IMAGE_MAX_BYTES=1572864`、`AI_IMAGE_TARGET_LONG_EDGE=1280`、`AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS=2` 和 `AI_MEDIA_CONCURRENCY=4`。身份不匹配时服务会在启动轮询前退出。

模型及服务层字段依据 [OpenAI GPT-5.6 Sol 模型说明](https://developers.openai.com/api/docs/models/gpt-5.6-sol) 和 [Responses API 参考](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)。

## 3. 当前生产部署

VM `192.168.0.35` 使用原生 systemd，不依赖 Docker：

| 项目 | 位置 |
|---|---|
| 服务 | `nexa-guardian.service` |
| 当前版本 | `/opt/nexa-guardian/current` |
| 发布目录 | `/opt/nexa-guardian/releases/` |
| 密钥配置 | `/etc/nexa-guardian/guardian.env`，`root:root 0600` |
| 数据库 | `/var/lib/nexa-guardian/guardian.db` |
| 备份 | 每日备份写入 `/var/backups/nexa-guardian/`；发布前手工备份写入仅 root 可访问的 `manual/` |
| 运行用户 | `nexa-guardian` |

常用检查：

```bash
ssh root@192.168.0.35 'systemctl status nexa-guardian.service --no-pager'
ssh root@192.168.0.35 'journalctl -u nexa-guardian.service -n 100 --no-pager'
```

生产 unit 模板位于 `deploy/systemd/`。主服务使用 `nexa-guardian.service`，数据库备份由 `nexa-guardian-backup.timer` 调度。发布采用独立 release 目录和原子 `current` 软链接，配置、数据库及备份均不放入代码目录。

## 4. 可选 Docker 部署

服务器需安装 Docker Engine 和 Compose 插件。

```bash
docker compose up -d --build
docker compose logs -f guardian
```

修改 `.env` 后：

```bash
docker compose up -d --force-recreate guardian
```

SQLite 存在 Docker 命名卷 `guardian-data`。创建一致性在线备份：

```bash
docker compose exec guardian python -m nexa_guardian.backup /data/backups
docker compose cp guardian:/data/backups ./backups
```

备份程序会调用 SQLite Online Backup API，并对备份执行 `PRAGMA quick_check` 和 `PRAGMA foreign_key_check`。systemd timer 每天北京时间 03:30 执行，标准命名的自动备份保留 30 天；手工创建的子目录不参与自动清理。恢复前应先停止服务并另存当前数据库，不要直接覆盖正在写入的文件。

## 5. 本地运行与测试

要求 Python 3.10+；推荐 `uv`：

```bash
uv sync --extra dev
PYTHONPATH=src uv run python -m nexa_guardian
```

验证：

```bash
PYTHONPATH=src uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright src
uv run pytest --cov=src/nexa_guardian --cov-fail-under=75
```

## 6. 管理命令

菜单只保留五个入口。群内封禁和单条删除推荐“回复目标消息 + 命令”；唯一授权管理员也可私聊机器人并使用数字 ID。

```text
/help                         极简说明
/status                       权限、AI、抽奖及全群清理状态
/ban                          回复成员消息或私聊填写用户 ID
/del                          删除回复的群消息；私聊可填写消息 ID
/del all                      立即清理群历史，不锁群、不二次确认
/lottery 30m 1 参与 奖品      创建关键词抽奖并置顶
/lottery end|cancel|reroll    立即开奖、取消或重抽
```

抽奖持续时间为 1 分钟至 30 天，中奖人数为 1–20；参与关键词是 1–32 字符且不含空格的单词或短码。同一群同时只允许一个进行中的抽奖。用户发送整条精确匹配的关键词后登记参与，原消息立即删除，确认回复约 30 秒后清理；报名和开奖都会核验当前群成员身份，使用安全随机源无放回抽取。进行中的抽奖卡是 30 秒清理规则的唯一群消息例外；开奖或取消时立即解除该卡片置顶，结果进入正常清理，管理员私聊结果永久保留。无法置顶时整场创建会安全回滚。

机器人在群聊和日志群发送的消息会在首次发送成功约 30 秒后删除；编辑“正在审核…”不会重置时间。删除队列只保存 Telegram 消息标识和到期时间，服务重启后会继续补删。删除使用独立于长轮询的 Telegram 连接，单次请求超时后保留队列并退避重试。唯一授权管理员的私聊回复永久保留，不进入删除队列。

`/del all` 依赖在 VM 上预先登录的 `@China_OpenAi` 用户会话；未配置时 `/status` 明确显示待配置并拒绝执行。任务按最多 100 条分批删除，限流不超过 300 秒时等待一次，批量失败退回逐条删除，最后追加两轮扫描。

机器人会核验命令发起者在 Telegram 中的实际管理员权限：没有“限制成员”权限的管理员不能借机器人禁言或封禁，没有“删除消息”权限的管理员不能借机器人删除消息。群主和管理员不会被自动过滤，也不能成为禁言/封禁目标。

如果配置了 `AUTHORIZED_ADMIN_IDS`，手动命令只接受名单内用户。匿名管理员消息不会直接携带真实用户 ID；机器人仅在群内恰好存在一名匿名人类管理员、且该账号位于授权名单时放行命令。若存在多个匿名管理员而无法唯一归属，机器人会拒绝命令并要求关闭“保持匿名”。匿名管理员消息仍会被自动过滤逻辑安全跳过。

## 7. 上线验证顺序

1. 执行完整 CI，并在受保护环境文件中显式配置 `EXPECTED_BOT_ID`、
   `EXPECTED_BOT_USERNAME`、`UPDATE_CONCURRENCY_LIMIT=32`、`AI_MODEL=gpt-5.6-sol`、
   `AI_REASONING_EFFORT=high`、`AI_NORMAL_REASONING_EFFORT=low`、
   `AI_SUSPICIOUS_REASONING_EFFORT=high`、`AI_NORMAL_ACCEPT_CONFIDENCE=0.90`、
   `AI_SERVICE_TIER=default`、`AI_IMAGE_MAX_BYTES=1572864`、
   `AI_IMAGE_TARGET_LONG_EDGE=1280`、`AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS=2`、
   `AI_MEDIA_CONCURRENCY=4`、`AI_ENABLED=true`、`WARN_LIMIT=3`、
   `NOTICE_TTL_SECONDS=30` 和 `AI_FAILURE_DELETE=true`。
2. 运行发布脚本；脚本先备份配置、构建候选版本，并用不含群消息的请求验证 Responses API、
   结构化输出、图片输入、low/high 推理响应契约及上述生产设置；通过后才短暂停服，创建并校验最终一致的 SQLite 备份并原子切换。
3. 上线后验证服务、唯一管理员私聊权限、群内临时消息补删队列，并确认私聊回复不入队。
4. 用非管理员测试账号验证正常聊天静默、可疑提示、广告删除和第三次警告封禁。
5. 验证 AI 超时时只删除消息、不增加警告，并确认连续失败告警与 `/aipause` 可用。
6. 验证 `/inactive` 只显示 v0.3.0 上线后观察到的活动元数据；不要为验收而实际清理生产成员。

网关凭据写入受保护环境文件后，可在候选 release 的虚拟环境中执行：

```bash
/path/to/candidate/.venv/bin/nexa-guardian-probe \
  --env-file /etc/nexa-guardian/guardian.env \
  --require-production-settings
```

探测只发送固定的无害文字和 1×1 测试图片，不读取或发送群消息；只有网关明确回显
`completed`、`gpt-5.6-sol`、请求对应的 `low/high` 和 `default`，且全部生产配置契约通过后，才允许切换生产 release。

轮询启动时如果发现该 Token 已经配置 webhook，程序会拒绝启动，**不会自动删除现有 webhook**。这是为了避免误用现有客服机器人 Token 时切断原服务。
