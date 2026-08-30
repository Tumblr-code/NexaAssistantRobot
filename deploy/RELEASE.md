# Nexa Guardian 发布说明

`deploy/release.sh` 仅用于生产 VM，以 root 身份把一个已构建 wheel 或源码目录发布到
`/opt/nexa-guardian/releases/`。脚本不会修改或输出 `/etc/nexa-guardian/guardian.env`
的内容，但会在切换代码前创建一份 root 专用备份；脚本不会删除 `manual/` 下的手工和
发布前备份，根目录的标准每日备份仍会按 30 天策略轮换。

## 前置条件

- `nexa-guardian.service` 已按 `deploy/systemd/` 中的 unit 安装并启用。
- `/etc/nexa-guardian/guardian.env` 是 `root:root` 所有的普通文件，权限为 `0600` 或更严格的 `0400`，且不是符号链接。
- `/var/lib/nexa-guardian/guardian.db` 存在且可由 root 读取。
- `/var/lib/nexa-guardian/mtproto/admin.session` 是已授权的唯一管理员 Telethon SQLite
  会话，必须是普通非符号链接文件，且权限不得宽于 `0600`。
- `/var/backups/nexa-guardian/` 由 root 持有，`nexa-guardian` 服务组只用于写入和清理
  标准命名的每日备份；目录带 sticky bit，`manual/` 保持 `root:root 0700`。发布脚本会
  校正这些权限，不会移动或删除 `manual/` 下的已有备份。
- VM 已创建 `nexa-guardian` 用户和组，并安装 `/usr/bin/python3`、Python `venv`、`flock`、
  `getent`、`sha256sum` 和 systemd；源码模式还需要 `git`。
- 使用 `--source` 时，还需安装 Python `build`、`uv` 或可用的 `python3 -m pip`；生产发布优先使用 CI 构建的 wheel。
- 在运行脚本前，将仓库或 wheel 放到 VM 上的明确绝对路径；不要把 API Key 写入 wheel、源码或命令行。
- `/etc/nexa-guardian/guardian.env` 必须配置兼容网关的 `OPENAI_BASE_URL`、
  `OPENAI_API_KEY`、`AI_MODEL=gpt-5.6-sol`、`AI_REASONING_EFFORT=high`、
  `AI_NORMAL_REASONING_EFFORT=low`、`AI_SUSPICIOUS_REASONING_EFFORT=high` 和
  `AI_SERVICE_TIER=default`；模型与自适应推理契约必须显式存在，不接受默认补齐、家族别名或静默改写。
  还必须显式配置正确的 `EXPECTED_BOT_ID`、`EXPECTED_BOT_USERNAME`，以及
  `UPDATE_CONCURRENCY_LIMIT=32`、`AI_NORMAL_ACCEPT_CONFIDENCE=0.90`、
  `AI_IMAGE_MAX_BYTES=1572864`、`AI_IMAGE_TARGET_LONG_EDGE=1280`、
  `AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS=2`、`AI_MEDIA_CONCURRENCY=4`。
  发布脚本会用候选虚拟环境的 `nexa-guardian-probe --require-production-settings` 验证
  Responses、严格结构化输出和图片输入，并要求响应明确返回 `status=completed`、
  `model=gpt-5.6-sol`、本次请求对应的 `reasoning.effort=low/high`、
  `service_tier=default`。探针还会拒绝未启用 AI、警告阈值不是 3、消息保留时间不是 30 秒、
  机器人身份未固定或关闭故障删除的配置，同时核对发布计划固定的阈值、超时、更新/AI/媒体
  并发、缓存、保留期及图片预览限制。探测只发送固定的无害文本及 1×1 测试图片，
  不读取群消息，也不会输出密钥或响应正文。

## 发布命令

使用 CI wheel（推荐）：

```bash
sudo /path/to/repository/deploy/release.sh \
  --wheel /root/releases/nexa_guardian-0.6.0-py3-none-any.whl \
  --git-sha 0123456789abcdef0123456789abcdef01234567 \
  --schema-version 8
```

直接从已检出的源码构建：

```bash
sudo /path/to/repository/deploy/release.sh \
  --source /root/NexaAssistantRobot \
  --schema-version 8
```

当前版本的 `--schema-version` 必须明确传入 `8`，脚本会在启动后核对数据库的
`PRAGMA user_version=8`。wheel 模式必须显式提供
完整 Git SHA；源码模式只接受无未提交或未跟踪文件的干净 Git worktree，默认读取 `HEAD`，
也可通过 `--git-sha` 固定指定且必须与 `HEAD` 完全一致。

## 脚本执行顺序

1. 获取 `/run/lock/nexa-guardian-release.lock`，拒绝并发发布。
2. 将配置原子复制到
   `/var/backups/nexa-guardian/manual/pre-release-<release-id>.env`，校验内容一致并固定为
   `root:root 0600`；日志只显示备份路径，不输出配置内容。
3. 校验或构建 wheel，计算并复核 SHA256，创建服务组只读的独立虚拟环境。
4. 使用候选版本对兼容网关执行固定无害请求，验证 Responses、严格结构化输出、图片输入、
   `low/high` 自适应推理契约及服务端实际采用 `gpt-5.6-sol`、`default`；同时验证机器人身份、
   更新并发与媒体预览生产配置。任一能力或响应契约失败即停止，不切换生产。
5. 写入 `release.json` 并完成候选 release 后，停止服务并确认它不再写库。
   若数据库需要升级且仍有进行中的旧版抽奖，发布会停止并恢复原服务；同一 schema 的
   文案或代码补丁会保留活动抽奖并在新进程中继续。
6. 使用 SQLite 在线备份 API 创建最终一致的主数据库和 MTProto 会话备份：
   `/var/backups/nexa-guardian/manual/pre-release-<release-id>.db` 与同名 `.session`。
   两者均运行 `PRAGMA quick_check`、`PRAGMA foreign_key_check`，固定为
   `root:root 0600` 并记录 SHA256；日志仅显示路径，不读取或输出会话内容。
7. 将 `current` 软链接原子切换到 `<UTC timestamp>-<short SHA>` release，启动服务。
8. 检查 systemd 启用/运行状态、主进程、退出状态、自动重启次数、数据库完整性和 schema 8；再运行一次每日备份并核验同时间戳的主数据库/会话数据库备份对、`0600` 权限及 SQLite 完整性。失败时执行下述数据库与代码回滚。
9. 验证成功后，仅清理标准命名的旧代码 release，固定保留 `current` 和最近两个旧版本；
   备份目录、非标准目录及 `current` 指向的 release 不会被清理。

`release.json` 记录完整 Git SHA、UTC 构建时间、wheel SHA256、数据库 schema 版本和
wheel 文件名，可用于把 VM release 对应回 GitHub 版本与构建产物。

## 失败回滚

新版本切换后如果启动或健康检查失败，脚本会先停止服务，并且只在上线前数据库备份已经
完成校验、SHA256 与创建时一致、文件仍位于受保护的 `manual` 目录且原数据库属主和权限
已经捕获时继续。备份会先
复制到数据库同目录的临时文件，再次通过 `quick_check` 和 `foreign_key_check` 后原子替换
生产数据库；随后清除与新数据库对应的 WAL、SHM 和 rollback journal，保留原属主与权限，
并对恢复后的生产数据库再次执行完整性检查。最后才切回旧 `current` 并启动旧服务。

如果候选构建、网关探测或停服前检查失败，生产服务保持运行；停服后、切换前失败时，脚本
会重新启动原服务。若有效备份不存在、服务无法确认停止、数据库恢复失败或旧 release 无法
恢复，脚本不会盲目改写数据库或启动不兼容的代码，并会输出明确的 CRITICAL 错误供人工处理。
只有无法安全恢复或启动旧版本时，服务才会保持停止状态等待人工处理。
上线前的数据库、MTProto 会话和配置备份均会保留。自动回滚只替换本次发布可能迁移的
主数据库；MTProto 会话备份作为人工灾难恢复副本保留，不会在普通代码回滚时覆盖仍有效的
线上会话。

## 上线后核验

```bash
systemctl is-active nexa-guardian.service
systemctl is-enabled nexa-guardian.service
systemctl show nexa-guardian.service \
  --property=MainPID,ExecMainStatus,NRestarts
readlink -f /opt/nexa-guardian/current
cat /opt/nexa-guardian/current/release.json
systemctl status nexa-guardian-backup.timer --no-pager
```

每日备份服务会使用同一时间戳分别生成 `guardian-*.db` 和
`telegram-session-*.session`，两类 SQLite 备份均通过完整性校验、权限为 `0600`，并共同按
30 天策略清理。Docker 部署固定使用持久卷内的 `/data/mtproto/admin.session`；systemd 部署
固定使用 `/var/lib/nexa-guardian/mtproto/admin.session`。

脚本只验证服务和 SQLite 的基础健康状态。Telegram 权限、管理员私聊、Nexa Guardian 官方
群管理审核员身份、三个一方站点的精确域名行为、AI 网关 Sol/low-high/default 响应契约、
群内与日志群 30 秒消息清理、授权管理员私聊永久保留、日志 outbox 重试及真实群组审核行为
仍需按照发布验收清单逐项检查。

启动时会按 `deploy/telegram-profile.md` 同步默认和 `zh` 的机器人名称、简短介绍、详细说明
与命令菜单。上线后应通过 Telegram API 回读两套资料，确认成员筛选、群内 30 秒清理及唯一
管理员私聊永久保留的说明均已生效；头像不由服务自动覆盖。

v0.6.0 的 `/del members` 使用已授权的 MTProto 用户会话分页读取目标群成员，并只扫描一次最近
30 天群历史。已注销、精确离线超过 30 天、`LastMonth` 或入群满 30 天且无发言，任一命中即
进入候选；`LastMonth` 受隐私设置影响，会在预览中单独标识；官方没有独立的 `LongAgo` 类型，
`UserStatusEmpty` 不作为离线依据。历史未跨过 30 天边界时不使用“无发言”条件。上线验收只在唯一管理员私聊中运行 `/del members` 查看预览，
不得发送确认命令。实际清理需要限制成员权限和 10 分钟确认码，且执行前再次排除管理员、机器人、
可信用户、已离群成员和预览后新增活动者。若解除封禁失败，批次立即停止，并由 5 分钟临时封禁
兜底。`/del all` 与成员扫描共用一个 MTProto 会话锁，不会并发打开 session 文件。
