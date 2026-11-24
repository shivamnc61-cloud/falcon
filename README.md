# Falcon Premium Bot

Falcon is an asynchronous Discord bot built with discord.py. It supports self-hosted PostgreSQL and can continue using SQLite when `DATABASE_URL` is empty.

## Run locally

1. Copy `.env.example` to `.env`, set `DISCORD_TOKEN`, and replace the example PostgreSQL password.
2. Start the database with `docker compose up -d postgres`.
3. Install dependencies with `python -m pip install -r requirements.txt`, then run `python main.py`.

`DATABASE_URL` selects PostgreSQL (for example, `postgresql://falcon:your-password@localhost:5432/falcon`). Leave it blank to use the SQLite file configured by `DATABASE_PATH`. PostgreSQL tables are initialized by the bot on startup; no manual migration command is required. Existing SQLite data stays in the SQLite database and is not copied automatically to PostgreSQL. When the current SQLite schema is upgraded, pre-isolation member statistics and invite records are retained in `users_legacy` and `invite_tracker_legacy`; new guild-scoped statistics start fresh. Guild-specific configuration, invite records, and member statistics are scoped by Discord guild ID.

The Compose file binds PostgreSQL to localhost and stores its data in the persistent `falcon-postgres-data` volume. Keep the real `.env` file private and use a strong password when exposing PostgreSQL beyond your local machine.

## Run 11 clustered workers with Docker Compose

Set `DISCORD_TOKEN` and the PostgreSQL credentials in `.env`, then run:

```sh
docker compose up --build -d
```

Compose starts PostgreSQL and 11 bot workers, with each worker owning one of the 11 gateway shards. The workers share the Compose PostgreSQL service; the bot coordinates schema setup and gateway identify starts through PostgreSQL advisory locks. Clustered workers skip the per-process `/gateway/bot` limit lookup because their identifies are already serialized through PostgreSQL. If `POSTGRES_PASSWORD` contains URL-reserved characters, set `POSTGRES_PASSWORD_URLENC` to its percent-encoded form as well. To stop the bot workers while keeping the database, run `docker compose stop bot-0 bot-1 bot-2 bot-3 bot-4 bot-5 bot-6 bot-7 bot-8 bot-9 bot-10`.

For a single local process, keep `CLUSTER_COUNT` unset and run `python main.py`; it retains the existing 10-shard default.

## Deploy on Railway

Use one Railway bot service and Railway's managed PostgreSQL. The root [Dockerfile](./Dockerfile) is detected automatically; no local Docker host, Docker Compose file, web port, or deprecated `railway.json` is needed.

1. In Railway, create a project with **New Project → Deploy from GitHub repo**, then select `moonlight90100/falcon-2`.
2. In that project, choose **New → Database → PostgreSQL**.
3. Open the bot service's **Variables** and add:
   - `DISCORD_TOKEN` — your Discord bot token.
   - `DATABASE_URL` — a Railway reference to your PostgreSQL service, for example `${{ Postgres.DATABASE_URL }}`. Replace `Postgres` with the exact database service name.
   - Optionally, `OWNER_IDS` and `SUPPORT_SERVER_URL`.
4. Deploy the bot service. The Dockerfile runs `python main.py`; keep one replica and leave `CLUSTER_COUNT`, `CLUSTER_ID`, and `SHARD_COUNT` unset.

Railway's PostgreSQL reference connects over its private network and persists data across bot redeploys. Don't set `DATABASE_URL` to `localhost` or the Compose hostname `postgres`, and don't import the example `DATABASE_URL` from `.env.example`; those addresses only work in their respective local environments. Scaling a single default bot service to multiple replicas will start duplicate workers, so keep it at one replica unless you configure a coordinated shard cluster.

## Premium server access

Only a Premium account can add Falcon. When Discord exposes the bot-add audit log, Falcon checks the installer and DMs them; if the log is unavailable, it falls back to checking and notifying the server owner. Servers that fail the check receive a Premium Required embed with a support-server button where Falcon has permission, the installer is DMed the same embed, and Falcon leaves. Set `SUPPORT_SERVER_URL` in `.env` so the button works. Discord does not allow the bot to join a human to another server automatically; they must click the button themselves.

Use `/premium gift @user` to transfer all of your active Premium allocations and guild activations. Your Premium access is removed after the transfer; `/premium gift` does not create free Premium.

## Social GIF commands

Use the bot prefix for interactions such as `,hug @user`, `,slap @user`, or `,kiss @user`. Replies to a member's message also work. Add `spam` before the target (for example, `,hug spam @user`) to repeat the action five times in one response. When `GIPHY_API_KEY` is configured, actions include G-rated GIPHY GIFs; without it, the text action still works without a GIF.

## GIPHY commands

Set `GIPHY_API_KEY` to use the direct commands `,search <query>`, `,trending [limit] [offset]`, `,random [tag]`, `,translate <phrase>`, `,get <id[,id...]>`, `,categories`, `,category <category> [limit] [offset]`, or `,category-tag <category> <tag> [limit] [offset]`. Use `,sticker <name>` to search animated stickers (`sticker-search` is an alias), or `,sticker-trending`, `,sticker-random`, and `,sticker-translate`. GIF result lists can be browsed with requester-only buttons. Results are restricted to G-rated content.

## TempVoice

Falcon includes Join-to-Create voice rooms, persistent owner controls, slash commands, staff overrides, multi-generator configuration, and JSON backup/restore.

See [TEMPVOICE_GUIDE.md](./TEMPVOICE_GUIDE.md) for Hindi setup, permissions, supported commands, and local, Railway, and Termux deployment notes.

## Community features

The bot supports server-specific message prefixes with `,setprefix <prefix>` and `,deleteprefix`. Invite summaries distinguish fake accounts and returning members; configure the young-account threshold with `,setaltthreshold <days>`. Timers are persisted across restarts, greetings can auto-delete with `,greet delete-after <join|leave|both> <seconds>`, and `/poll` supports a private response visible only to its creator.
