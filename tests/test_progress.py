from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def ws():
    ws = AsyncMock()
    ws.broadcast = AsyncMock()
    return ws


class TestProgressReporter:
    async def test_noop_without_ws(self):
        from platform_video_downloader.core.progress import make_progress
        p = make_progress(None, 1)
        await p.downloading()          # 不抛错
        await p.completed(200)

    async def test_downloading_payload(self, ws):
        from platform_video_downloader.core.progress import make_progress
        await make_progress(ws, 7).downloading()
        ws.broadcast.assert_awaited_once_with({
            "type": "download_progress", "download_id": 7, "status": "downloading",
        })

    async def test_completed_payload(self, ws):
        from platform_video_downloader.core.progress import make_progress
        await make_progress(ws, 7).completed(file_size=999)
        ws.broadcast.assert_awaited_once_with({
            "type": "download_progress", "download_id": 7,
            "status": "completed", "file_size": 999,
        })

    async def test_merging_failed_removed_payload(self, ws):
        from platform_video_downloader.core.progress import make_progress
        p = make_progress(ws, 7)
        await p.merging()
        await p.failed()
        await p.removed()
        statuses = [c.args[0]["status"] for c in ws.broadcast.await_args_list]
        assert statuses == ["merging", "failed", "removed"]

    async def test_stream_payload_without_status(self, ws):
        """_download_stream 遗留形状：file_size/total_size，无 status 键（前端依赖该形状路由到进度条分支）。"""
        from platform_video_downloader.core.progress import make_progress
        await make_progress(ws, 7)(file_size=4096, total_size=1500)
        ws.broadcast.assert_awaited_once_with({
            "type": "download_progress", "download_id": 7,
            "file_size": 4096, "total_size": 1500,
        })
