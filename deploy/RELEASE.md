# 升级工具 release.sh

`deploy/release.sh` 用来把一台**已经装好并在运行**本项目的主机升级到新版本。它以 root 身份运行，不能用来做首次安装（首次安装见 [docs/BOOTSTRAP.zh-CN.md](../docs/BOOTSTRAP.zh-CN.md)）。

它把升级做成一个事务：先在不影响线上服务的前提下准备并验证候选版本，全部通过后才短暂停服、备份、切换；任何一步失败都会停下并恢复到升级前的状态。

## 前提

- 主机上已有 `nexa-guardian` 用户和组、运行中的 `nexa-guardian.service` 和备份定时器、数据库、`/etc/nexa-guardian/guardian.env`，以及已登录的管理员 MTProto 会话。
- 已安装 `/usr/bin/python3`、Python `venv`、`flock`。使用 `--source` 时还需要 `uv`、Python `build` 或可用的 `python3 -m pip`；推荐直接用 CI 或本地 `uv build` 产出的 wheel。
- 把 wheel 或源码放到主机上的一个绝对路径。不要把 API Key 写进 wheel、源码或命令行。
- 配置文件里要把机器人身份、模型、阈值、超时和并发等逐项写明，并且 `AI_ENABLED=true`、`AI_FAILURE_DELETE=true`、`DRY_RUN=false`；升级工具会用 `nexa-guardian-probe --require-production-settings` 检查。

## 用法

```bash
sudo deploy/release.sh --wheel /absolute/path/nexa_guardian-X.Y.Z-py3-none-any.whl \
  --git-sha <完整提交号> --schema-version 9

sudo deploy/release.sh --source /absolute/path/to/repository \
  [--git-sha <完整提交号>] --schema-version 9
```

可选参数：

| 参数 | 作用 |
| --- | --- |
| `--ai-model <模型 ID>` | 在本次升级里把配置中的 `AI_MODEL` 换成新值；探测用的是换好后的配置，失败则配置原样恢复 |
| `--ai-credentials-file <路径>` | 连同服务商一起更换。文件必须归当前用户所有、权限 0600、不是符号链接，且恰好只有 `OPENAI_BASE_URL`（https）和 `OPENAI_API_KEY` 两行。需要同时给出 `--ai-model` |
| `--allow-model-cooldown` | 仅当探测得到精确的 `HTTP 429 model_cooldown`、并且你确认接受模型暂时冷却时使用；其他任何探测错误仍会中止。需要同时给出 `--ai-model`，且只能出现一次 |

## 它按什么顺序做

1. 加锁，确保同一时间只有一个升级在进行；校验参数和 Schema 版本。
2. 备份受保护的配置文件（root 所有、0600）。需要更换模型时，另建一份短期的候选配置副本，不动线上服务正在读取的配置。
3. 在新的 release 目录里建独立虚拟环境并安装 wheel。
4. 用候选版本探测模型：只发送固定的无害文字和 1×1 测试图片，同时检查配置是否完整。探测失败就到此为止，线上服务不受影响。
5. 暂停备份定时器，停止服务，做两份 SQLite 一致性备份并校验。
6. 需要更换模型时，在这一刻才原子地更新线上配置里唯一的那一行 `AI_MODEL`（以及凭据）。
7. 原子切换 `current` 到新版本，启动服务并做健康检查。
8. 写入本次升级的元数据（提交号、wheel 文件名、探测策略），恢复备份定时器，清理临时文件；保留当前版本和最近的两个旧版本。

## 失败时会怎样

- 候选构建、探测或停服前的检查失败：线上服务一直在运行，什么都没变。
- 停服之后、切换之前失败：恢复升级前的配置，重新启动原来的服务。
- 切换之后健康检查失败：用停服时做的备份恢复数据库，清理对应的 WAL、SHM 和回滚日志，保持原属主和权限并再次做完整性检查，然后把 `current` 指回旧版本、恢复升级前的配置并启动旧服务。
- 如果回滚的前置条件无法满足，脚本会停下并说明原因，不会带着不确定的状态继续。

发布脚本的自动回滚只还原它自己改过的东西：`current` 链接、数据库和（使用 `--ai-model` 时的）配置。它不会改写其他环境变量。

## 升级之后

脚本成功只说明服务启动且通过了健康检查。请再在群里验证：`/status`、管理员私聊权限、正常聊天不被打扰、可疑消息出现审核提示、广告被删除并警告。验证 `/del members` 时只看预览，不要发送确认码。
