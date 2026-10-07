# Nexa Guardian

English | [简体中文](README.zh-CN.md)

A self-hosted Telegram group moderation bot built with aiogram and optional
Telethon administrator sessions. It reviews suspected advertisements with the
configured DeepSeek Responses endpoint, supports warnings/bans, persistent
keyword raffles, and private administrator tools. Only explicitly allowlisted
numeric groups and the configured administrator may use management features.

**v0.6.8 is a documentation and release-packaging update.** Application behavior
remains that of v0.6.7. On 2026-10-07 a read-only comparison verified that all
24 installed Python package files of production v0.6.7 match baseline `063a8d7`.
Publishing this release does not deploy code, restart services, send messages,
or perform moderation. See [source provenance](PRODUCTION_BASELINE.md).

## Features and boundaries

- Single-pass AI review, `deepseek-flash`, `reasoning.effort=none`, 512 output
  tokens, 0.92 advertising threshold and a 12-second total review budget.
- Bounded preview downloads, shared HTTP connections, concurrent-request
  coalescing, and aggregate learned signals without persisted message bodies.
- `/help`, `/status`, `/del`, `/ban`, `/lottery`; sensitive history/member
  cleanup uses the unique administrator's optional MTProto session and the
  existing confirmation flow.
- Persistent raffle anchors, deletion tasks, and management-log outbox retries.
  Telegram delivery cannot be promised exactly once across a process crash.
- SQLite online backup, non-root systemd/Docker execution and bot identity checks.

`DRY_RUN=true` suppresses **automatic** moderation only. Manual administrator
commands still execute. Starting the bot or probing the external gateway is
not an offline test. AI failure behavior is separately controlled by
`AI_FAILURE_DELETE`; inspect it before enabling a real group.

## Install and verify the release

Use Python 3.10+ (CI covers 3.10 and 3.12). Download the wheel, deployment bundle,
metadata and `SHA256SUMS` from the private GitHub Release; verify all downloaded
assets before installation:

```sh
sha256sum -c SHA256SUMS
python3 -m venv .venv
.venv/bin/python -m pip install ./nexa_guardian-0.6.8-py3-none-any.whl
.venv/bin/python -c 'from importlib.metadata import version; print(version("nexa-guardian"))'
```

Dependency installation uses the package index and the versions pinned in the
wheel metadata. It does not start a service. The release also includes an sdist
and a source/deployment bundle; neither contains credentials, databases, sessions,
or a production configuration. On macOS use `shasum -a 256 -c SHA256SUMS`.

## Configure and deploy

Copy [`.env.example`](.env.example) to a private environment file, fill it only
on the target host, and set permissions to `0600`. Required production settings
include the bot token, expected numeric bot ID and username, exactly one
administrator ID, and explicit numeric group allowlist. AI and MTProto
credentials are separate opt-in capabilities.

Read the paired [English operations guide](docs/OPERATIONS.en.md) /
[中文运维指南](docs/OPERATIONS.zh-CN.md) for the configuration map, deployment,
upgrade, backup/restore, rollback and troubleshooting. Existing guarded
[release script](deploy/release.sh) and [systemd templates](deploy/systemd/)
remain authoritative. Docker is optional via [compose.yaml](compose.yaml).
The detailed historical runbook is retained in [deploy/RELEASE.md](deploy/RELEASE.md).
Do not use deployment or startup as a release-package smoke test.

## Development and offline validation

```sh
uv sync --frozen --extra dev
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright src
uv run --no-sync pytest --cov=src/nexa_guardian --cov-fail-under=75
uv build
python3 scripts/build_release.py
```

Build the final bundle from a committed clean tree. CI additionally audits
locked runtime dependencies and installs the wheel into an isolated environment
for an import/version check, without starting the application. Every release
asset is bound to the immutable Git commit in `release-metadata.json`.

[Contributing / 贡献](CONTRIBUTING.md) · [Security / 安全](SECURITY.md) ·
[MIT license](LICENSE)

This release also pins `multidict==6.9.1` in the lock and wheel requirements
to address [GHSA-54p9-h82j-f925](https://github.com/advisories/GHSA-54p9-h82j-f925).
No business rules change; the production dependency has not been updated.
