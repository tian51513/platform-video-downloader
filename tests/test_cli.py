import pytest
from unittest.mock import patch, MagicMock, AsyncMock

class TestCLIParsing:
    def test_parse_download_command(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["https://space.bilibili.com/33676449"])
        assert args.command == "download"
        assert args.urls == ["https://space.bilibili.com/33676449"]
        assert args.output == "./downloads"

    def test_parse_multiple_urls(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["https://space.bilibili.com/33676449", "https://space.bilibili.com/123456"])
        assert len(args.urls) == 2

    def test_parse_output_dir(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["https://space.bilibili.com/33676449", "-o", r"E:\映畫\B"])
        assert args.output == r"E:\映畫\B"

    def test_parse_resolution(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["https://space.bilibili.com/33676449", "-r", "1080p"])
        assert args.resolution == "1080p"

    def test_parse_dry_run(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["https://space.bilibili.com/33676449", "--dry-run"])
        assert args.dry_run is True

    def test_parse_force(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["https://space.bilibili.com/33676449", "--force"])
        assert args.force is True

    def test_parse_web_command(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["web", "--port", "9090"])
        assert args.command == "web"
        assert args.port == 9090

    def test_parse_web_default_port(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["web"])
        assert args.port == 8080

    def test_parse_concurrency(self):
        from platform_video_downloader.cli.main import parse_args
        args = parse_args(["https://space.bilibili.com/33676449", "-n", "10"])
        assert args.concurrency == 10


def _make_download_args(**overrides):
    defaults = dict(
        urls=["https://space.bilibili.com/33676449"],
        output="./downloads",
        resolution=None,
        concurrency=5,
        dry_run=True,
        force=False,
        name_template=None,
        headed=False,
        no_cache=True,
        cookie=[],
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


class TestDownloadCommandCleanup:
    async def test_db_closed_when_browser_start_fails(self):
        """browser.start() 抛错（如 chromium 未安装）时 db 必须被关闭，
        否则 aiosqlite 非 daemon 线程会导致进程无法退出。"""
        from platform_video_downloader.cli.main import download_command

        db_instance = AsyncMock()
        db_instance.get_stats.return_value = {}

        browser_instance = AsyncMock()
        browser_instance.start.side_effect = RuntimeError("Executable doesn't exist")

        with patch("platform_video_downloader.cli.main.Database", return_value=db_instance), \
             patch("platform_video_downloader.browser.PlaywrightBrowser", return_value=browser_instance):
            with pytest.raises(RuntimeError):
                await download_command(_make_download_args())

        db_instance.close.assert_awaited()

    async def test_db_closed_when_dry_run_completes(self):
        from platform_video_downloader.cli.main import download_command

        db_instance = AsyncMock()
        db_instance.get_platform_by_name.return_value = {"id": 1}
        db_instance.get_creator_by_remote.return_value = {"id": 2}
        db_instance.get_stats.return_value = {}

        browser_instance = AsyncMock()
        browser_instance.page = AsyncMock()
        scraper_instance = AsyncMock()
        scraper_instance.collect.return_value = {"space_info": {"name": "up"}, "videos": [], "sections": []}

        with patch("platform_video_downloader.cli.main.Database", return_value=db_instance), \
             patch("platform_video_downloader.browser.PlaywrightBrowser", return_value=browser_instance), \
             patch("platform_video_downloader.bilibili.scraper.BilibiliScraper", return_value=scraper_instance), \
             patch.dict("os.environ", {"BILIBILI_SESSDATA": "x"}, clear=False):
            await download_command(_make_download_args())

        db_instance.close.assert_awaited()
