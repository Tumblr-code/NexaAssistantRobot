# Operations guide

English | [简体中文](OPERATIONS.zh-CN.md) · [Home](../README.en.md)

For a first installation see the [first-install guide](BOOTSTRAP.en.md). This guide covers what comes after: configuration, upgrades, rollback, backup, recovery and troubleshooting. The database is at **schema 8**.

## Where things live

After following the first-install guide:

| What | Where |
| --- | --- |
| Releases and the `current` link | `/opt/projects/nexa-guardian/runtime/` |
| Compatibility link | `/opt/nexa-guardian/current` |
| Configuration and secrets | `/etc/nexa-guardian/guardian.env`, owned by root, 0600 |
| Database | `/var/lib/nexa-guardian/guardian.db` |
| Administrator MTProto session | `/var/lib/nexa-guardian/mtproto/admin.session` |
| Backups | `/var/backups/nexa-guardian/`; manual copies in `manual/` |

The units in `deploy/systemd/` run as a non-root user with filesystem protection; configuration and data stay outside the program directory. The Docker variant uses `compose.yaml`, a non-root image and the persistent volume `guardian-data`.

## Configuration

`.env.example` is the complete list. Restart the service after editing `/etc/nexa-guardian/guardian.env`.

| Settings | Notes |
| --- | --- |
| `BOT_TOKEN`, `EXPECTED_BOT_ID`, `EXPECTED_BOT_USERNAME` | Pin the bot identity; a mismatch refuses to start |
| `ALLOWED_CHAT_IDS`, `AUTHORIZED_ADMIN_IDS` | Group allowlist and administrators; private-chat management needs exactly one administrator |
| `DRY_RUN`, `AI_FAILURE_DELETE`, `WARN_LIMIT` | Observation mode, behaviour when the model fails, penalty limit |
| `AI_ENABLED`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `AI_MODEL` | The model service of your choice; keep the key only in the 0600 configuration file |
| `AI_API_STYLE`, `AI_STRUCTURED_OUTPUT`, `AI_STRICT_RESPONSE` | API style, structured-output mode, whether the response must echo the request |
| `AI_REASONING_EFFORT`, `AI_SERVICE_TIER` | Optional request parameters; empty omits them |
| `BRAND_NAME`, `COMMUNITY_NAME`, `OFFICIAL_SITES`, `AI_MODEL_LABEL` | The bot's identity and your official sites |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH` | Optional administrator session for `/del all` and `/del members` |

After changing the model or provider, probe before restarting the service:

```sh
/opt/nexa-guardian/current/.venv/bin/nexa-guardian-probe --env-file /etc/nexa-guardian/guardian.env
```

The probe sends one fixed harmless sentence and a 1×1 test image, never a group message; it makes a real model call and may consume quota. With `--require-production-settings` it also requires the bot identity, model, thresholds, timeouts and concurrency to be written out and well-formed in the file, and `AI_ENABLED=true`, `AI_FAILURE_DELETE=true`, `DRY_RUN=false`. The upgrade tool runs the probe in this form.

## Upgrade

1. Get the new wheel and `SHA256SUMS`, verify them, and place the wheel at an absolute path on the server.
2. Run the upgrade tool:

```sh
sudo deploy/release.sh --wheel /absolute/path/nexa_guardian-X.Y.Z-py3-none-any.whl \
  --git-sha <full commit id> --schema-version 8
```

It backs up the configuration, installs the candidate in its own directory and probes the model; only when all of that passes does it stop the service briefly, take a final database backup, switch `current` atomically and run a health check. Any failure stops and restores. Do not edit `current` by hand. See [deploy/RELEASE.md](../deploy/RELEASE.md) (Chinese) for the options and details.

To change the model during an upgrade add `--ai-model <model id>`; to change the provider as well add `--ai-credentials-file`, pointing at a 0600 file with exactly two lines, `OPENAI_BASE_URL` and `OPENAI_API_KEY`.

With Docker: `git pull`, then `docker compose up -d --build`. Never delete the data volume.

A started process is not a finished upgrade. Verify in the group afterwards: `/status`, an ordinary message left alone, a suspicious message showing the review notice.

## Rollback

Failures during an upgrade are rolled back by the tool. If a problem appears after the upgrade completed:

- Pause AI review with `/status ai pause`, or set `DRY_RUN=true`, to stop new automatic actions.
- Take a backup of the current database and check whether new data has been written since the upgrade.
- Prefer fixing forward. Do not overwrite the current database with an old one just to run old code; if a restore is really needed, follow Recovery below.

## Backup

```sh
/opt/nexa-guardian/current/.venv/bin/python -m nexa_guardian.backup /protected/backup-directory
```

The tool uses SQLite Online Backup and verifies `quick_check` and `foreign_key_check`; it is safe while the service runs. The bundled timer runs once a day, standard-named automatic backups are kept for 30 days, and `manual/` is never pruned. With Docker:

```sh
docker compose exec guardian python -m nexa_guardian.backup /data/backups
docker compose cp guardian:/data/backups ./backups
```

A database backup does not contain the configuration file or the administrator session; protect those separately. Do not simply copy the live database file, which may give an inconsistent copy. Backups hold moderation records and credentials; never upload them anywhere public.

## Recovery

Rehearse in an isolated directory first: check the checksum, integrity, schema version and the expected aggregate counts, and do not start the bot with a real session during the rehearsal.

For a real recovery: stop the service, set aside the current database together with its WAL and SHM files, put the consistent copy in place with the original owner and permissions, verify it, and only then start the service.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Identity check fails at start-up | `EXPECTED_BOT_ID` and `EXPECTED_BOT_USERNAME` must match this token |
| Start-up reports a configured webhook | Another service uses this token through a webhook; the program will not delete it for you |
| The target group is not moderated | `ALLOWED_CHAT_IDS` and the bot's administrator rights; `DRY_RUN` only affects automatic actions |
| AI timeouts, 429 or malformed responses | `/status ai` for latency and error types; the probe to confirm the model and API style; adjust `AI_API_STYLE` or `AI_STRUCTURED_OUTPUT` if needed |
| A response is rejected as an unexpected model | The provider returns a different model name than requested; keep `AI_STRICT_RESPONSE=false` unless you need the check |
| `/del all` or `/del members` unavailable | Session path and ownership; `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` configured as a pair |
| Backup fails | The timer's exit status, free space and directory permissions; never delete the main database to clear an error |

Logs: `systemctl status nexa-guardian.service` and `journalctl -u nexa-guardian.service`. Redact tokens, sessions, user messages and identities before sharing logs.
