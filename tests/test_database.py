import pytest
from datetime import datetime


class TestDatabase:
    async def test_init_creates_tables(self, db):
        async with db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert {"platform", "creator", "video", "download"}.issubset(tables)

    async def test_insert_and_get_platform(self, db):
        pid = await db.insert_platform(
            name="bilibili", base_url="https://www.bilibili.com"
        )
        assert pid == 1
        platform = await db.get_platform(pid)
        assert platform["name"] == "bilibili"

    async def test_insert_duplicate_platform_raises(self, db):
        await db.insert_platform(name="bilibili")
        with pytest.raises(Exception):
            await db.insert_platform(name="bilibili")

    async def test_insert_and_get_creator(self, db):
        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid,
            remote_id="33676449",
            name="TestUP",
            space_url="https://space.bilibili.com/33676449",
        )
        creator = await db.get_creator(cid)
        assert creator["name"] == "TestUP"
        assert creator["remote_id"] == "33676449"

    async def test_insert_and_get_video(self, db):
        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid,
            remote_id="123",
            name="UP",
            space_url="https://x.com",
        )
        vid = await db.insert_video(
            creator_id=cid,
            remote_id="BV1xx",
            title="Test Video",
            duration=120,
            extra='{"cid": 456}',
            section_id="s1",
            section_name="合集A",
        )
        video = await db.get_video(vid)
        assert video["title"] == "Test Video"
        assert video["section_name"] == "合集A"

    async def test_insert_download_and_get(self, db):
        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid,
            remote_id="123",
            name="UP",
            space_url="https://x.com",
        )
        vid = await db.insert_video(
            creator_id=cid,
            remote_id="BV1xx",
            title="V",
            duration=60,
        )
        did = await db.insert_download(
            video_id=vid, save_path="/tmp/v.mp4", resolution="720p"
        )
        dl = await db.get_download(did)
        assert dl["status"] == "pending"
        assert dl["resolution"] == "720p"

    async def test_update_download_status(self, db):
        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid,
            remote_id="123",
            name="UP",
            space_url="https://x.com",
        )
        vid = await db.insert_video(
            creator_id=cid,
            remote_id="BV1xx",
            title="V",
            duration=60,
        )
        did = await db.insert_download(
            video_id=vid, save_path="/tmp/v.mp4", resolution="720p"
        )
        await db.update_download_status(did, "downloading")
        dl = await db.get_download(did)
        assert dl["status"] == "downloading"

    async def test_get_existing_downloads(self, db):
        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid,
            remote_id="123",
            name="UP",
            space_url="https://x.com",
        )
        vid1 = await db.insert_video(
            creator_id=cid,
            remote_id="BV1a",
            title="VA",
            duration=60,
        )
        vid2 = await db.insert_video(
            creator_id=cid,
            remote_id="BV1b",
            title="VB",
            duration=60,
        )
        await db.insert_download(
            video_id=vid1, save_path="/a.mp4", resolution="720p"
        )
        await db.insert_download(
            video_id=vid2, save_path="/b.mp4", resolution="720p"
        )
        d3 = await db.insert_download(
            video_id=vid2, save_path="/b2.mp4", resolution="480p"
        )
        await db.update_download_status(d3, "completed")
        existing = await db.get_existing_downloads(cid)
        assert ("BV1a", "720p") in existing
        assert ("BV1b", "720p") in existing
        assert ("BV1b", "480p") in existing
        assert len(existing) == 3

    async def test_get_stats(self, db):
        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid,
            remote_id="123",
            name="UP",
            space_url="https://x.com",
        )
        vid = await db.insert_video(
            creator_id=cid,
            remote_id="BV1xx",
            title="V",
            duration=60,
        )
        d1 = await db.insert_download(
            video_id=vid, save_path="/a.mp4", resolution="720p"
        )
        d2 = await db.insert_download(
            video_id=vid, save_path="/b.mp4", resolution="480p"
        )
        await db.update_download_status(d1, "completed")
        await db.update_download_status(d2, "skipped")
        stats = await db.get_stats()
        assert stats["total_creators"] == 1
        assert stats["total_videos"] == 1
        assert stats["completed"] == 1
        assert stats["skipped"] == 1

    async def test_task_table_exists(self, db):
        cur = await db._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task'")
        row = await cur.fetchone()
        assert row is not None

    async def test_insert_and_get_task(self, db):
        pid = await db.insert_platform("bilibili")
        task_id = await db.insert_task(platform_id=pid, space_url="https://space.bilibili.com/12345", space_uid="12345")
        task = await db.get_task(task_id)
        assert task["space_url"] == "https://space.bilibili.com/12345"
        assert task["status"] == "pending"

    async def test_update_task_status(self, db):
        pid = await db.insert_platform("bilibili")
        task_id = await db.insert_task(platform_id=pid, space_url="https://space.bilibili.com/1", space_uid="1")
        await db.update_task_status(task_id, "scraping", total_videos=41, scraped_videos=10)
        task = await db.get_task(task_id)
        assert task["status"] == "scraping"
        assert task["total_videos"] == 41

    async def test_get_all_tasks(self, db):
        pid = await db.insert_platform("bilibili")
        await db.insert_task(platform_id=pid, space_url="https://space.bilibili.com/1", space_uid="1")
        await db.insert_task(platform_id=pid, space_url="https://space.bilibili.com/2", space_uid="2")
        result = await db.get_all_tasks()
        assert len(result["items"]) == 2
        assert "total" in result
        assert "page" in result

    async def test_download_table_has_audio_fields(self, db):
        pid = await db.insert_platform("bilibili")
        cid = await db.insert_creator(platform_id=pid, remote_id="123", name="Test", space_url="https://space.bilibili.com/123")
        vid = await db.insert_video(creator_id=cid, remote_id="BV1xx", title="Test Video")
        dl_id = await db.insert_download(video_id=vid, save_path="/tmp/test.mp4", resolution="720p")
        cur = await db._conn.execute("SELECT audio_url, merge_status FROM download WHERE id=?", (dl_id,))
        row = await cur.fetchone()
        assert row is not None

    async def test_get_all_downloads_paginated(self, db):
        pid = await db.insert_platform("bilibili")
        cid = await db.insert_creator(platform_id=pid, remote_id="123", name="Test", space_url="https://space.bilibili.com/123")
        for i in range(5):
            vid = await db.insert_video(creator_id=cid, remote_id=f"BV{i}", title=f"Video {i}")
            await db.insert_download(video_id=vid, save_path=f"/tmp/test{i}.mp4", resolution="720p")
        result = await db.get_all_downloads(page=1, page_size=2)
        assert len(result["items"]) == 2
        assert result["total"] == 5
        assert result["total_pages"] == 3

    async def test_update_download_total_size(self, db):
        platform = await db.insert_platform(name="bilibili")
        creator = await db.insert_creator(platform_id=platform, remote_id="1", name="up", space_url="https://space.bilibili.com/1")
        video = await db.insert_video(creator_id=creator, remote_id="BV1", title="t")
        dl = await db.insert_download(video_id=video, save_path="x.mp4", resolution="720p")
        await db.update_download_total_size(dl, 12345)
        stats = await db.get_stats()
        assert stats["total_downloads"] == 1

    async def test_update_download_save_path(self, db):
        platform = await db.insert_platform(name="bilibili")
        creator = await db.insert_creator(platform_id=platform, remote_id="1", name="up", space_url="https://space.bilibili.com/1")
        video = await db.insert_video(creator_id=creator, remote_id="BV1", title="t")
        dl = await db.insert_download(video_id=video, save_path="old.mp4", resolution="720p")
        await db.update_download_save_path(dl, "new.mp4")
        res = await db.get_all_downloads()
        assert any(r["save_path"] == "new.mp4" for r in res["items"])

    async def test_get_creator_name_by_video(self, db):
        platform = await db.insert_platform(name="bilibili")
        creator = await db.insert_creator(platform_id=platform, remote_id="1", name="UP名字", space_url="https://space.bilibili.com/1")
        video = await db.insert_video(creator_id=creator, remote_id="BV1", title="t")
        assert await db.get_creator_name_by_video(video) == "UP名字"
        assert await db.get_creator_name_by_video(99999) is None

    async def test_count_pending_downloads_by_creator(self, db):
        platform = await db.insert_platform(name="bilibili")
        creator = await db.insert_creator(platform_id=platform, remote_id="1", name="up", space_url="https://space.bilibili.com/1")
        video = await db.insert_video(creator_id=creator, remote_id="BV1", title="t")
        await db.insert_download(video_id=video, save_path="x.mp4", resolution="720p")
        assert await db.count_pending_downloads_by_creator(creator) == 1
        assert await db.count_pending_downloads_by_creator(99999) == 0
