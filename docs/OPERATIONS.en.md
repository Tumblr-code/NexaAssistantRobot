# Operations guide

English | [简体中文](OPERATIONS.zh-CN.md)

## Scope and configuration

v0.6.8 changes documentation, version metadata, packaging and the audited
multidict dependency; database
schema stays **8**. The verified production package is still v0.6.7. Release
publication is separate from deployment approval. Never put `.env`, session
files, SQLite/WAL files, backups or real identifiers in a release or CI log.

Use `.env.example` as the complete variable inventory. Its values are examples,
not a deployable production configuration. Key groups are:

| Settings | Required operator decision |
| --- | --- |
| `BOT_TOKEN`, `EXPECTED_BOT_ID`, `EXPECTED_BOT_USERNAME` | Exact bot identity; mismatch prevents polling |
| `ALLOWED_CHAT_IDS`, `AUTHORIZED_ADMIN_IDS` | Numeric groups and exactly one private administrator |
| `DATABASE_PATH`, `LOG_LEVEL`, `LOG_CHAT_ID` | Persistent database, safe log level, optional log destination |
| `DRY_RUN`, `AI_FAILURE_DELETE`, `WARN_LIMIT` | Automatic observation/failure/moderation policy; manual commands still execute |
| `AI_ENABLED`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `AI_MODEL` | Optional official DeepSeek integration; credentials stay in a 0600 file |
| `AI_REASONING_EFFORT`, normal/suspicious variants | `none`; no automatic model fallback |
| `AI_TIMEOUT_SECONDS`, `UPDATE_CONCURRENCY_LIMIT` | Production contract: 12 seconds / 32 updates |
| `AI_IMAGE_MAX_BYTES`, target edge, timeout, concurrency | Production: 1572864 / 1280 / 2 / 4 |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH` | Optional authenticated administrator session; never copy into source |

The remaining threshold/cache/flood settings retain their checked-in defaults.
Production release probes also validate identity and the full existing AI
contract; do not weaken them to make a release pass. Gateway probes contact an
external API and may consume quota; keep them out of offline package tests.

## Installation and deployment

Verify `SHA256SUMS` and the commit in `release-metadata.json`. Install the wheel
in a separate Python 3.10+ virtual environment and verify its distribution
version without importing `main` or starting polling. Dependencies are pinned
in `pyproject.toml` and `uv.lock`; CI uses `uv sync --frozen --extra dev`.

For a separately authorized production deployment, use the unchanged guarded
`deploy/release.sh --wheel /absolute/path/to/wheel --git-sha FULL_SHA
--schema-version 8`. Its probes, protected configuration backup, final stopped
writer backup, atomic current switch and health checks are required. This
command is a real deployment, not a dry run. Do not manually repoint `current`.
Review its optional model-migration flags only when that migration is intended.

| Resource | Canonical location |
| --- | --- |
| Releases/current | `/opt/projects/nexa-guardian/runtime/` |
| Compatibility link | `/opt/nexa-guardian/current` |
| Environment | `/etc/nexa-guardian/guardian.env`, root-owned 0600 |
| Database | `/var/lib/nexa-guardian/guardian.db` |
| MTProto session | `/var/lib/nexa-guardian/mtproto/admin.session` |
| Backups | `/var/backups/nexa-guardian/`; manual copies in `manual/` |

The `deploy/systemd/` templates run as `nexa-guardian` with filesystem
protection. Existing paths/configuration/data remain external to releases.
Optional Docker uses `compose.yaml`, a persistent `guardian-data` volume and a
non-root image; never remove that volume during an application upgrade.

## Upgrade, rollback and recovery

Before changing a running installation, record current package/source identity,
service state, database schema, protected configuration and session permissions.
Keep a tested old wheel and independent backups. Follow `deploy/RELEASE.md`
and the guarded script, then read back the installed package version, source
identity, service state and database integrity. A GitHub check or successful
process start alone is not production acceptance.

The script owns rollback during its transaction and refuses unsafe schema
reversal. For post-release problems first stop new moderation work through an
approved service stop, preserve current state and determine whether any new
writes occurred. Do not overwrite a newer database with an old snapshot just
to run old code. Prefer a forward code fix; any separately approved restore
requires a compatible schema and evidence that retained state is reconciled.

Create an online consistent backup using the installed interpreter:

```sh
/opt/nexa-guardian/current/.venv/bin/python -m nexa_guardian.backup /protected/backup-directory
```

The helper uses SQLite Online Backup and verifies `quick_check` and
`foreign_key_check`. The existing backup timer runs at 03:30 Asia/Shanghai and
retains standard automatic backups for 30 days; `manual/` is excluded. Preserve
the private MTProto session and environment separately; a database backup does
not replace them. Do not copy a live database file without its transaction state.

Rehearse recovery in an isolated directory: check the backup hash and integrity,
open it using the matching schema version, and check expected aggregate counts.
Never start a bot against a production session during rehearsal. Before an
approved actual restore, stop writers, archive the current database and WAL/SHM,
restore the consistent copy with the original ownership/permissions, validate it,
then explicitly authorize startup. Do not replay manual commands or notifications.

## Troubleshooting

| Symptom | Safe checks and boundary |
| --- | --- |
| Startup identity rejection | Compare configured expected ID/username privately; do not print the token |
| No group moderation | Confirm numeric allowlist and bot admin permissions; `DRY_RUN` is automatic-only |
| AI timeout/429/format rejection | Inspect safe status/latency/code logs and configured contract; do not silently change model or thresholds |
| MTProto command unavailable | Check session path/owner and paired API settings; interactive login requires separate authorization |
| Backup failure | Check timer exit status, disk/permissions and backup integrity; never delete the live database to clear errors |
| Repeated outbox attempts | Inspect aggregate pending/failed counts; do not resend real messages as a test |

Use read-only `systemctl status nexa-guardian.service` and scoped journal reads;
redact tokens, sessions, raw user messages and identities before sharing evidence.
The detailed Chinese command/config reference remains in `README.zh-CN.md`.

This release also pins `multidict==6.9.1` in the lock and wheel requirements
to address [GHSA-54p9-h82j-f925](https://github.com/advisories/GHSA-54p9-h82j-f925).
No business rules change; the production dependency has not been updated.
