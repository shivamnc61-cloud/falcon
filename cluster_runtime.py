from contextlib import asynccontextmanager


def cluster_shard_ids(cluster_id: int, cluster_count: int, shard_count: int) -> tuple[int, ...]:
    if cluster_count < 1:
        raise ValueError("CLUSTER_COUNT must be at least 1.")
    if not 0 <= cluster_id < cluster_count:
        raise ValueError("CLUSTER_ID must be between 0 and CLUSTER_COUNT - 1.")
    if shard_count < cluster_count:
        raise ValueError("SHARD_COUNT must be at least CLUSTER_COUNT.")
    return tuple(range(cluster_id, shard_count, cluster_count))


def bot_owns_guild(bot, guild_id: int) -> bool:
    shard_ids = getattr(bot, "shard_ids", None)
    shard_count = getattr(bot, "shard_count", None)
    if shard_ids is None or not shard_count:
        return True
    guild_shard_id = (int(guild_id) >> 22) % int(shard_count)
    return guild_shard_id in shard_ids


@asynccontextmanager
async def shared_advisory_lock(bot, lock_id: int):
    if getattr(bot, "cluster_count", 1) <= 1:
        yield
        return

    async with bot.db.execute("SELECT pg_advisory_lock(?)", (lock_id,)) as cursor:
        await cursor.fetchone()
    try:
        yield
    finally:
        async with bot.db.execute(
            "SELECT pg_advisory_unlock(?)", (lock_id,)
        ) as cursor:
            await cursor.fetchone()
