import pytest
import asyncio
import aiohttp


class TestRetry:
    async def test_success_no_retry(self):
        from platform_video_downloader.core.retry import retry_async

        call_count = 0

        async def success():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await retry_async(success, max_retries=3)
        assert result == "ok"
        assert call_count == 1

    async def test_retry_then_success(self):
        from platform_video_downloader.core.retry import retry_async

        call_count = 0

        async def fail_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise aiohttp.ClientError("timeout")
            return "ok"

        result = await retry_async(fail_twice, max_retries=3, backoff_base=0)
        assert result == "ok"
        assert call_count == 3

    async def test_exhaust_retries_raises(self):
        from platform_video_downloader.core.retry import retry_async

        async def always_fail():
            raise ConnectionError("dead")

        with pytest.raises(ConnectionError):
            await retry_async(always_fail, max_retries=2, backoff_base=0)

    async def test_no_retry_on_permanent_error(self):
        from platform_video_downloader.core.retry import retry_async

        call_count = 0

        async def fail_404():
            nonlocal call_count
            call_count += 1
            raise ValueError("API error: code=-404, message=视频不可用")

        with pytest.raises(ValueError):
            await retry_async(fail_404, max_retries=3, backoff_base=0)
        assert call_count == 1

    async def test_no_retry_on_skip_error(self):
        from platform_video_downloader.core.retry import retry_async

        call_count = 0

        async def fail_skip():
            nonlocal call_count
            call_count += 1
            raise ValueError("skipped: 需充值")

        with pytest.raises(ValueError, match="skipped"):
            await retry_async(fail_skip, max_retries=3, backoff_base=0)
        assert call_count == 1


class TestIsPermanentError:
    def test_all_keywords_permanent(self):
        from platform_video_downloader.core.retry import is_permanent_error
        for msg in (
            "code=-404, message=啥都木有",
            "code=62002, message=仅UP主自己可见",
            "code=87008, message=充电专属",
            "code=62002, message=需要充值",
            "No video streams available",
        ):
            assert is_permanent_error(msg), msg

    def test_transient_not_permanent(self):
        from platform_video_downloader.core.retry import is_permanent_error
        assert not is_permanent_error("timeout")
        assert not is_permanent_error(ValueError("server error 500"))
        assert not is_permanent_error("")
        assert not is_permanent_error("download skipped")

    async def test_permanent_error_raises_immediately(self):
        from platform_video_downloader.core.retry import is_permanent_error, retry_async
        calls = []
        async def failing():
            calls.append(1)
            raise ValueError("code=-404 not found")
        with pytest.raises(ValueError):
            await retry_async(failing, max_retries=3, backoff_base=0.01)
        assert len(calls) == 1  # 永久错误不重试
