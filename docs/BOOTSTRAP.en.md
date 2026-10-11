# Fresh Ubuntu installation

English | [简体中文](BOOTSTRAP.zh-CN.md)

This guide installs the bot for the first time on a **new Ubuntu 24.04 host
with systemd**. It writes system configuration, and its final phases connect to
Telegram and your model provider and start real moderation, so do not repeat it
on a host that already runs this project. `deploy/release.sh` is an upgrade
tool: it requires existing active services, account, database, configuration and
an authenticated MTProto session, so it cannot bootstrap an empty machine.
Docker is an alternative; see the [README](../README.en.md#run-with-docker).

## Prepare without starting the bot

Download all v0.8.0 assets from the repository's Releases to
`/root/nexa-guardian-v0.8.0`. If there is no release, run `uv build` and
`python scripts/build_release.py` locally as described in the
[README](../README.en.md#development) and copy the files in `dist/` there. The
commands below require a fresh target; a failed guard means stop and use the
upgrade or recovery process instead. Keep this root Bash session open so
`artifact_dir` and `release_dir` stay defined. The commands need package-index
access; they do not start the bot.

```bash
# Run as root, only on a fresh Ubuntu host.
set -euo pipefail
umask 0077
artifact_dir=/root/nexa-guardian-v0.8.0
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
  -c requirements-runtime.txt ./nexa_guardian-0.8.0-py3-none-any.whl
"$release_dir/.venv/bin/python" -m pip check
install -o root -g nexa-guardian -m 0640 release-metadata.json SHA256SUMS \
  requirements-runtime.txt nexa_guardian-0.8.0-py3-none-any.whl "$release_dir/"
chown -R root:nexa-guardian "$release_dir"
chmod -R u=rwX,g=rX,o= "$release_dir"
tar -xzf nexa-guardian-0.8.0-deployment.tar.gz
install -o root -g root -m 0600 nexa-guardian-0.8.0/.env.example \
  /etc/nexa-guardian/guardian.env
nano /etc/nexa-guardian/guardian.env
```

In the editor fill in the real bot token, the expected bot ID and username, the
single administrator's numeric ID and the numeric group allowlist. Set
`DATABASE_PATH` to `/var/lib/nexa-guardian/guardian.db`. For AI review enter your
own provider's endpoint, key and model, and spell out the remaining settings
from the template (see the [operations guide](OPERATIONS.en.md)). Configure the
MTProto API ID and hash as a pair, with the session path shown below. Do not
copy credentials into a command, shell history or Git. `DRY_RUN` does not
protect manual commands. Confirm the bot is not shared with another deployment
or using a webhook, and review its administrator permissions in the group
before activation.

## Initialize persistent state and authenticate the administrator

Database initialization below is local-only and refuses an existing database.
The next interactive command logs into Telegram as the administrator; it needs a
real login code and 2FA password, but sends no group message or moderation
command. The backup unit and the upgrade tool both require this real session.
Do not create an empty dummy session to satisfy the check. If identity
verification fails, stop before installing units.

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

## Explicit activation and acceptance

From here the bot really starts working. The gateway probe contacts your model
provider and may consume quota. Installing unit files or enabling the timer does
not prove the bot works: after startup verify stable service status, the correct
installed version and bot identity, schema 9 database integrity and a successful
paired database and session backup. Redact logs before sharing them.

```bash
cd "$artifact_dir"
ln -s "$release_dir" /opt/projects/nexa-guardian/runtime/current
ln -s /opt/projects/nexa-guardian/runtime /opt/nexa-guardian
install -o root -g root -m 0644 \
  nexa-guardian-0.8.0/deploy/systemd/nexa-guardian.service \
  nexa-guardian-0.8.0/deploy/systemd/nexa-guardian-backup.service \
  nexa-guardian-0.8.0/deploy/systemd/nexa-guardian-backup.timer /etc/systemd/system/
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

If activation fails, stop the new service and keep its configuration, data and
logs for diagnosis; do not loop restarts or delete state. Later upgrades use
`deploy/release.sh`, which has its own backup and rollback checks; see
[deploy/RELEASE.md](../deploy/RELEASE.md). Keep the wheel, metadata and the first
backup as the record of this installation.
