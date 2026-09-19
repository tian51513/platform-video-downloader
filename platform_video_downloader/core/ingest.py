"""采集结果入库与排队接缝：统一 CLI 与 Web 的 scrape→DB→pending 流水线。

行为基线（源自 insert_download 的 DB 级语义，本模块不改变它）：
- completed 下载永不重置；failed/skipped 在默认路径重置为 pending 重试；
- skip_existing=True（CLI 非 --force 语义）先用 get_existing_downloads 预检
  跳过已有记录 —— 该集合仅含 completed/skipped/downloading/pending，不含
  failed，因此 failed 在两种模式下都会重试；两模式的真实差异是 skipped
  是否被重置（skip_existing=True 时 skipped 保持原状）。
- CLI 裁定：CLI 只下载用户指定的 URL，manager.run 传 auto_discover=False
  （旧行为会隐式入队库中全部 UP 主，与"多UP主批量下载"语义冲突）。
"""

import json

from platform_video_downloader.storage.files import build_filename, resolve_save_path


async def upsert_videos(
    db, creator_id: int, videos: list[dict], sections: list[dict] | None = None,
) -> None:
    """逐个 upsert video 行；已存在的仅补 tags（不覆盖），新的写入全字段。"""
    section_map = {s["remote_id"]: s for s in (sections or [])}
    for video_data in videos:
        remote_id = video_data["remote_id"]
        sec = section_map.get(remote_id)
        video = await db.get_video_by_remote(creator_id, remote_id)
        if not video:
            await db.insert_video(
                creator_id=creator_id, remote_id=remote_id,
                title=video_data["title"],
                duration=video_data.get("duration"),
                pubdate=video_data.get("pubdate"),
                extra=json.dumps(video_data.get("extra")) if video_data.get("extra") is not None else None,
                section_id=sec["section_id"] if sec else None,
                section_name=sec["section_name"] if sec else None,
                tags=video_data.get("tags"),
            )
            video = await db.get_video_by_remote(creator_id, remote_id)
        tags = video_data.get("tags", [])
        if tags and not video.get("tags"):
            await db.update_video_tags(video["id"], tags)


async def _creator_download_count(db, creator_id: int) -> int:
    """该 creator 名下的 download 行数（新建计数基线）。"""
    res = await db.get_all_downloads(creator_id=creator_id, page=1, page_size=1)
    return res.get("total", 0) if isinstance(res, dict) else 0


async def enqueue_downloads(
    db, creator_id: int, videos: list[dict], *,
    creator_name: str, save_dir: str, name_template: str,
    resolution: str, skip_existing: bool = False,
) -> int:
    """为视频行列表创建 pending 下载记录，返回新建记录数。

    videos 必须是 DB video 行（含 id/title/section_name/remote_id）。
    skip_existing=True 时先用 get_existing_downloads 预检跳过已有记录
    （CLI 非 --force 语义；该集合仅含 completed/skipped/downloading/pending，
    不含 failed —— failed 在两种模式下都会重试）；False 时依赖 insert_download
    的 DB 级去重（completed 保留、failed/skipped 重置 pending —— Web 语义）。
    两模式的实际差异：skip_existing=True 时 skipped 状态不被重置。
    返回值按调用前后该 creator 的 download 行数差计（真正的 INSERT OR IGNORE
    新建数；重置与忽略不计入）。
    """
    existing: set = set()
    if skip_existing:
        existing = await db.get_existing_downloads(creator_id)  # {(remote_id, resolution), ...}
    before = await _creator_download_count(db, creator_id)
    for video in videos:
        if skip_existing and (video["remote_id"], resolution) in existing:
            continue
        filename = build_filename(
            title=video["title"], creator=creator_name,
            section=video.get("section_name"), bvid=video.get("remote_id", ""),
            template=name_template,
        )
        save_path = resolve_save_path(save_dir, filename)
        await db.insert_download(
            video_id=video["id"], save_path=save_path, resolution=resolution,
        )
    return await _creator_download_count(db, creator_id) - before
