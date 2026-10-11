# Nexa Guardian

[简体中文](README.md) | English · [First install](docs/BOOTSTRAP.en.md) · [Operations](docs/OPERATIONS.en.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

A self-hosted Telegram group moderation bot. It uses a model of your choice to recognise advertising and flooding, deletes, warns and bans automatically, and adds raffles and private administrator controls.

- **Bring your own model**: any OpenAI-compatible service works; the endpoint, model and API style are configuration.
- **Bring your own identity**: the bot's name, the community it serves and your own official sites all come from configuration.
- **No message archive**: AI decisions store no message text, images, penalty reasons or model reasoning.
- **Your data**: one SQLite file with consistent backups, running as a non-root user.

## What it does

- Links, forwards, contact details, obfuscated characters and flooding are only *suspicion* signals. Suspicious messages go to the model; ordinary chat is left alone.
- Text, images and video covers are reviewed together. The smallest adequate preview is downloaded under size, concurrency and time limits; if a download fails the review continues with text only.
- Advertising above the confidence threshold is deleted and warned; reaching the warning limit bans automatically.
- Stable phrases from confirmed advertising are learned, so the same advert is later caught without a model call.
- Only suspicious messages show a "reviewing…" notice, updated in place; the bot's replies in the group are removed after about 30 seconds.
- Raffles: keyword entry, pinned card, timed or manual draw, cancel, reroll, and recovery after a restart.
- Administrators can message the bot privately for statistics, pausing AI review, purging group history, and previewing and removing long-inactive members.
- `DRY_RUN` observation mode, a group allowlist, bot identity verification and an optional log chat.

## Before you start

1. Send `/newbot` to [@BotFather](https://t.me/BotFather) and keep the token. The token is full control of the bot; keep it only in the server's configuration file.
2. Add the bot to your group as an administrator with just the rights it needs: **delete messages**, **ban users**, **pin messages**.
3. Find two numeric IDs: the group's (negative, like `-1001234567890`) and your own user ID.
4. For AI review, have an OpenAI-compatible model service ready: endpoint, API key and model ID. The bot also runs without AI, filtering by keywords, links and similar rules only.

## Run with Docker

Requires Docker Engine and the Compose plugin.

```bash
git clone https://github.com/Tumblr-code/NexaAssistantRobot.git
cd NexaAssistantRobot
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
BOT_TOKEN=the token from BotFather
ALLOWED_CHAT_IDS=-1001234567890
AUTHORIZED_ADMIN_IDS=your numeric ID
```

Start it and follow the logs:

```bash
docker compose up -d --build
docker compose logs -f guardian
```

Send `/status` in the group to confirm it is working. After changing `.env`, run `docker compose up -d --force-recreate guardian`. Data lives in the named volume `guardian-data`; do not delete it when upgrading.

Consider starting with `DRY_RUN=true`: the bot records what it would act on without deleting, warning or banning (manual administrator commands still execute).

To install directly on a host with systemd, follow the [first-install guide](docs/BOOTSTRAP.en.md).

## Connect your model

In `.env`:

```dotenv
AI_ENABLED=true
OPENAI_BASE_URL=https://your-provider.example
OPENAI_API_KEY=your key
AI_MODEL=model-id
```

Most services need only two more decisions:

| Variable | Values | How to choose |
|---|---|---|
| `AI_API_STYLE` | `responses` (default), `chat_completions` | Keep the default if the provider implements the OpenAI Responses API; choose the latter if it only has `/chat/completions` |
| `AI_STRUCTURED_OUTPUT` | `json_schema` (default), `json_object` | Switch to `json_object` when the model does not support strict JSON Schema output |

Other knobs:

| Variable | Default | Meaning |
|---|---|---|
| `AI_REASONING_EFFORT` | `none` | `none`, `minimal`, `low`, `medium` or `high`; leave empty to omit the parameter |
| `AI_SERVICE_TIER` | `default` | Service tier parameter; leave empty to omit it |
| `AI_STRICT_RESPONSE` | `false` | When `true`, the service must echo the model, tier and reasoning effort exactly, or the review fails |
| `AI_MODEL_LABEL` | empty | Model name shown to members; empty shows "AI" |

The endpoint may be a bare host or end in `/v1`; the path is completed for you. It must be `https://`, except a local service such as Ollama, which may use `http://localhost`.

**Probe before going live.** This sends one fixed harmless sentence and a 1×1 test image, never any group message, to confirm the model supports the chosen API style, structured output and image input:

```bash
uv run nexa-guardian-probe --env-file .env
```

The probe makes a real model call and may use a little quota. If it fails, leave `AI_ENABLED` off: try another model, or adjust `AI_API_STYLE` and `AI_STRUCTURED_OUTPUT`, then probe again.

What the model needs: it must return JSON only when told to, and accept image input (the probe checks both). A fast, inexpensive small model is recommended; the total budget per message defaults to 12 seconds.

## Set the identity and official sites

All optional. Left empty, the bot is a neutral "Guardian".

```dotenv
BRAND_NAME=Example Guardian
COMMUNITY_NAME=Example
OFFICIAL_SITES=Example Store|https://store.example.com,Example Help|https://help.example.com
AI_MODEL_LABEL=GPT
```

- `BRAND_NAME` and `COMMUNITY_NAME` go into the review prompt, `/help` and the bot profile.
- Hosts in `OFFICIAL_SITES` are treated as yours: an exact-match link is not external advertising by itself, while a lookalike such as `store.example.com.evil.example` is still reviewed. To allow domains without naming them in the prompt, use `ALLOWED_DOMAINS`, which also allows subdomains.
- After start-up the bot syncs its name, short description and description to the generated text. To maintain the profile yourself in BotFather, set `SYNC_BOT_PROFILE=false`.
- The warning limit is `WARN_LIMIT` (default 3); the wording follows it. Profile and help texts shown to members are in Chinese.

## Commands

The menu has five entries: `/help`, `/status`, `/del`, `/ban`, `/lottery`.

```text
/status                       status and moderation statistics
/status ai                    AI review details (private chat)
/status ai pause|resume       pause or resume AI review (private chat)
/status ai words              list learned AI signatures (private chat)
/status ai del <id>           disable a wrongly learned signature (private chat)
/del                          delete the replied-to message; in private chat give a message ID
/del all                      purge the whole group history
/del members                  preview long-inactive members (private chat)
/del members <code> confirm   remove the previewed members
/ban                          ban the author of the replied-to message; in private chat give a user ID
/lottery 30m 1 <keyword> <prize>   create a keyword raffle
/lottery end|cancel|reroll    draw, cancel or reroll
```

- Management commands accept only users in `AUTHORIZED_ADMIN_IDS`, and their actual Telegram rights are checked: an administrator without the ban right cannot ban through the bot. Private-chat management requires exactly one listed user.
- With "anonymous administrator" enabled Telegram hides the real user ID; turn it off before running a command.
- Raffles last from 1 minute to 30 days with 1–20 winners; the keyword is 1–32 characters without spaces. One active raffle per group.
- `/del all` and `/del members` need the administrator's own Telegram user session (MTProto): set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` and log in once on the server, as described in the [first-install guide](docs/BOOTSTRAP.en.md). Without it both commands refuse to run.
- `/del members` shows a preview and a confirmation code before removing anyone; an active raffle blocks `/del all`.

## Common settings

[.env.example](.env.example) has the complete annotated list.

| Variable | Default | Purpose |
|---|---:|---|
| `ALLOWED_CHAT_IDS` | empty | Numeric IDs of the groups to manage; empty manages none |
| `AUTHORIZED_ADMIN_IDS` | empty | Numeric IDs of users allowed to run management commands |
| `EXPECTED_BOT_ID` / `EXPECTED_BOT_USERNAME` | empty | Verify the bot's identity at start-up so a wrong token is caught |
| `DRY_RUN` | `false` | Record automatic matches without acting |
| `WARN_LIMIT` | `3` | Warnings before an automatic ban |
| `AI_AD_THRESHOLD` | `0.92` | Minimum advertising confidence to delete and warn |
| `AI_LEARN_THRESHOLD` | `0.97` | Minimum confidence to enable a learned signature |
| `AI_FAILURE_DELETE` | `true` | When the model call fails, delete the suspicious message without warning or learning |
| `AI_TIMEOUT_SECONDS` | `12` | Total review budget per message, including queueing and one retry |
| `NOTICE_TTL_SECONDS` | `30` | How long the bot's group replies stay |
| `BLOCK_EXTERNAL_LINKS` | `true` | External links as a suspicion signal; filtered directly when AI is off |
| `ALLOWED_DOMAINS` | empty | Allowed domains and their subdomains |
| `LOG_CHAT_ID` | empty | Optional group or channel that receives moderation logs |
| `DATABASE_PATH` | `data/guardian.db` | Location of the SQLite database |

The bot uses long polling. If the token already has a webhook configured, the program refuses to start and **does not delete the webhook for you**, so another service using it is not cut off.

## Privacy

- The model receives only the message text under review, the hosts of its links, the names of local risk signals and, when present, one image preview. The user identifier is irreversibly hashed before it is sent as the safety identifier.
- Stored AI decisions contain no message text, images, penalty reasons or model reasoning. What is learned is stable advertising phrases only, never URLs, handles or phone numbers.
- Decision records are kept for 30 days by default (`AI_DECISION_RETENTION_DAYS`).

## Development

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pyright src
uv run pytest --cov=src/nexa_guardian --cov-fail-under=75
uv build
python scripts/build_release.py
```

Tests use mocks and temporary data only; they never contact Telegram or a model. The last two commands write the wheel, the deployment bundle and checksums to `dist/`. To run locally: `uv run nexa-guardian`.

## Documentation

- [First install (systemd)](docs/BOOTSTRAP.en.md)
- [Operations: upgrade, rollback, backup, troubleshooting](docs/OPERATIONS.en.md)
- [The release.sh upgrade tool](deploy/RELEASE.md) (Chinese)

## License

MIT, see [LICENSE](LICENSE). Built with [aiogram](https://github.com/aiogram/aiogram) 3 and Telethon.
