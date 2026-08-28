# Nexa Guardian

一个独立、自托管的 Telegram 群管理机器人，面向广告删除、刷屏控制、警告和禁言。它不会改动现有客服机器人，也不会自动接管未明确列入白名单的群。

当前状态：已于 2026-08-27 部署到 VM `SERVER-IP`，由 `nexa-guardian.service` 持久运行；目标为 `@ExampleGroup`，正式管理模式已启用。部署后的 Telegram 身份、群权限、轮询、SQLite 完整性和在线备份均已验证。

## 已实现

- 自动删除非白名单外链，包括藏在文字中的 `text_link`。
- 可配置广告词；会归一化全角字符、零宽字符、空格和标点，识别简单规避写法。
- 可选删除转发内容和 `@账号` 推广；默认关闭，避免误伤正常讨论。
- 限速和重复消息检测。
- 违规累计警告，达到阈值后自动禁言并清零。
- 管理员命令：警告、禁言、解禁、封禁、解封、移出、删消息。
- 每群独立的可信用户、域名白名单、拦截词和 SQLite 状态。
- 可选独立日志群；日志不保存完整消息正文或 Token。
- `DRY_RUN` 观察模式、群 ID 白名单、在线 SQLite 备份、systemd/Docker 非 root 运行。

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

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `BLOCK_EXTERNAL_LINKS` | `true` | 删除非白名单外链 |
| `AUTHORIZED_ADMIN_IDS` | 空 | 可执行手动命令的数字用户 ID；仍需具备相应群权限 |
| `ALLOWED_DOMAINS` | 空 | 全局允许的域名及其子域名 |
| `BLOCKED_WORDS` | 空 | 全局广告词，英文逗号分隔 |
| `BLOCK_FORWARDS` | `false` | 是否删除普通成员的转发内容 |
| `BLOCK_CHANNEL_MENTIONS` | `false` | 是否拦截推广语境中的 `@账号` 引流 |
| `WARN_LIMIT` | `3` | 自动禁言前的违规次数 |
| `DEFAULT_MUTE_SECONDS` | `3600` | 默认禁言秒数 |
| `DRY_RUN` | `false` | 只记录自动命中，不删除/警告/禁言；手动管理员命令仍执行 |
| `LOG_CHAT_ID` | 空 | 可选的集中管理日志群/频道 |

`BLOCK_CHANNEL_MENTIONS=true` 使用推广语境判断：带“加我、进群、代理、兼职、投资群”等引流提示，或一条消息同时推广多个账号时才拦截；普通单个 `@成员` 提及会保留。拦截词会先进行 Unicode、全半角、空格和零宽字符归一化，可识别“加 微​信”等简单规避写法。

## 3. 当前生产部署

VM `SERVER-IP` 使用原生 systemd，不依赖 Docker：

| 项目 | 位置 |
|---|---|
| 服务 | `nexa-guardian.service` |
| 当前版本 | `/opt/nexa-guardian/current` |
| 发布目录 | `/opt/nexa-guardian/releases/` |
| 密钥配置 | `/etc/nexa-guardian/guardian.env`，`root:root 0600` |
| 数据库 | `/var/lib/nexa-guardian/guardian.db` |
| 备份 | `/var/backups/nexa-guardian/`，每天北京时间 03:30 自动执行 |
| 运行用户 | `nexa-guardian` |

常用检查：

```bash
ssh root@SERVER-IP 'systemctl status nexa-guardian.service --no-pager'
ssh root@SERVER-IP 'journalctl -u nexa-guardian.service -n 100 --no-pager'
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
```

## 6. 管理命令

群内多数成员操作推荐“回复目标成员消息 + 命令”，这样不依赖用户名解析。配置了唯一的 `ALLOWED_CHAT_IDS` 后，`AUTHORIZED_ADMIN_IDS` 中仍具备该群管理员身份的用户也可私聊机器人操作；私聊时必须把数字用户 ID 放在命令后，例如 `/mute 123456789 2h`。`/status`、`/settings`、`/stats`、规则白名单等命令可直接私聊发送。

```text
/warn                         警告
/mute [1m|10m|2h|1d]         禁言
/unmute                      解除禁言并恢复群默认权限
/ban                          封禁并删除历史消息
/unban 用户ID                解除封禁
/kick                         移出，但允许重新加入
/del                         删除被回复的消息
/warnings /clearwarns        查询/清除警告

/trust /untrust              添加/移除可信用户
/addomain example.com        添加域名白名单
/deldomain example.com       移除域名白名单
/domains                     查看域名白名单
/addword 词1 词2             批量添加拦截词
/delword 词1 词2             批量移除拦截词
/words                       查看拦截词

/settings /stats /status /chatid /help
/recent [1-30]               私聊查看最近管理操作
```

管理动作不接收、显示或记录原因；旧版命令尾部附带的文字会被兼容忽略。`/stats` 按北京时间展示今天、近 7 天和累计数据。`/recent` 默认返回 10 条、最多 30 条，为避免在群里公开用户 ID，只能由授权管理员私聊使用。

批量拦截词使用空格、英文逗号、中文逗号或换行分隔，每次最多 20 条；需要保留空格的短语使用英文或中文引号，例如 `/addword "guaranteed profit" “长期 合作”`。`/words` 横向排列关键词，域名白名单保持逐行显示；列表过长时都会自动分段发送。

私聊 `/del` 无法引用群消息，需填写群消息 ID：`/del 群消息ID`。机器人不会仅凭私聊身份放行：每次仍会同时核验授权名单、目标群管理员身份和所需权限。若配置零个或多个目标群，私聊管理会拒绝执行，避免误操作到错误群。

机器人会核验命令发起者在 Telegram 中的实际管理员权限：没有“限制成员”权限的管理员不能借机器人禁言或封禁，没有“删除消息”权限的管理员不能借机器人删除消息。群主和管理员不会被自动过滤，也不能成为禁言/封禁目标。

如果配置了 `AUTHORIZED_ADMIN_IDS`，手动命令只接受名单内用户。匿名管理员消息不会直接携带真实用户 ID；机器人仅在群内恰好存在一名匿名人类管理员、且该账号位于授权名单时放行命令。若存在多个匿名管理员而无法唯一归属，机器人会拒绝命令并要求关闭“保持匿名”。匿名管理员消息仍会被自动过滤逻辑安全跳过。

## 7. 上线验证顺序

1. 先设 `DRY_RUN=true`，用非管理员测试账号发送外链、允许域名和普通消息；确认只记录应拦截项。
2. 执行 `/status`，确认机器人具有删除消息和限制成员权限。
3. 设 `DRY_RUN=false` 后重启，只在测试群验证删除、三次警告禁言、`/unmute`、管理员跳过和可信用户跳过。
4. 若配置 `LOG_CHAT_ID`，确认日志到达且不含完整原消息。
5. 创建首份数据库备份，再加入正式群 ID。

轮询启动时如果发现该 Token 已经配置 webhook，程序会拒绝启动，**不会自动删除现有 webhook**。这是为了避免误用现有客服机器人 Token 时切断原服务。
