# 空 Ubuntu 主机首次安装

[English](BOOTSTRAP.en.md) | 简体中文

本指南仅适用于**另行授权的全新 Ubuntu 24.04 systemd 主机**，不是本次发布实际执行的脚本。它会写系统配置，最后阶段连接 Telegram/模型供应商并启动真实管理，禁止在现有 VM 照搬。`deploy/release.sh` 是升级工具，要求已有运行中服务、账号、数据库、配置和真实 MTProto 会话，不能初始化空主机。Docker 是另一个可选安装方式。

## 先准备，不启动机器人

通过已认证的私有 GitHub 连接把 v0.6.8 全部资产下载到 `/root/nexa-guardian-v0.6.8`，独立核对 tag 与完整提交。以下命令的空机检查失败时立即停止，改用已有主机升级/恢复流程。保持同一 root Bash 会话，后续会使用 `artifact_dir` 与 `release_dir`。安装需要包索引，以下步骤不启动轮询。

```bash
# Run as root only on a fresh, separately authorized Ubuntu host.
set -euo pipefail
umask 0077
artifact_dir=/root/nexa-guardian-v0.6.8
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
  -c requirements-runtime.txt ./nexa_guardian-0.6.8-py3-none-any.whl
"$release_dir/.venv/bin/python" -m pip check
install -o root -g nexa-guardian -m 0640 release-metadata.json SHA256SUMS \
  requirements-runtime.txt nexa_guardian-0.6.8-py3-none-any.whl "$release_dir/"
chown -R root:nexa-guardian "$release_dir"
chmod -R u=rwX,g=rX,o= "$release_dir"
tar -xzf nexa-guardian-0.6.8-deployment.tar.gz
install -o root -g root -m 0600 nexa-guardian-0.6.8/.env.example \
  /etc/nexa-guardian/guardian.env
nano /etc/nexa-guardian/guardian.env
```

在私有编辑器填写真实 Token、预期机器人 ID/用户名、唯一管理员和数字群白名单。`DATABASE_PATH` 设置为 `/var/lib/nexa-guardian/guardian.db`；AI 按运维指南填写官方地址/密钥和完整 `none` 参数。MTProto API ID/hash 成对设置，路径使用下文值。凭据不得出现在命令参数、shell 历史和 Git。`DRY_RUN` 不保护手工命令；激活前核实 Token 不被其他部署共用、无 webhook，并审阅群管理权限。

## 初始化持久状态并认证管理员

数据库初始化仅在本地执行，存在数据库时拒绝。后续交互命令以管理员本人身份登录 Telegram，须明确授权并输入真实验证码/两步密码，不发送群消息或管理命令。正式备份单元与升级工具要求真实会话，不能造空 Session 绕过门槛。身份不符时停止，不安装并启动服务。

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

只有真实机器人活动与网关探针已获授权才能继续。探针会连接模型供应商，可能消耗额度。安装单元或启动定时器不等于机器人可用；启动后核实稳定状态、真实安装版本/身份、Schema 8 完整性和数据库/会话成对备份成功。只看脱敏日志，发布包验收不需要测试消息、删除、封禁或抽奖。

```bash
cd "$artifact_dir"
ln -s "$release_dir" /opt/projects/nexa-guardian/runtime/current
ln -s /opt/projects/nexa-guardian/runtime /opt/nexa-guardian
install -o root -g root -m 0644 \
  nexa-guardian-0.6.8/deploy/systemd/nexa-guardian.service \
  nexa-guardian-0.6.8/deploy/systemd/nexa-guardian-backup.service \
  nexa-guardian-0.6.8/deploy/systemd/nexa-guardian-backup.timer /etc/systemd/system/
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

激活失败时停止新服务，保留配置、数据和日志诊断，不循环重启、不删状态。空主机没有旧生产部署可回滚；后续升级走 `deploy/release.sh` 原有备份/回退门槛。保留 wheel、元数据和受保护初始备份作为首次安装记录。本指南按现有源码/单元核对，未在生产执行。
