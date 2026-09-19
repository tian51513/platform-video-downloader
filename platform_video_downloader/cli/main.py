import argparse
import asyncio
import json
import logging
import re
import sys

import aiohttp

from platform_video_downloader.config import (
    BILIBILI_SPACE_URL_PATTERN,
    DEFAULT_COOKIE_CACHE_PATH,
    DEFAULT_NAME_TEMPLATE,
    DEFAULT_RESOLUTION_PRIORITY,
    DEFAULT_WEB_PORT,
    MAX_CONCURRENT_DOWNLOADS,
    get_effective_db_path,
)
from platform_video_downloader.bilibili.api import BilibiliAPI
from platform_video_downloader.core.manager import DownloadManager
from platform_video_downloader.storage.database import Database
from platform_video_downloader.storage.files import build_filename, resolve_save_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="pvd", description="多平台视频批量下载器")
    subparsers = parser.add_subparsers(dest="command")

    dl_parser = subparsers.add_parser("download", help="下载UP主视频")
    dl_parser.add_argument("urls", nargs="+", help="UP主空间地址")
    dl_parser.add_argument("-o", "--output", default="./downloads", help="保存目录")
    dl_parser.add_argument("-r", "--resolution", default=None, help="目标分辨率")
    dl_parser.add_argument("-n", "--concurrency", type=int, default=MAX_CONCURRENT_DOWNLOADS, help="并发下载数")
    dl_parser.add_argument("--dry-run", action="store_true", help="仅分析不下载")
    dl_parser.add_argument("--force", action="store_true", help="忽略已下载记录")
    dl_parser.add_argument("--name-template", default=None, help="文件命名模板")
    dl_parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（调试用）")
    dl_parser.add_argument("--no-cache", action="store_true", help="不使用缓存的cookie，强制重新登录")
    dl_parser.add_argument("--cookie", action="append", default=[], metavar="NAME=VALUE",
                           help="B站cookie，如 --cookie SESSDATA=xxx （也可用环境变量 BILIBILI_SESSDATA）")

    web_parser = subparsers.add_parser("web", help="启动Web仪表盘")
    web_parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="Web服务端口")

    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] not in ("download", "web"):
        argv = ["download"] + argv
    args = parser.parse_args(argv)
    return args


def extract_mid(url: str) -> str:
    match = re.search(BILIBILI_SPACE_URL_PATTERN, url)
    if not match:
        raise ValueError(f"Invalid Bilibili space URL: {url}")
    return match.group(1)


def _resolve_cookies(cli_cookies: list[dict], args) -> tuple[list[dict], bool]:
    """解析 cookies 来源：CLI参数 > 环境变量 > 缓存文件 > 扫码登录。

    Returns:
        (cookies, needs_qr_login) 元组。
    """
    from platform_video_downloader.browser import build_cookies_from_env, load_cookies_from_file

    if cli_cookies:
        logger.info("使用 CLI 参数提供的 cookies")
        return cli_cookies, False

    env_cookies = build_cookies_from_env()
    if env_cookies:
        logger.info("使用环境变量提供的 cookies")
        return env_cookies, False

    if not args.no_cache:
        cached = load_cookies_from_file(DEFAULT_COOKIE_CACHE_PATH)
        if cached:
            logger.info("使用缓存的 cookies")
            return cached, False

    logger.info("未找到 cookies（CLI参数、环境变量、缓存均无），将启动扫码登录")
    return [], True


async def download_command(args):
    db = Database(get_effective_db_path())
    await db.init()
    try:
        await _run_download(args, db)
    finally:
        # 任何退出路径（含 browser.start() 抛错）都必须关闭数据库，
        # 否则 aiosqlite 非 daemon 工作线程会阻塞进程退出
        await db.close()


async def _run_download(args, db):
    """下载主流程（调用方负责 db 生命周期）。"""

    resolution_priority = [args.resolution] if args.resolution else DEFAULT_RESOLUTION_PRIORITY

    # --- Phase 1: 数据采集（Playwright 浏览器） ---
    from platform_video_downloader.browser import (
        BILIBILI_COOKIE_DOMAIN,
        PlaywrightBrowser,
        qr_code_login,
        save_cookies_to_file,
    )
    from platform_video_downloader.bilibili.scraper import BilibiliScraper

    # 构建 CLI cookies
    cli_cookies = []
    for c in args.cookie:
        if '=' not in c:
            logger.warning(f"忽略无效 cookie 参数: {c}")
            continue
        name, _, value = c.partition('=')
        cli_cookies.append({"name": name, "value": value, "domain": BILIBILI_COOKIE_DOMAIN, "path": "/"})

    # 解析 cookies 来源
    resolved_cookies, needs_qr_login = _resolve_cookies(cli_cookies, args)
    use_headed = args.headed or needs_qr_login

    browser = PlaywrightBrowser(headless=not use_headed, cookies=resolved_cookies or None)
    await browser.start()

    # 如果需要扫码登录
    if needs_qr_login:
        login_cookies = await qr_code_login(browser.page)
        save_cookies_to_file(login_cookies, DEFAULT_COOKIE_CACHE_PATH)
        resolved_cookies = login_cookies

    scraper = BilibiliScraper(browser)

    try:
        for url in args.urls:
            try:
                mid = extract_mid(url)
                logger.info(f"Processing UP主: {url} (mid={mid})")

                platform = await db.get_platform_by_name("bilibili")
                pid = platform["id"] if platform else await db.insert_platform(name="bilibili", base_url="https://www.bilibili.com")

                creator = await db.get_creator_by_remote(pid, mid)
                if not creator:
                    # 通过空间页面采集UP主数据
                    data = await scraper.collect(mid)
                    info = data['space_info']
                    if not info:
                        raise ValueError(f"无法获取UP主信息: mid={mid}")
                    cid = await db.insert_creator(platform_id=pid, remote_id=mid, name=info["name"], space_url=url, avatar_url=info.get("avatar_url"))
                else:
                    cid = creator["id"]
                    data = await scraper.collect(mid)

                videos = data['videos']
                sections = data['sections']
                section_map = {s["remote_id"]: s for s in sections}

                existing = set()
                if not args.force:
                    existing = await db.get_existing_downloads(cid)

                new_videos = 0
                for video_data in videos:
                    remote_id = video_data["remote_id"]
                    sec = section_map.get(remote_id)
                    video = await db.get_video_by_remote(cid, remote_id)
                    if not video:
                        vid = await db.insert_video(
                            creator_id=cid, remote_id=remote_id, title=video_data["title"],
                            duration=video_data.get("duration"), pubdate=video_data.get("pubdate"),
                            extra=json.dumps(video_data.get("extra")),
                            section_id=sec["section_id"] if sec else None,
                            section_name=sec["section_name"] if sec else None,
                        )
                        video = await db.get_video(vid)
                    # 存储标签（采集阶段直接从 arc/search vlist.tag 获取）
                    tags = video_data.get("tags", [])
                    if tags and not video.get("tags"):
                        await db.update_video_tags(video["id"], tags)

                    if not args.force and (remote_id, resolution_priority[0]) in existing:
                        continue

                    creator_info = await db.get_creator(cid)
                    filename = build_filename(
                        title=video_data["title"], creator=creator_info["name"],
                        section=sec["section_name"] if sec else None,
                        bvid=video_data["remote_id"],
                        template=args.name_template or DEFAULT_NAME_TEMPLATE,
                    )
                    save_path = resolve_save_path(args.output, filename)
                    await db.insert_download(video_id=video["id"], save_path=save_path, resolution=resolution_priority[0])
                    new_videos += 1

                await db.update_creator_sync(cid)
                logger.info(f"Found {len(videos)} videos, {new_videos} new downloads queued")
            except Exception as e:
                logger.error(f"UP主处理失败: {url} — {e}")
    finally:
        await browser.close()

    if args.dry_run:
        stats = await db.get_stats()
        logger.info(f"Dry run complete: {stats}")
        return

    # --- Phase 2: 下载（aiohttp） ---
    async with aiohttp.ClientSession() as session:
        api = BilibiliAPI(session, cookies=resolved_cookies or None)
        manager = DownloadManager(
            db=db, api=api, save_dir=args.output,
            resolution_priority=resolution_priority,
            max_concurrent_downloads=args.concurrency,
            name_template=args.name_template or DEFAULT_NAME_TEMPLATE,
        )
        await manager.run(session)

    stats = await db.get_stats()
    logger.info(f"Done: {stats}")


async def web_command(args):
    import uvicorn
    from platform_video_downloader.web.app import create_app
    from platform_video_downloader.web.ws_manager import get_ws_manager
    from platform_video_downloader.web.task_service import TaskService
    db = Database(get_effective_db_path())
    await db.init()
    await db.cleanup_stale_downloads()
    task_service = TaskService(db=db, ws_manager=get_ws_manager())
    app = create_app(db, task_service=task_service)
    logger.info(f"Web dashboard starting on http://localhost:{args.port}")
    config = uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.command == "web":
            asyncio.run(web_command(args))
        else:
            asyncio.run(download_command(args))
    except KeyboardInterrupt:
        logger.info("已取消")
        return 130
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        return 1
    return 0


def cli_entry():
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
