# 运维指南

[English](OPERATIONS.en.md) | 简体中文

## 范围和配置

v0.6.8 仅整理文档、版本信息和发布包；数据库仍为 **Schema 8**。只读核验的生产安装包仍为 v0.6.7。GitHub 发布与生产部署审批相互独立。不得把真实环境文件、Session、SQLite/WAL、备份或客户标识加入资产和 CI 日志。

`.env.example` 是完整变量清单，示例不能直接作为生产配置。关键分组：

| 配置 | 操作要求 |
| --- | --- |
| `BOT_TOKEN`、`EXPECTED_BOT_ID`、`EXPECTED_BOT_USERNAME` | 锁定真实机器人身份，不一致时拒绝启动轮询 |
| `ALLOWED_CHAT_IDS`、`AUTHORIZED_ADMIN_IDS` | 数字群白名单；私聊管理员必须恰好一人 |
| `DATABASE_PATH`、`LOG_LEVEL`、`LOG_CHAT_ID` | 持久数据、安全日志级别、可选日志接收目标 |
| `DRY_RUN`、`AI_FAILURE_DELETE`、`WARN_LIMIT` | 自动观察、故障和处罚策略；手工管理员命令仍会执行 |
| `AI_ENABLED`、`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`AI_MODEL` | 可选 DeepSeek 官方接口；凭据只放 0600 文件 |
| `AI_REASONING_EFFORT` 及 normal/suspicious 兼容字段 | 固定 `none`，不自动换模型 |
| `AI_TIMEOUT_SECONDS`、`UPDATE_CONCURRENCY_LIMIT` | 生产约定为 12 秒 / 32 个更新 |
| 图片字节、目标长边、超时、并发 | 生产约定为 1572864 / 1280 / 2 / 4 |
| `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_PATH` | 可选管理员会话，禁止加入源码 |

其他阈值、缓存和刷屏参数沿用模板。生产发布探针还校验完整身份与 AI 参数约定，不得为通过发布降低门槛。网关探针会调用外部 API 并可能消耗额度，不能作为离线包测试。

## 安装和部署

先验证 `SHA256SUMS` 与 `release-metadata.json` 的完整 Git SHA。把 wheel 安装到独立 Python 3.10+ 虚拟环境，只查询安装版本，不导入 `main` 或启动轮询。依赖由 `pyproject.toml` 与 `uv.lock` 固定，CI 使用 `uv sync --frozen --extra dev`。

另行获得生产部署授权后，沿用 `deploy/release.sh --wheel /absolute/path/to/wheel --git-sha FULL_SHA --schema-version 8`。该命令确实部署，必须保留探针、受保护配置备份、停写后的最终备份、原子切换与健康检查；不得手工改 `current`。仅在确需模型迁移时审阅并使用对应可选参数。

| 资源 | 正式路径 |
| --- | --- |
| release 与 current | `/opt/projects/nexa-guardian/runtime/` |
| 兼容链接 | `/opt/nexa-guardian/current` |
| 环境文件 | `/etc/nexa-guardian/guardian.env`，root 所有、0600 |
| 数据库 | `/var/lib/nexa-guardian/guardian.db` |
| MTProto 会话 | `/var/lib/nexa-guardian/mtproto/admin.session` |
| 备份 | `/var/backups/nexa-guardian/`；手工副本在 `manual/` |

`deploy/systemd/` 模板以非 root 用户运行并保留文件系统保护，配置和数据在发布目录之外。可选 Docker 使用 `compose.yaml`、非 root 镜像和持久 `guardian-data` 卷；升级时不得删除卷。

## 升级、回退与备份恢复

变更前记录安装包与源码身份、服务状态、数据库版本、配置和会话权限，保留已验证旧 wheel 和独立备份。遵循 `deploy/RELEASE.md` 与受限脚本，完成后回读真实安装包、源码、服务和数据库完整性。CI 通过或进程启动不等于生产验收。

发布事务内部的回退由原脚本控制，并拒绝不安全的 Schema 逆向操作。上线后发现问题，先经授权停止新的管理动作，保存当前状态并核实是否发生新写入。不得为运行旧代码直接覆盖为旧数据库，优先前向修复；另行批准的恢复必须满足 Schema 兼容并核对保留状态。

使用已安装解释器进行一致性在线备份：

```sh
/opt/nexa-guardian/current/.venv/bin/python -m nexa_guardian.backup /protected/backup-directory
```

工具使用 SQLite Online Backup，校验 `quick_check` 和 `foreign_key_check`。现有定时器每天北京时间 03:30 备份，标准自动文件保留 30 天，`manual/` 不参与自动清理。私有会话和环境文件另行保护，数据库备份不能代替它们。不得只复制正在写入的数据库主文件。

恢复演练使用隔离目录，核对摘要、完整性、相同 Schema 与预期聚合计数，不得连接生产 Session 启动机器人。正式恢复须先停写，另存现有数据库及 WAL/SHM，按原权限与属主放置一致性副本并校验，再明确批准启动；不得自动重放管理命令和通知。

## 故障排查

| 现象 | 安全检查 |
| --- | --- |
| 身份校验阻止启动 | 私下比对预期数字 ID/用户名，不能输出 Token |
| 不管理目标群 | 检查数字白名单、管理员权限；`DRY_RUN` 只影响自动动作 |
| AI 超时、429 或结构错误 | 查看脱敏状态、耗时和错误码，不擅改模型或阈值 |
| MTProto 命令不可用 | 核对会话路径、属主、成对 API 配置；交互登录另行授权 |
| 备份失败 | 检查定时器退出码、空间、权限与副本完整性，不能删主库消错 |
| outbox 持续重试 | 只读查看待发/失败聚合，不能用真实重发做测试 |

可只读使用 `systemctl status nexa-guardian.service` 和范围明确的 journal。共享前脱敏 Token、Session、原始用户消息和身份。更完整的命令与配置说明见 `README.zh-CN.md`。

本发布同时把 `multidict` 固定到 `6.9.1`，修复审计发现的 [GHSA-54p9-h82j-f925](https://github.com/advisories/GHSA-54p9-h82j-f925)。锁文件与 wheel 依赖要求同步，不改变业务规则；生产尚未更新此依赖。
