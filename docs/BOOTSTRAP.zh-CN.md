# 空 Ubuntu 主机首次安装

[English](BOOTSTRAP.en.md) | 简体中文

本指南用于在一台**全新的 Ubuntu 24.04（systemd）主机**上首次安装。它会写入系统配置，最后阶段会连接 Telegram 和模型服务商并开始真实的群管理，所以不要在已经装过本项目的主机上照搬。`deploy/release.sh` 是升级工具，要求主机上已有运行中的服务、账号、数据库、配置和已登录的 MTProto 会话，不能用来初始化空主机。Docker 是另一种安装方式，见[首页](../README.md#用-docker-运行)。

## 先准备，不启动机器人

从仓库的 Releases 把 v0.7.0 的全部附件下载到 `/root/nexa-guardian-v0.7.0`。没有发行版时，在本地按[首页「开发」一节](../README.md#开发)执行 `uv build` 和 `python scripts/build_release.py`，把 `dist/` 里的文件传到该目录。以下命令的空机检查失败时立即停止，改用升级或恢复流程。保持同一个 root Bash 会话，以便 `artifact_dir` 和 `release_dir` 一直有效。这些命令需要访问软件源，但不会启动机器人。

```bash
# Run as root, only on a fresh Ubuntu host.
set -euo pipefail
umask 0077
artifact_dir=/root/nexa-guardian-v0.7.0
cd "$artifact_dir"
sha256sum -c SHA256SUMS
test ! -e /opt/nexa-guardian && test ! -L /opt/nexa-guardian
test ! -e /opt/projects/nexa-guardian/runtime
test ! -e /var/lib/nexa-guardian/guardian.db
apt-get update
apt-get install -y python3 python3-venv ca-certificates util-linux nano
getent group nexa-guardian >/dev/null || groupadd --system nexa-guardian
getent passwd nexa-guardian >/dev/null || useradd --system \
  --gid nexa-guardian --home-dir /var/lib/nexa-guardian \
  --no-create-home --shell /usr/sbin/nologin nexa-guardian
install -d -o root -g nexa-guardian -m 0750 \
  /opt/projects/nexa-guardian/runtime /opt/projects/nexa-guardian/runtime/releases
install -d -o root -g root -m 0700 /etc/nexa-guardian
install -d -o nexa-guardian -g nexa-guardian -m 0700 \
  /var/lib/nexa-guardian /var/lib/nexa-guardian/mtproto
install -d -o root -g nexa-guardian -m 1770 /var/backups/nexa-guardian
install -d -o root -g root -m 0700 /var/backups/nexa-guardian/manual
release_sha=$(python3 -c 'import json; print(json.load(open("release-metadata.json"))["source_commit"])')
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]]
release_id="$(date -u +%Y%m%dT%H%M%SZ)-${release_sha:0:12}"
release_dir="/opt/projects/nexa-guardian/runtime/releases/$release_id"
install -d -o root -g nexa-guardian -m 0750 "$release_dir"
python3 -m venv "$release_dir/.venv"
"$release_dir/.venv/bin/python" -m pip install \
  -c requirements-runtime.txt ./nexa_guardian-0.7.0-py3-none-any.whl
"$release_dir/.venv/bin/python" -m pip check
install -o root -g nexa-guardian -m 0640 release-metadata.json SHA256SUMS \
  requirements-runtime.txt nexa_guardian-0.7.0-py3-none-any.whl "$release_dir/"
chown -R root:nexa-guardian "$release_dir"
chmod -R u=rwX,g=rX,o= "$release_dir"
tar -xzf nexa-guardian-0.7.0-deployment.tar.gz
install -o root -g root -m 0600 nexa-guardian-0.7.0/.env.example \
  /etc/nexa-guardian/guardian.env
nano /etc/nexa-guardian/guardian.env
```

在编辑器里填写真实 Token、预期的机器人 ID 和用户名、唯一管理员的数字 ID，以及群的数字 ID 白名单。`DATABASE_PATH` 设为 `/var/lib/nexa-guardian/guardian.db`。AI 相关配置按你自己的服务商填写接口地址、密钥和模型，其余参数照模板逐项写明（见[运维指南](OPERATIONS.zh-CN.md)）。MTProto 的 API ID 和 Hash 必须成对配置，会话路径用下文的值。不要把凭据写进命令、Shell 历史或 Git。`DRY_RUN` 不保护手工命令。确认这个机器人没有被其他部署共用、没有配置 webhook，并在启用前检查它在群里的管理员权限。

## 初始化持久状态并认证管理员

数据库初始化只在本地进行，已有数据库时会拒绝。接下来的交互命令以管理员本人的身份登录 Telegram，需要输入真实的验证码和两步验证密码，但不会发送群消息或管理命令。备份单元和升级工具都要求这个真实会话存在，不要创建空的占位会话来绕过检查。身份校验失败时停下来，不要继续安装服务单元。

```bash
cd /
runuser -u nexa-guardian -- "$release_dir/.venv/bin/python" - <<'PYCODE'
import asyncio
from pathlib import Path
from nexa_guardian.storage import Storage
async def initialize():
    path = Path("/var/lib/nexa-guardian/guardian.db")
    if path.exists():
        raise SystemExit("Refusing to initialize over an existing database")
    store = Storage(path)
    try:
        await store.open()
    finally:
        await store.close()
asyncio.run(initialize())
PYCODE
runuser -u nexa-guardian -- "$release_dir/.venv/bin/python" -c '
import getpass
from telethon.sync import TelegramClient
client = TelegramClient("/var/lib/nexa-guardian/mtproto/admin", int(input("API ID: ")), getpass.getpass("API hash: "))
try:
    client.start(phone=lambda: getpass.getpass("Phone: "), code_callback=lambda: getpass.getpass("Login code: "), password=lambda: getpass.getpass("2FA password: "))
    if client.get_me().id != int(input("Authorized numeric administrator ID: ")):
        raise SystemExit("Session identity mismatch; do not start the service")
finally:
    client.disconnect()
'
chmod 0600 /var/lib/nexa-guardian/guardian.db \
  /var/lib/nexa-guardian/mtproto/admin.session
```

## 明确激活并验收

到这一步机器人会真正开始工作。网关探针会连接模型服务商，可能消耗额度。装好单元文件或启用定时器并不代表机器人可用：启动后要确认服务状态稳定、安装的版本和机器人身份正确、数据库为 Schema 8 且完整，并且数据库与会话的备份成功。查看日志时注意隐去敏感内容。

```bash
cd "$artifact_dir"
ln -s "$release_dir" /opt/projects/nexa-guardian/runtime/current
ln -s /opt/projects/nexa-guardian/runtime /opt/nexa-guardian
install -o root -g root -m 0644 \
  nexa-guardian-0.7.0/deploy/systemd/nexa-guardian.service \
  nexa-guardian-0.7.0/deploy/systemd/nexa-guardian-backup.service \
  nexa-guardian-0.7.0/deploy/systemd/nexa-guardian-backup.timer /etc/systemd/system/
"$release_dir/.venv/bin/nexa-guardian-probe" \
  --env-file /etc/nexa-guardian/guardian.env --require-production-settings
systemctl daemon-reload
systemctl enable --now nexa-guardian.service
systemctl start nexa-guardian-backup.service
systemctl enable --now nexa-guardian-backup.timer
systemctl is-active nexa-guardian.service nexa-guardian-backup.timer
systemctl show nexa-guardian.service --property=ExecMainStatus,NRestarts
systemctl show nexa-guardian-backup.service --property=Result,ExecMainStatus
"$release_dir/.venv/bin/python" -I -c \
  'from importlib.metadata import version; print(version("nexa-guardian"), version("multidict"))'
```

激活失败时停止新服务，保留配置、数据和日志用于排查，不要循环重启，也不要删除状态。之后的升级使用 `deploy/release.sh`，它自带备份和回退检查，用法见 [deploy/RELEASE.md](../deploy/RELEASE.md)。请保留 wheel、元数据和首次备份，作为首次安装的记录。
