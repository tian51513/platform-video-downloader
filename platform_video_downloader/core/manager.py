import asyncio
import logging

from platform_video_downloader.config import DEFAULT_NAME_TEMPLATE, MAX_CONCURRENT_DOWNLOADS, MAX_CONCURRENT_API_REQUESTS
from platform_video_downloader.core.ingest import enqueue_downloads
from platform_video_downloader.core.worker import download_video

logger = logging.getLogger(__name__)


class DownloadManager:
    def __init__(self, db, api, save_dir: str, resolution_priority: list[str] | None = None,
                 max_concurrent_downloads: int = MAX_CONCURRENT_DOWNLOADS,
                 max_concurrent_api: int = MAX_CONCURRENT_API_REQUESTS,
                 name_template: str = DEFAULT_NAME_TEMPLATE,
                 speed_limit_bps: int = 0,
                 ws_manager=None,
                 cancel_event=None):
        self.db = db
        self.api = api
        self.save_dir = save_dir
        self.resolution_priority = resolution_priority or ["720p", "480p", "1080p", "240p"]
        self.max_concurrent_downloads = max_concurrent_downloads
        self.max_concurrent_api = max_concurrent_api
        self.name_template = name_template
        self.speed_limit_bps = speed_limit_bps
        self.ws_manager = ws_manager
        self.cancel_event = cancel_event
        self.api_semaphore = asyncio.Semaphore(max_concurrent_api)
        self.download_semaphore = asyncio.Semaphore(max_concurrent_downloads)

    async def _build_queue(self, creator_id: int) -> list[dict]:
        videos = await self.db.get_videos_by_creator(creator_id)
        existing = await self.db.get_existing_downloads(creator_id)
        queue = []
        for video in videos:
            if (video["remote_id"], self.resolution_priority[0]) not in existing:
                queue.append(video)
        return queue

    async def run(self, session, auto_discover=True, exclude_platforms: set[str] | None = None):
        # Auto-discover: enqueue un-downloaded videos for all creators
        # Web模式下由TaskService管理，不需要auto-discover
        if auto_discover:
            creators = await self.db.get_all_creators()
            for creator in creators:
                # queue 已按 get_existing_downloads 预检过滤，行为与原内联循环一致
                queue = await self._build_queue(creator["id"])
                await enqueue_downloads(
                    self.db, creator["id"], queue,
                    creator_name=creator["name"],
                    save_dir=self.save_dir,
                    name_template=self.name_template,
                    resolution=self.resolution_priority[0],
                )

        # Process all pending downloads (fetch all pages, not just first page)
        all_pending: list[dict] = []
        page = 1
        while True:
            pending = await self.db.get_all_downloads(status="pending", page=page, page_size=200)
            items = pending["items"] if isinstance(pending, dict) else pending
            all_pending.extend(items)
            if len(all_pending) >= pending.get("total", 0) or not items:
                break
            page += 1
        pending_items = all_pending
        if exclude_platforms:
            pending_items = [d for d in pending_items if d.get("platform_name") not in exclude_platforms]
        if not pending_items:
            logger.info("No pending downloads")
            return

        tasks = []
        for dl in pending_items:
            video = await self.db.get_video(dl["video_id"])
            if not video:
                logger.warning(f"download id={dl['id']} 关联的 video_id={dl['video_id']} 不存在，跳过")
                continue
            creator_name = dl.get("creator_name")
            if not creator_name:
                creator_name = await self.db.get_creator_name_by_video(dl["video_id"])
                if not creator_name:
                    logger.warning(f"download id={dl['id']} 关联的 creator 不存在，跳过")
                    continue

            logger.info(f"开始下载: {video['title']} (bvid={video['remote_id']})")
            tasks.append(download_video(
                db=self.db, download_id=dl["id"], video=video,
                creator_name=creator_name, section_name=video.get("section_name"),
                save_dir=self.save_dir, api=self.api, session=session,
                resolution_priority=self.resolution_priority,
                name_template=self.name_template,
                api_semaphore=self.api_semaphore,
                download_semaphore=self.download_semaphore,
                speed_limit_bps=self.speed_limit_bps,
                ws_manager=self.ws_manager,
                cancel_event=self.cancel_event,
            ))

            if self.cancel_event and self.cancel_event.is_set():
                logger.info("Download cancelled, stopping")
                break

        await asyncio.gather(*tasks, return_exceptions=True)

    async def enqueue_creator_videos(self, creator_id: int):
        videos = await self.db.get_videos_by_creator(creator_id)
        creator = await self.db.get_creator(creator_id)
        # skip_existing=True：已有记录（completed/skipped/downloading/pending）一律跳过
        new_count = await enqueue_downloads(
            self.db, creator_id, videos,
            creator_name=creator["name"] if creator else "",
            save_dir=self.save_dir,
            name_template=self.name_template,
            resolution=self.resolution_priority[0],
            skip_existing=True,
        )
        logger.info(f"Enqueued {new_count} new downloads for creator {creator_id}")
        return new_count
