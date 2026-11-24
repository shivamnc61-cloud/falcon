import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cluster_runtime import bot_owns_guild, cluster_shard_ids, shared_advisory_lock


def test_eleven_clusters_cover_each_shard_once() -> None:
    shard_ids = [
        cluster_shard_ids(cluster_id, cluster_count=11, shard_count=11)
        for cluster_id in range(11)
    ]

    assert shard_ids == [(shard_id,) for shard_id in range(11)]
    assert sorted(shard_id for cluster in shard_ids for shard_id in cluster) == list(
        range(11)
    )


def test_clusters_partition_guilds_by_discord_shard() -> None:
    shard_count = 11
    cluster_count = 11
    guild_ids = [(shard_id << 22) for shard_id in range(shard_count)]

    for cluster_id, shard_ids in enumerate(
        cluster_shard_ids(i, cluster_count, shard_count)
        for i in range(cluster_count)
    ):
        bot = SimpleNamespace(shard_ids=shard_ids, shard_count=shard_count)
        assert [bot_owns_guild(bot, guild_id) for guild_id in guild_ids] == [
            shard_id == cluster_id for shard_id in range(shard_count)
        ]


def test_cluster_shard_assignment_rejects_empty_workers() -> None:
    with pytest.raises(ValueError, match="SHARD_COUNT must be at least CLUSTER_COUNT"):
        cluster_shard_ids(cluster_id=10, cluster_count=11, shard_count=10)


def test_falcon_bot_uses_only_its_cluster_shards(monkeypatch) -> None:
    import main as bot_module

    monkeypatch.setattr(bot_module, "CLUSTER_COUNT", 11)
    monkeypatch.setattr(bot_module, "CLUSTER_ID", 7)
    monkeypatch.setattr(bot_module, "SHARD_COUNT", 11)
    monkeypatch.setattr(bot_module, "CLUSTER_SHARD_IDS", (7,))

    bot = bot_module.FalconBot()

    assert bot.shard_count == 11
    assert bot.shard_ids == (7,)
    assert bot.owns_guild((7 << 22) + 1)
    assert not bot.owns_guild((6 << 22) + 1)


def test_cluster_startup_rejects_non_shared_database(monkeypatch) -> None:
    import main as bot_module

    bot = bot_module.FalconBot()
    bot.cluster_count = 2
    monkeypatch.setattr(bot_module, "DATABASE_URL", "")

    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        asyncio.run(bot.setup_hook())


def test_clustered_startup_skips_gateway_limits_request() -> None:
    import main as bot_module

    bot = bot_module.FalconBot()
    bot.cluster_count = 11
    bot.shard_count = 11
    bot.fetch_session_start_limits = AsyncMock()

    asyncio.run(bot._configure_identify_concurrency())

    bot.fetch_session_start_limits.assert_not_awaited()
    assert bot.identify_max_concurrency == 1


def test_single_process_startup_reads_gateway_identify_limit() -> None:
    import main as bot_module

    bot = bot_module.FalconBot()
    bot.cluster_count = 1
    bot.shard_count = 10
    bot.fetch_session_start_limits = AsyncMock(
        return_value=SimpleNamespace(max_concurrency=8)
    )

    asyncio.run(bot._configure_identify_concurrency())

    bot.fetch_session_start_limits.assert_awaited_once()
    assert bot.identify_max_concurrency == 8


def test_shared_advisory_lock_releases_after_cluster_operation() -> None:
    class LockCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetchone(self):
            return (True,)

    class LockDatabase:
        def __init__(self):
            self.queries = []

        def execute(self, query, parameters):
            self.queries.append((query, parameters))
            return LockCursor()

    async def verify():
        db = LockDatabase()
        bot = SimpleNamespace(cluster_count=11, db=db)
        async with shared_advisory_lock(bot, 123):
            assert len(db.queries) == 1
        assert db.queries == [
            ("SELECT pg_advisory_lock(?)", (123,)),
            ("SELECT pg_advisory_unlock(?)", (123,)),
        ]

    asyncio.run(verify())
