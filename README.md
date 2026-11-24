# Falcon Premium Bot

Falcon is a Discord bot built with `discord.py`. It includes moderation, invite and activity tracking, temporary voice rooms, premium features, and community tools.

## Quick start: Docker Compose

Docker Compose is the recommended setup. It runs PostgreSQL and 11 bot workers, with each worker handling one gateway shard.

### 1. Get the code and configure the bot

```bash
git clone https://github.com/shivamnc61-cloud/falcon.git
cd falcon
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
DISCORD_TOKEN=your-discord-bot-token
POSTGRES_PASSWORD=use-a-strong-password
```

Keep `.env` private. Do not commit or share your Discord token, database password, or API keys. If the PostgreSQL password contains URL-reserved characters, set `POSTGRES_PASSWORD_URLENC` to its percent-encoded form too.

### 2. Enable Discord intents

In the Discord Developer Portal, enable **Server Members Intent** and **Message Content Intent** for the application. Give the bot only the server permissions needed for the features you plan to use. Common permissions include View Channels, Send Messages, Embed Links, Read Message History, Manage Channels, Manage Roles, and Move Members. Moderation features may also require Kick Members, Ban Members, or Moderate Members.

### 3. Build and start

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot-0
```

The image uses the Tsinghua PyPI mirror by default. To use another compatible package index, set `PIP_INDEX_URL` in `.env` or the shell before building.

Useful lifecycle commands:

```bash
docker compose stop       # Stop services without removing containers or data
docker compose start      # Start them again
docker compose down       # Remove containers and network; keep the database volume
```

PostgreSQL data is stored in the `falcon-postgres-data` volume. Do not use `docker compose down -v` unless you intend to delete that database data.

## Run locally

Use Python 3.11 for local development; the Docker image uses Python 3.14. Copy `.env.example` to `.env`, set `DISCORD_TOKEN`, and choose a database:

- **SQLite:** set `DATABASE_URL=` (empty). Falcon uses `DATABASE_PATH`, which defaults to `data/falcon_data.db`.
- **PostgreSQL:** start the database with `docker compose up -d postgres`, then set `DATABASE_URL` to a PostgreSQL URL pointing at `localhost` with the same credentials as `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` in `.env`. URL-encode reserved characters in the URL password.

Install dependencies and run the bot:

```bash
python -m pip install -r requirements.txt
python main.py
```

For a single local process, leave `CLUSTER_COUNT`, `CLUSTER_ID`, and `SHARD_COUNT` unset. Falcon defaults to 10 shards. Do not run a local process at the same time as the Compose cluster with the same bot token.

PostgreSQL tables are initialized automatically at startup. SQLite data is not copied to PostgreSQL automatically; choose one database before you start using the bot.

## Configuration

The main settings are in `.env`; non-secret temporary voice defaults are in [config.yml](./config.yml).

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Required Discord bot token |
| `PREFIX` | Prefix for prefix commands; defaults to `,` |
| `DATABASE_URL` | PostgreSQL URL; leave empty to use SQLite |
| `DATABASE_PATH` | SQLite file path; defaults to `data/falcon_data.db` |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | PostgreSQL credentials used by Compose |
| `POSTGRES_PASSWORD_URLENC` | Optional URL-encoded password for Compose when the password contains reserved characters |
| `OWNER_IDS`, `PRIMARY_OWNER_ID` | Bot owner IDs |
| `SUPPORT_SERVER_URL` | Support-server link used by premium messages |
| `GIPHY_API_KEY` | Optional key for GIF and sticker commands |
| `AUTO_UPLOAD_EMOJIS` | Set to `true` to enable application emoji synchronization |
| `PIP_INDEX_URL` | Optional Python package index override for Docker builds |

Compose sets the cluster variables and database URL for its 11 workers. Do not set `CLUSTER_COUNT`, `CLUSTER_ID`, or `SHARD_COUNT` for a normal single local process.

## Command examples

The default prefix is a comma. Replace it with your configured prefix if you changed `PREFIX`.

```text
,help
,invites [@member]
,messages [@member]
,vc [@member]
,setprefix !
,deleteprefix
,voicehub setup
,room info
,rank [@member]
,hug @member
,search <query>
/poll
/premium gift @user
/tempvoice setup
```

For a detailed command reference, see [COMPLETE_COMMANDS_LIST.md](./COMPLETE_COMMANDS_LIST.md). That reference uses `!` as a prefix placeholder in some sections; use the prefix configured for your bot.

### Temporary voice rooms

Falcon supports join-to-create voice rooms, persistent owner controls, staff overrides, and backup/restore. See [TEMPVOICE_GUIDE.md](./TEMPVOICE_GUIDE.md) for setup steps, permissions, and the complete room command guide.

### GIF and sticker commands

Set `GIPHY_API_KEY` to enable GIPHY features. Examples include `,search <query>`, `,trending`, `,random [tag]`, `,sticker <name>`, and `,sticker-trending`. Results are restricted to G-rated content.

### Premium access

Only servers with active Premium can use Premium-gated features. `/premium gift @user` transfers your active Premium allocations and guild activations; it does not create free Premium.

## Railway deployment

Use one bot service and Railway's managed PostgreSQL:

1. Deploy this repository from GitHub; Railway uses the root [Dockerfile](./Dockerfile).
2. Add a PostgreSQL service to the project.
3. Set `DISCORD_TOKEN` and `DATABASE_URL` in the bot service. Use Railway's reference to the PostgreSQL service URL, not `localhost` or the Compose hostname `postgres`.
4. Keep one bot-service replica and leave the cluster variables unset.

Railway's PostgreSQL service persists data across deployments. Scaling a single bot service to multiple replicas starts duplicate workers unless a coordinated shard cluster is configured.

## Troubleshooting

- **Docker dependency install times out:** the Docker build uses the Tsinghua PyPI mirror by default. Set `PIP_INDEX_URL` to another reachable Python package index and rebuild with `docker compose up -d --build`.
- **Invalid Discord token:** create or reset the token in the Developer Portal, update `DISCORD_TOKEN`, then recreate the bot workers:

  ```bash
  docker compose up -d --force-recreate bot-0 bot-1 bot-2 bot-3 bot-4 bot-5 bot-6 bot-7 bot-8 bot-9 bot-10
  ```
- **Prefix commands do not respond:** check the configured prefix, enable Message Content Intent, and make sure the bot can read and reply in the channel.
- **PostgreSQL connection fails:** Compose workers use the `postgres` service name automatically. A local `python main.py` process should connect to PostgreSQL through `localhost`.

## Tests

Run the test suite and compile the Python files with:

```bash
python -m pytest -q
python -m compileall -q .
```
