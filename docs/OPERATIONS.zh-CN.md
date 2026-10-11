# 运维指南

[English](OPERATIONS.en.md) | 简体中文 · [首页](../README.md)

首次安装见[首次安装指南](BOOTSTRAP.zh-CN.md)。这里讲装好之后的事：配置、升级、回退、备份、恢复和排查。数据库结构版本为 **Schema 9**。

## 文件位置

按首次安装指南装好后：

| 内容 | 位置 |
| --- | --- |
| 各版本与 `current` 链接 | `/opt/projects/nexa-guardian/runtime/` |
| 兼容链接 | `/opt/nexa-guardian/current` |
| 配置与密钥 | `/etc/nexa-guardian/guardian.env`，root 所有、0600 |
| 数据库 | `/var/lib/nexa-guardian/guardian.db` |
| 管理员 MTProto 会话 | `/var/lib/nexa-guardian/mtproto/admin.session` |
| 备份 | `/var/backups/nexa-guardian/`；手工副本放 `manual/` |

`deploy/systemd/` 里的服务单元以非 root 用户运行，并带文件系统保护；配置和数据都在程序目录之外。Docker 方式使用 `compose.yaml`、非 root 镜像和持久卷 `guardian-data`。

## 配置

`.env.example` 是完整的变量清单。修改 `/etc/nexa-guardian/guardian.env` 后重启服务生效。

| 配置 | 说明 |
| --- | --- |
| `BOT_TOKEN`、`EXPECTED_BOT_ID`、`EXPECTED_BOT_USERNAME` | 锁定机器人身份，不一致时拒绝启动 |
| `ALLOWED_CHAT_IDS`、`AUTHORIZED_ADMIN_IDS` | 群白名单和管理员；私聊管理要求恰好一名管理员 |
| `DRY_RUN`、`AI_FAILURE_DELETE`、`WARN_LIMIT` | 观察模式、模型故障时的处理、处罚上限 |
| `AI_ENABLED`、`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`AI_MODEL` | 模型服务，由你自己选择；密钥只放在 0600 的配置文件里 |
| `AI_API_STYLE`、`AI_STRUCTURED_OUTPUT`、`AI_STRICT_RESPONSE` | 接口风格、结构化输出方式、是否要求响应回显 |
| `AI_REASONING_EFFORT`、`AI_SERVICE_TIER` | 可选的请求参数，留空则不发送 |
| `BRAND_NAME`、`COMMUNITY_NAME`、`OFFICIAL_SITES`、`AI_MODEL_LABEL` | 机器人的身份和你的官方站点 |
| `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_PATH` | 可选的管理员会话，供 `/del all` 和 `/del members` 使用 |

换模型或服务商之后，先探测再重启服务：

```sh
/opt/nexa-guardian/current/.venv/bin/nexa-guardian-probe --env-file /etc/nexa-guardian/guardian.env
```

探测只发送固定的无害文字和 1×1 测试图片，不涉及群消息；它会真实调用模型，可能消耗额度。加上 `--require-production-settings` 时，还会要求机器人身份、模型、阈值、超时和并发等每一项都在配置文件里明确写出且格式正确，并且 `AI_ENABLED=true`、`AI_FAILURE_DELETE=true`、`DRY_RUN=false`。升级工具使用的就是这种带检查的探测。

## 升级

1. 得到新版本的 wheel 和 `SHA256SUMS`，校验后放到服务器上的一个绝对路径。
2. 运行升级工具：

```sh
sudo deploy/release.sh --wheel /absolute/path/nexa_guardian-X.Y.Z-py3-none-any.whl \
  --git-sha <完整提交号> --schema-version 9
```

它会备份配置、在独立目录里安装候选版本并探测模型，全部通过后才短暂停止服务，做最终的数据库备份并原子切换 `current`，再做健康检查；任何一步失败都会停下并恢复。不要手工修改 `current`。参数和细节见 [deploy/RELEASE.md](../deploy/RELEASE.md)。

升级时顺便换模型，加 `--ai-model <模型 ID>`；连服务商一起换，再加 `--ai-credentials-file`，指向一个只含 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY` 两行的 0600 文件。

**从 0.7.x 升级到 0.8.0：** 这一版把数据库升到 Schema 9。首次启动时会删除旧版本留下的三张数据表及相关标记，其中的数据不再保留，而且无法降回 Schema 8。升级前请确认已有备份；需要回到旧版本时，只能用升级前的数据库备份恢复。

Docker 方式升级：`git pull` 后执行 `docker compose up -d --build`，不要删除数据卷。

进程启动成功不等于升级完成，升级后请在群里实际验证：`/status`、一条正常消息不被打扰、一条可疑消息出现审核提示。

## 回退

升级过程中的失败由升级工具自动回退。升级完成后才发现问题时：

- 先用 `/status ai pause` 暂停 AI 审核，或设 `DRY_RUN=true`，停止新的自动处置。
- 保存当前数据库的备份，确认升级后是否已经写入了新数据。
- 优先向前修复。不要为了运行旧代码而直接用旧数据库覆盖现有数据库；确需恢复时按下文“恢复”的步骤做。

## 备份

```sh
/opt/nexa-guardian/current/.venv/bin/python -m nexa_guardian.backup /protected/backup-directory
```

备份使用 SQLite Online Backup，并校验 `quick_check` 和 `foreign_key_check`，服务运行时也能做。自带的定时器每天执行一次，标准命名的自动备份保留 30 天，`manual/` 目录不参与自动清理。Docker 方式：

```sh
docker compose exec guardian python -m nexa_guardian.backup /data/backups
docker compose cp guardian:/data/backups ./backups
```

数据库备份不包含配置文件和管理员会话，这两样要另外保管。不要只复制正在使用的数据库文件，那样得到的可能是不一致的副本。备份里有群管理记录和凭据，不要上传到公开位置。

## 恢复

先在隔离目录里演练：核对校验和、完整性、Schema 版本和预期的统计数字，演练时不要用真实会话启动机器人。

正式恢复：停止服务，把现有数据库连同 WAL、SHM 文件另存一份，按原来的属主和权限放入一致性副本并校验，然后再启动。

## 故障排查

| 现象 | 检查 |
| --- | --- |
| 启动时身份校验失败 | 核对 `EXPECTED_BOT_ID`、`EXPECTED_BOT_USERNAME` 是否对应这个 Token |
| 启动时提示已配置 webhook | 这个 Token 正被别的服务以 webhook 方式使用；程序不会替你删除它 |
| 不管理目标群 | 检查 `ALLOWED_CHAT_IDS` 和机器人在群里的管理员权限；`DRY_RUN` 只影响自动处置 |
| AI 超时、429 或响应格式错误 | 用 `/status ai` 看耗时和错误类型；用探测命令确认模型和接口风格；必要时改 `AI_API_STYLE`、`AI_STRUCTURED_OUTPUT` |
| 响应被判为“意外的模型” | 服务商返回的模型名和请求的不一致；除非确实需要，把 `AI_STRICT_RESPONSE` 设为 `false` |
| `/del all`、`/del members` 不可用 | 核对会话文件路径和属主，`TELEGRAM_API_ID` 与 `TELEGRAM_API_HASH` 是否成对配置 |
| 备份失败 | 看定时器的退出码、磁盘空间和目录权限；不要通过删除主库来消除报错 |

日志：`systemctl status nexa-guardian.service` 和 `journalctl -u nexa-guardian.service`。把日志发给别人之前，先隐去 Token、会话、用户消息和身份信息。
