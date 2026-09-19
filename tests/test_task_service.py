import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


async def test_submit_creates_task():
    from platform_video_downloader.web.task_service import TaskService
    db = AsyncMock()
    db.insert_platform = AsyncMock(return_value=1)
    db.get_platform_by_name = AsyncMock(return_value={"id": 1})
    db.insert_task = AsyncMock(return_value=42)
    db.get_task_by_url = AsyncMock(return_value=None)
    ws_manager = MagicMock()
    ws_manager.broadcast = AsyncMock()
    ts = TaskService(db=db, ws_manager=ws_manager)
    task_id = await ts.submit_task("https://space.bilibili.com/12345")
    assert task_id == 42
    db.insert_task.assert_called_once()


async def test_submit_returns_existing():
    from platform_video_downloader.web.task_service import TaskService
    db = AsyncMock()
    db.get_task_by_url = AsyncMock(return_value={"id": 99, "status": "pending"})
    ws_manager = MagicMock()
    ws_manager.broadcast = AsyncMock()
    ts = TaskService(db=db, ws_manager=ws_manager)
    task_id = await ts.submit_task("https://space.bilibili.com/12345")
    assert task_id == 99
    db.insert_task.assert_not_called()


async def test_submit_invalid_url():
    from platform_video_downloader.web.task_service import TaskService
    db = AsyncMock()
    db.get_task_by_url = AsyncMock(return_value=None)
    db.get_platform_by_name = AsyncMock(return_value=None)
    db.insert_platform = AsyncMock(return_value=1)
    db.insert_task = AsyncMock(return_value=99)
    db.update_task_status = AsyncMock()
    ws_manager = AsyncMock()
    ws_manager.broadcast = AsyncMock()
    ts = TaskService(db=db, ws_manager=ws_manager)
    task_id = await ts.submit_task("https://example.com/invalid")
    assert task_id == 99
    db.update_task_status.assert_called_once_with(99, "failed", error_message="该平台暂不支持，开发中")


async def test_broadcast_calls_ws_manager():
    from platform_video_downloader.web.task_service import TaskService
    db = AsyncMock()
    ws_manager = AsyncMock()
    ws_manager.broadcast = AsyncMock()
    ts = TaskService(db=db, ws_manager=ws_manager)
    await ts._broadcast({"type": "test"})
    ws_manager.broadcast.assert_called_once_with({"type": "test"})


async def test_broadcast_no_ws_manager():
    from platform_video_downloader.web.task_service import TaskService
    db = AsyncMock()
    ts = TaskService(db=db, ws_manager=None)
    await ts._broadcast({"type": "test"})  # Should not raise


def _video(n: int) -> dict:
    return {"remote_id": f"BV{n}", "title": f"标题{n}", "duration": 60,
            "pubdate": None, "extra": None, "tags": ["tagA"]}


async def test_rescrape_completed_creator_syncs_task_status(db, tmp_path):
    """重抓已全部完成的UP主：enqueue 新建数为 0 时，任务应按实际下载行
    同步为 completed/total_downloads=N，而不是卡在 pending/0
    （否则 start_downloads 无 pending 可跑，任务永远不会结束）。"""
    from platform_video_downloader.web.task_service import TaskService

    pid = await db.insert_platform(name="bilibili", base_url="https://www.bilibili.com")
    cid = await db.insert_creator(
        platform_id=pid, remote_id="42", name="UP",
        space_url="https://space.bilibili.com/42",
    )
    for n in (1, 2):
        vid = await db.insert_video(creator_id=cid, remote_id=f"BV{n}", title=f"标题{n}")
        await db.insert_download(
            video_id=vid, save_path=f"./downloads/标题{n}.mp4", resolution="720p",
        )
    for it in (await db.get_all_downloads())["items"]:
        await db.update_download_status(it["id"], "completed")

    task_id = await db.insert_task(
        platform_id=pid, space_url="https://space.bilibili.com/42", space_uid="42",
    )

    fake_platform = MagicMock()
    fake_platform.name = "bilibili"
    fake_platform.display_name = "哔哩哔哩"
    fake_platform.needs_cookie.return_value = False
    fake_platform.needs_browser.return_value = False
    fake_platform.scrape = AsyncMock(return_value={
        "creator_info": {"name": "UP", "remote_id": "42", "avatar_url": None},
        "videos": [_video(1), _video(2)],
        "sections": [],
    })

    ws_manager = MagicMock()
    ws_manager.broadcast = AsyncMock()
    ts = TaskService(db=db, ws_manager=ws_manager)
    registry = MagicMock()
    registry.get_by_db_platform_id = AsyncMock(return_value=fake_platform)
    ts._registry = registry

    settings = {
        "resolution_priority": ["720p"],
        "name_template": "{title}",
        "output_dir": str(tmp_path / "downloads"),
    }
    with patch("platform_video_downloader.web.task_service.load_settings",
               return_value=settings):
        await ts._execute_task(task_id)

    task = await db.get_task(task_id)
    assert task["status"] == "completed"      # 修复前为 "pending"
    assert task["total_downloads"] == 2       # 修复前为 0
    assert task["downloaded_videos"] == 2
    items = (await db.get_all_downloads())["items"]
    assert all(i["status"] == "completed" for i in items)  # completed 不被重置
