import pytest


@pytest.fixture
async def seeded(db):
    platform = await db.insert_platform(name="bilibili", base_url="https://www.bilibili.com")
    creator = await db.insert_creator(
        platform_id=platform, remote_id="42", name="UP",
        space_url="https://space.bilibili.com/42",
    )
    return platform, creator


def _v(n: int) -> dict:
    return {"remote_id": f"BV{n}", "title": f"标题{n}", "duration": 60, "pubdate": None,
            "extra": None, "tags": ["tagA"]}


class TestUpsertVideos:
    async def test_insert_new_and_backfill_tags_only(self, db, seeded):
        from platform_video_downloader.core.ingest import upsert_videos
        _, creator = seeded
        await upsert_videos(db, creator, [_v(1), _v(2)], sections=[])
        rows = await db.get_videos_by_creator(creator)
        assert {r["remote_id"] for r in rows} == {"BV1", "BV2"}

        # 再来一次（tags 已有则不覆盖，视频不重复）
        await upsert_videos(db, creator, [_v(1), _v(3)], sections=[])
        rows = await db.get_videos_by_creator(creator)
        assert {r["remote_id"] for r in rows} == {"BV1", "BV2", "BV3"}

    async def test_section_fields_stored(self, db, seeded):
        from platform_video_downloader.core.ingest import upsert_videos
        _, creator = seeded
        sections = [{"remote_id": "BV1", "section_id": "9", "section_name": "合集A"}]
        await upsert_videos(db, creator, [_v(1)], sections=sections)
        video = await db.get_video_by_remote(creator, "BV1")
        assert video["section_name"] == "合集A"


class TestEnqueueDownloads:
    async def test_creates_pending_records(self, db, seeded):
        from platform_video_downloader.core.ingest import upsert_videos, enqueue_downloads
        _, creator = seeded
        await upsert_videos(db, creator, [_v(1), _v(2)])
        rows = await db.get_videos_by_creator(creator)
        n = await enqueue_downloads(
            db, creator, rows, creator_name="UP", save_dir="/tmp/dl",
            name_template="{title}", resolution="720p",
        )
        assert n == 2
        res = await db.get_all_downloads()
        assert res["total"] == 2

    async def test_skip_existing_true_keeps_skipped_unreset(self, db, seeded):
        """skip_existing=True：skipped 状态不被重置 —— CLI 非 --force 语义
        （get_existing_downloads 含 skipped 不含 failed）。"""
        from platform_video_downloader.core.ingest import upsert_videos, enqueue_downloads
        _, creator = seeded
        await upsert_videos(db, creator, [_v(1)])
        rows = await db.get_videos_by_creator(creator)
        await enqueue_downloads(db, creator, rows, creator_name="UP",
                                save_dir="/tmp/dl", name_template="{title}", resolution="720p")
        dl = (await db.get_all_downloads())["items"][0]
        await db.update_download_status(dl["id"], "skipped", "付费")

        n = await enqueue_downloads(db, creator, rows, creator_name="UP",
                                    save_dir="/tmp/dl", name_template="{title}",
                                    resolution="720p", skip_existing=True)
        assert n == 0
        dl2 = (await db.get_all_downloads())["items"][0]
        assert dl2["status"] == "skipped"  # 默认路径此处会重置为 pending

    async def test_default_resets_skipped(self, db, seeded):
        """默认（Web 语义）：skipped 重置 pending。"""
        from platform_video_downloader.core.ingest import upsert_videos, enqueue_downloads
        _, creator = seeded
        await upsert_videos(db, creator, [_v(1)])
        rows = await db.get_videos_by_creator(creator)
        await enqueue_downloads(db, creator, rows, creator_name="UP",
                                save_dir="/tmp/dl", name_template="{title}", resolution="720p")
        dl = (await db.get_all_downloads())["items"][0]
        await db.update_download_status(dl["id"], "skipped", "付费")
        await enqueue_downloads(db, creator, rows, creator_name="UP",
                                save_dir="/tmp/dl", name_template="{title}", resolution="720p")
        dl2 = (await db.get_all_downloads())["items"][0]
        assert dl2["status"] == "pending"

    async def test_default_resets_failed_preserves_completed(self, db, seeded):
        """默认（Web 语义）：failed 重置 pending，completed 保留。"""
        from platform_video_downloader.core.ingest import upsert_videos, enqueue_downloads
        _, creator = seeded
        await upsert_videos(db, creator, [_v(1), _v(2)])
        rows = await db.get_videos_by_creator(creator)
        await enqueue_downloads(db, creator, rows, creator_name="UP",
                                save_dir="/tmp/dl", name_template="{title}", resolution="720p")
        items = (await db.get_all_downloads())["items"]
        by_remote = {i["title"]: i for i in items}
        await db.update_download_status(by_remote["标题1"]["id"], "failed", "x")
        await db.update_download_status(by_remote["标题2"]["id"], "completed")

        n = await enqueue_downloads(db, creator, rows, creator_name="UP",
                                    save_dir="/tmp/dl", name_template="{title}", resolution="720p")
        assert n == 0  # 无新增记录（DB 去重），但 failed 被重置
        items2 = (await db.get_all_downloads())["items"]
        by_remote2 = {i["title"]: i for i in items2}
        assert by_remote2["标题1"]["status"] == "pending"
        assert by_remote2["标题2"]["status"] == "completed"

    async def test_skip_existing_true_still_resets_failed(self, db, seeded):
        """skip_existing=True：failed 不在 get_existing_downloads 集合（仅含
        completed/skipped/downloading/pending），仍会经 insert_download 重置为
        pending —— failed 在两种模式下都会重试，两模式的真实差异是 skipped。"""
        from platform_video_downloader.core.ingest import upsert_videos, enqueue_downloads
        _, creator = seeded
        await upsert_videos(db, creator, [_v(1)])
        rows = await db.get_videos_by_creator(creator)
        await enqueue_downloads(db, creator, rows, creator_name="UP",
                                save_dir="/tmp/dl", name_template="{title}", resolution="720p")
        dl = (await db.get_all_downloads())["items"][0]
        await db.update_download_status(dl["id"], "failed", "x")

        n = await enqueue_downloads(db, creator, rows, creator_name="UP",
                                    save_dir="/tmp/dl", name_template="{title}",
                                    resolution="720p", skip_existing=True)
        assert n == 0  # 记录已存在，不计入新建数
        dl2 = (await db.get_all_downloads())["items"][0]
        assert dl2["status"] == "pending"  # 但 failed 被重置
