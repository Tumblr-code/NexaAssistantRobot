# Fresh Ubuntu installation

English | [简体中文](BOOTSTRAP.zh-CN.md)

This is an operator runbook for a **new, separately authorized Ubuntu 24.04
systemd host**, not a script executed by this GitHub release. It writes system
configuration; the final phases authenticate to Telegram/model providers and
start real moderation. Do not run it on the existing VM. `deploy/release.sh`
is an upgrade tool: it requires existing active services, account, database,
configuration and an authenticated MTProto session, so it cannot bootstrap an
empty machine. Docker installation is an alternative, not a prerequisite.

## Prepare without starting the bot

Download all v0.6.8 assets to `/root/nexa-guardian-v0.6.8` through an authenticated
private-repository session. Record the release commit and independently check
it against the tag. The commands below require a fresh target; a failed guard
means stop and use the existing-host upgrade/recovery process. Keep this root
Bash session open so `artifact_dir` and `release_dir` stay defined. These
commands need package-index access; they do not start polling.

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

In the private editor fill the real bot token, expected bot ID/username, one
administrator ID and numeric group allowlist. Set `DATABASE_PATH` to
`/var/lib/nexa-guardian/guardian.db`; use the production AI contract in the
operations guide, including explicit official endpoint/key and `none` fields.
Configure MTProto API ID/hash as a pair and the session path shown below. Do
not copy credentials into a command, shell history or Git. `DRY_RUN` does not
protect manual commands. Confirm the bot is not shared with another deployment
or using a webhook, and review group administrator permissions before activation.

## Initialize persistent state and authenticate the administrator

Database initialization below is local-only and refuses an existing database.
The next interactive command logs into Telegram using the administrator's own
account; it requires explicit authorization and a real login code/2FA, but sends
no group message or moderation command. The production backup unit and guarded
upgrade flow require this real session. Do not create an empty dummy session to
satisfy the gate. If identity verification fails, stop before installing units.

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

Only proceed when real bot activity and the gateway probe are authorized. The
probe contacts the model provider and may consume quota. Installing unit files
or enabling the timer alone does not prove the bot works. After startup verify
stable service status, the correct installed version/identity, schema 8 database
integrity and a successful paired database/session backup. Inspect redacted
logs; no test message, deletion, ban or raffle is necessary for package acceptance.

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

If activation fails, stop the new service and retain its configuration/data and
logs for diagnosis; do not loop restarts or delete state. No prior production
installation exists to roll back on this fresh host. Subsequent upgrades use
`deploy/release.sh` and its existing backup/rollback checks. Preserve the wheel,
metadata and protected initial backup as the first-install record. This runbook
was reviewed against the current units/source; it was not executed on production.
