import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_async_iter(chunks):
    """Create an async iterator from a list of byte chunks."""
    async def _aiter():
        for chunk in chunks:
            yield chunk
    return _aiter()


def _make_mock_response(status=200, headers=None, chunks=None):
    """Create a mock aiohttp response.

    Uses MagicMock (not AsyncMock) so that .headers returns a real dict
    and .raise_for_status is a plain callable. Only .content.iter_chunked
    needs to return an async iterator.
    """
    resp = MagicMock()
    resp.status = status
    resp.headers = headers if headers is not None else {}
    resp.raise_for_status = MagicMock()
    if chunks is None:
        chunks = []
    resp.content.iter_chunked = MagicMock(return_value=_make_async_iter(chunks))
    return resp


class _MockCtx:
    """Async context manager that returns a fixed response object."""
    def __init__(self, resp):
        self._resp = resp
    async def __aenter__(self):
        return self._resp
    async def __aexit__(self, *args):
        return False


def _mock_session_get(responses):
    """Create a mock session whose .get() returns async context managers.

    Must be a *sync* side_effect that returns an _MockCtx (an async context
    manager), not an async function -- otherwise session.get() would return a
    coroutine which cannot be used with ``async with``.
    """
    def _get(url, headers=None, timeout=None):
        return _MockCtx(responses.pop(0))
    mock = MagicMock()
    mock.get = MagicMock(side_effect=_get)
    return mock


class TestDownloadWorker:
    async def test_download_success(self, tmp_path, db):
        from platform_video_downloader.core.worker import download_video
        from platform_video_downloader.config import DEFAULT_NAME_TEMPLATE

        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid, remote_id="123", name="TestUP",
            space_url="https://space.bilibili.com/123",
        )
        vid = await db.insert_video(
            creator_id=cid, remote_id="BV1xx", title="Test Video",
            duration=60, extra='{"cid": 456}',
        )
        did = await db.insert_download(
            video_id=vid, save_path=str(tmp_path / "test.mp4"), resolution="720p",
        )

        mock_api = AsyncMock()
        mock_api.get_video_info.return_value = {"cid": 456, "tags": ["test"]}
        mock_api.get_stream_urls.return_value = {
            "video_url": "http://example.com/video.mp4",
            "audio_url": "http://example.com/audio.mp3",
            "resolution": "720p",
        }
        mock_api.headers = {"Referer": "https://www.bilibili.com/"}

        video_resp = _make_mock_response(
            headers={"Content-Length": "1000"},
            chunks=[b"v" * 1000],
        )
        audio_resp = _make_mock_response(
            headers={"Content-Length": "500"},
            chunks=[b"a" * 500],
        )
        mock_session = _mock_session_get([video_resp, audio_resp])

        with patch("platform_video_downloader.core.worker._merge_audio_video", new_callable=AsyncMock) as mock_merge:
            async def fake_merge(video_path, audio_path, output_path, download_id, db_arg):
                # Simulate ffmpeg merge: concatenate video+audio content to output
                with open(video_path, "rb") as vf, open(audio_path, "rb") as af:
                    with open(output_path, "wb") as of:
                        of.write(vf.read())
                        of.write(af.read())
                os.remove(video_path)
                os.remove(audio_path)
                await db_arg._conn.execute(
                    "UPDATE download SET save_path=? WHERE id=?", (output_path, download_id)
                )
                await db_arg._conn.commit()
                return True
            mock_merge.side_effect = fake_merge

            await download_video(
                db=db, download_id=did,
                video={"id": vid, "remote_id": "BV1xx", "title": "Test Video", "extra": {"cid": 456}},
                creator_name="TestUP", section_name=None, save_dir=str(tmp_path),
                api=mock_api, session=mock_session,
                resolution_priority=["720p"],
                name_template=DEFAULT_NAME_TEMPLATE,
                api_semaphore=AsyncMock(), download_semaphore=AsyncMock(),
            )

        dl = await db.get_download(did)
        assert dl["status"] == "completed"
        # Final size = video (1000) + audio (500) after merge
        assert dl["file_size"] == 1500

    async def test_download_skipped_on_paid(self, tmp_path, db):
        from platform_video_downloader.core.worker import download_video
        from platform_video_downloader.config import DEFAULT_NAME_TEMPLATE

        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid, remote_id="123", name="TestUP",
            space_url="https://space.bilibili.com/123",
        )
        vid = await db.insert_video(
            creator_id=cid, remote_id="BV1xx", title="Paid Video",
            duration=60, extra='{"cid": 456}',
        )
        did = await db.insert_download(
            video_id=vid, save_path=str(tmp_path / "paid.mp4"), resolution="720p",
        )

        mock_api = AsyncMock()
        mock_api.get_video_info.return_value = {"cid": 456, "tags": []}
        mock_api.get_stream_urls.side_effect = ValueError("code=62002, message=需要充值")
        mock_session = MagicMock()

        await download_video(
            db=db, download_id=did,
            video={"id": vid, "remote_id": "BV1xx", "title": "Paid Video", "extra": {"cid": 456}},
            creator_name="TestUP", section_name=None, save_dir=str(tmp_path),
            api=mock_api, session=mock_session,
            resolution_priority=["720p"],
            name_template=DEFAULT_NAME_TEMPLATE,
            api_semaphore=AsyncMock(), download_semaphore=AsyncMock(),
        )

        dl = await db.get_download(did)
        # Paid/exclusive videos are deleted from the system entirely
        assert dl is None
        # Video record should also be removed
        v = await db._xq("SELECT id FROM video WHERE id=?", (vid,))
        assert await v.fetchone() is None


class TestDownloadVideoBroadcastSequence:
    async def test_happy_path_status_sequence(self, tmp_path, db):
        """download_video 全程广播序列（真实 ProgressReporter，不 mock make_progress）：

        downloading → (无 status 键的流式进度) → completed(file_size>0)。
        前端依赖 status 键做状态机路由、无 status 键做进度条原地更新——消息形状不可变更。
        """
        from platform_video_downloader.core.worker import download_video
        from platform_video_downloader.config import DEFAULT_NAME_TEMPLATE

        pid = await db.insert_platform(name="bilibili")
        cid = await db.insert_creator(
            platform_id=pid, remote_id="123", name="TestUP",
            space_url="https://space.bilibili.com/123",
        )
        vid = await db.insert_video(
            creator_id=cid, remote_id="BV1xx", title="Seq Video",
            duration=60, extra='{"cid": 456}',
        )
        did = await db.insert_download(
            video_id=vid, save_path=str(tmp_path / "seq.mp4"), resolution="720p",
        )

        mock_api = AsyncMock()
        mock_api.get_video_info.return_value = {"cid": 456, "tags": ["test"]}
        # 无 audio_url → 纯视频路径，跳过 ffmpeg 合并
        mock_api.get_stream_urls.return_value = {
            "video_url": "http://example.com/video.mp4",
            "audio_url": None,
            "resolution": "720p",
        }
        mock_api.headers = {"Referer": "https://www.bilibili.com/"}

        # 5 个 chunk：_download_stream 的 progress_counter 每 5 个 chunk 广播一次，
        # 恰好触发 1 条无 status 键的流式进度消息
        video_resp = _make_mock_response(
            headers={"Content-Length": "1000"},
            chunks=[b"v" * 200] * 5,
        )
        mock_session = _mock_session_get([video_resp])

        ws = MagicMock()
        ws.broadcast = AsyncMock()

        await download_video(
            db=db, download_id=did,
            video={"id": vid, "remote_id": "BV1xx", "title": "Seq Video", "extra": {"cid": 456}},
            creator_name="TestUP", section_name=None, save_dir=str(tmp_path),
            api=mock_api, session=mock_session,
            resolution_priority=["720p"],
            name_template=DEFAULT_NAME_TEMPLATE,
            api_semaphore=AsyncMock(), download_semaphore=AsyncMock(),
            ws_manager=ws,
        )

        msgs = [c.args[0] for c in ws.broadcast.await_args_list
                if c.args[0]["type"] == "download_progress"]
        statuses = [m.get("status") for m in msgs]
        assert statuses[0] == "downloading"
        assert statuses[-1] == "completed"
        # 终态广播携带正数 file_size
        assert msgs[-1]["file_size"] > 0
        # 中间流式进度消息必须无 status 键（前端据此路由到进度条原地更新分支）
        intermediate = msgs[1:-1]
        assert intermediate, "expected at least one status-less stream progress broadcast"
        for m in intermediate:
            assert "status" not in m
            assert m["file_size"] > 0
        # 所有 download_progress 消息都属于本下载
        assert {m["download_id"] for m in msgs} == {did}


class TestDownloadStream:
    async def test_download_stream_basic(self, tmp_path, db):
        """Test _download_stream writes chunks to file and returns size."""
        from platform_video_downloader.core.worker import _download_stream

        pid = await db.insert_platform(name="bilibili")
        creator_id = await db.insert_creator(
            platform_id=pid, remote_id="1", name="T", space_url="https://space.bilibili.com/1",
        )
        video_id = await db.insert_video(
            creator_id=creator_id, remote_id="BV1", title="V", duration=10,
        )
        dl_id = await db.insert_download(
            video_id=video_id, save_path=str(tmp_path / "v.mp4"), resolution="720p",
        )

        save_path = str(tmp_path / "stream.bin")
        resp = _make_mock_response(
            headers={"Content-Length": "50"},
            chunks=[b"x" * 50],
        )
        mock_session = _mock_session_get([resp])

        total = await _download_stream(
            mock_session, "http://example.com/stream", save_path, dl_id, db,
        )
        assert total == 50
        assert os.path.exists(save_path)
        assert os.path.getsize(save_path) == 50

    async def test_download_stream_resume(self, tmp_path, db):
        """Test _download_stream resumes from existing_size."""
        from platform_video_downloader.core.worker import _download_stream

        pid = await db.insert_platform(name="bilibili")
        creator_id = await db.insert_creator(
            platform_id=pid, remote_id="1", name="T", space_url="https://space.bilibili.com/1",
        )
        video_id = await db.insert_video(
            creator_id=creator_id, remote_id="BV1", title="V", duration=10,
        )
        dl_id = await db.insert_download(
            video_id=video_id, save_path=str(tmp_path / "v.mp4"), resolution="720p",
        )

        save_path = str(tmp_path / "resume.bin")
        # Pre-existing partial file
        with open(save_path, "wb") as f:
            f.write(b"A" * 100)

        resp = _make_mock_response(
            headers={
                "Content-Range": "bytes 100-149/150",
                "Content-Length": "50",
            },
            chunks=[b"B" * 50],
        )
        mock_session = _mock_session_get([resp])

        total = await _download_stream(
            mock_session, "http://example.com/stream", save_path, dl_id, db,
            existing_size=100,
        )
        assert total == 150
        with open(save_path, "rb") as f:
            content = f.read()
        assert content == b"A" * 100 + b"B" * 50

    async def test_download_stream_with_speed_limit(self, tmp_path, db):
        """Test _download_stream respects speed_limit_bps."""
        from platform_video_downloader.core.worker import _download_stream

        pid = await db.insert_platform(name="bilibili")
        creator_id = await db.insert_creator(
            platform_id=pid, remote_id="1", name="T", space_url="https://space.bilibili.com/1",
        )
        video_id = await db.insert_video(
            creator_id=creator_id, remote_id="BV1", title="V", duration=10,
        )
        dl_id = await db.insert_download(
            video_id=video_id, save_path=str(tmp_path / "v.mp4"), resolution="720p",
        )

        save_path = str(tmp_path / "limited.bin")
        resp = _make_mock_response(
            headers={"Content-Length": "10"},
            chunks=[b"x" * 10],
        )
        mock_session = _mock_session_get([resp])

        total = await _download_stream(
            mock_session, "http://example.com/stream", save_path, dl_id, db,
            speed_limit_bps=10,
        )
        assert total == 10
        assert os.path.exists(save_path)


class TestSpeedLimit:
    async def test_speed_limit_constant_exists(self):
        from platform_video_downloader.config import MIN_SPEED_LIMIT_KB, DEFAULT_SPEED_LIMIT_MB
        assert MIN_SPEED_LIMIT_KB == 100
        assert DEFAULT_SPEED_LIMIT_MB == 0.0

    async def test_apply_speed_limit_no_limit(self):
        from platform_video_downloader.core.worker import _apply_speed_limit
        # speed_limit_bps=0 should return immediately
        await _apply_speed_limit(chunk_size=1024*1024, speed_limit_bps=0, elapsed=0)

    async def test_apply_speed_limit_throttles(self):
        from platform_video_downloader.core.worker import _apply_speed_limit
        import time
        start = time.monotonic()
        await _apply_speed_limit(chunk_size=100*1024, speed_limit_bps=100*1024, elapsed=0)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.8  # should sleep ~1 second
