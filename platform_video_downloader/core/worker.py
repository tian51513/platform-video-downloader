import asyncio
import json
import logging
import os
import shutil
import subprocess
import time

import aiohttp

from platform_video_downloader.config import MIN_SPEED_LIMIT_KB, REQUEST_TIMEOUT
from platform_video_downloader.core.progress import make_progress
from platform_video_downloader.core.retry import is_permanent_error, retry_async
from platform_video_downloader.storage.files import build_filename, resolve_save_path

logger = logging.getLogger(__name__)


async def _apply_speed_limit(chunk_size: int, speed_limit_bps: int, elapsed: float):
    """Sleep to enforce speed limit. No-op if speed_limit_bps is 0 (unlimited)."""
    if speed_limit_bps <= 0:
        return
    expected_time = chunk_size / speed_limit_bps
    if elapsed < expected_time:
        await asyncio.sleep(expected_time - elapsed)


async def _download_stream(
    session: aiohttp.ClientSession,
    url: str,
    save_path: str,
    download_id: int,
    db,
    headers: dict | None = None,
    chunk_size: int = 1024 * 1024,
    speed_limit_bps: int = 0,
    existing_size: int = 0,
    total_size: int = 0,
    progress=None,
) -> int:
    """Download a stream with resume support and optional speed limit.

    Returns total bytes downloaded (including existing).
    """
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    download_headers = dict(headers or {"Referer": "https://www.bilibili.com/"})
    if existing_size > 0:
        download_headers["Range"] = f"bytes={existing_size}-"

    timeout = aiohttp.ClientTimeout(total=300, connect=30)
    async with session.get(url, headers=download_headers, timeout=timeout) as resp:
        # 416 = 本地临时文件大小超过服务器文件（流URL过期/变更），删除临时文件重新下载
        if resp.status == 416:
            logger.warning(f"[download] 416 Range Not Satisfiable, restarting: {save_path}")
            if os.path.exists(save_path):
                os.remove(save_path)
            download_headers.pop("Range", None)
            async with session.get(url, headers=download_headers, timeout=timeout) as resp2:
                resp2.raise_for_status()
                downloaded = 0
                mode = "wb"
                progress_counter = 0
                with open(save_path, mode) as f:
                    async for chunk in resp2.content.iter_chunked(chunk_size):
                        chunk_start = time.monotonic()
                        f.write(chunk)
                        downloaded += len(chunk)
                        chunk_elapsed = time.monotonic() - chunk_start
                        await _apply_speed_limit(len(chunk), speed_limit_bps, chunk_elapsed)
                        if downloaded % (chunk_size * 10) == 0 or downloaded == 0:
                            await db.update_download_progress(download_id, downloaded)
                        progress_counter += 1
                        if progress_counter % 5 == 0 and progress:
                            await progress(file_size=downloaded, total_size=0)
                return downloaded
        resp.raise_for_status()
        # Determine total size
        if not total_size:
            content_range = resp.headers.get("Content-Range", "")
            if content_range and "/" in content_range:
                total_size_str = content_range.split("/")[-1]
                total_size = int(total_size_str) if total_size_str != "*" else 0
            else:
                total_size = existing_size + int(resp.headers.get("Content-Length", 0))

        if total_size > 0:
            await db.update_download_total_size(download_id, total_size)

        downloaded = existing_size
        mode = "ab" if existing_size > 0 else "wb"
        progress_counter = 0
        with open(save_path, mode) as f:
            async for chunk in resp.content.iter_chunked(chunk_size):
                chunk_start = time.monotonic()
                f.write(chunk)
                downloaded += len(chunk)
                chunk_elapsed = time.monotonic() - chunk_start
                await _apply_speed_limit(len(chunk), speed_limit_bps, chunk_elapsed)
                if downloaded % (chunk_size * 10) == 0 or downloaded == total_size:
                    await db.update_download_progress(download_id, downloaded)
                progress_counter += 1
                if progress_counter % 5 == 0 and progress:
                    await progress(file_size=downloaded, total_size=total_size)
        return downloaded


async def _merge_audio_video(
    video_path: str,
    audio_path: str,
    output_path: str,
    download_id: int,
    db,
):
    """Merge video and audio streams using ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe()
        except ImportError:
            pass
    if not ffmpeg:
        logger.warning("ffmpeg not found, skipping merge. Files saved separately.")
        return False
    try:
        cmd = [
            ffmpeg, "-y",
            "-i", video_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "copy",
            output_path,
        ]
        logger.info(f"[merge] Running: {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"[merge] ffmpeg failed: {stderr.decode()[:500]}")
            return False
        os.remove(video_path)
        os.remove(audio_path)
        logger.info(f"[merge] Merged to {output_path}, removed temp files")
        await db.update_download_save_path(download_id, output_path)
        return True
    except Exception as e:
        logger.error(f"[merge] Error: {e}")
        return False


async def download_video(
    db,
    download_id: int,
    video: dict,
    creator_name: str,
    section_name: str | None,
    save_dir: str,
    api,
    session: aiohttp.ClientSession,
    resolution_priority: list[str],
    name_template: str,
    api_semaphore: asyncio.Semaphore,
    download_semaphore: asyncio.Semaphore,
    speed_limit_bps: int = 0,
    ws_manager=None,
    cancel_event=None,
):
    bvid = video["remote_id"]
    progress = make_progress(ws_manager, download_id)
    try:
        if cancel_event and cancel_event.is_set():
            return
        await db.update_download_status(download_id, "downloading")
        await progress.downloading()

        extra = video.get("extra", {})
        if isinstance(extra, str):
            extra = json.loads(extra)
        cid = extra.get("cid")
        logger.debug(f"[worker] {bvid} extra={extra}, cid={cid}")

        # 1. Get stream URLs
        stream = None
        async with api_semaphore:
            if not cid:
                logger.info(f"[worker] {bvid} missing cid, fetching via API...")
                video_info = await retry_async(
                    lambda: api.get_video_info(bvid),
                    max_retries=3, backoff_base=2,
                )
                cid = video_info["cid"]
            else:
                video_info = None

            # 检查是否为充电专属视频（get_video_info 返回 is_upower_exclusive）
            if video_info and video_info.get("is_upower_exclusive"):
                await db.remove_paid_video(download_id)
                await progress.removed()
                logger.info(f"Removed {bvid}: UP主充电专属视频")
                return

            # Fetch tags if video doesn't have them yet
            if video.get("id") and not video.get("tags"):
                try:
                    if not video_info:
                        video_info = await retry_async(
                            lambda: api.get_video_info(bvid),
                            max_retries=2, backoff_base=2,
                        )
                    if video_info and video_info.get("is_upower_exclusive"):
                        await db.remove_paid_video(download_id)
                        await progress.removed()
                        logger.info(f"Removed {bvid}: UP主充电专属视频")
                        return
                    if video_info and video_info.get("tags"):
                        await db.update_video_tags(video["id"], video_info["tags"])
                except Exception as e:
                    logger.debug(f"[worker] {bvid} tag fetch failed: {e}")

            try:
                stream = await retry_async(
                    lambda: api.get_stream_urls(
                        video["remote_id"], cid, resolution_priority
                    ),
                    max_retries=3, backoff_base=2,
                )
                logger.debug(f"[worker] {bvid} stream: {stream['resolution']}, "
                             f"video={stream['video_url'][:60]}... audio={'yes' if stream.get('audio_url') else 'no'}")
            except ValueError as e:
                if is_permanent_error(e):
                    # Delete the download and video record — paid/exclusive videos don't belong in the system
                    await db.remove_paid_video(download_id)
                    await progress.removed()
                    logger.info(f"Removed {bvid}: paid/exclusive video ({e})")
                    return
                raise

        # 2. Build filename and paths
        filename = build_filename(
            title=video["title"], creator=creator_name,
            section=section_name, bvid=bvid, template=name_template,
        )
        save_path = resolve_save_path(save_dir, filename)
        video_tmp = save_path + ".video.tmp"
        audio_tmp = save_path + ".audio.tmp"

        # 3. Download video stream with resume + speed limit
        existing_video_size = 0
        if os.path.exists(video_tmp):
            existing_video_size = os.path.getsize(video_tmp)
            logger.info(f"[worker] {bvid} Resuming video from {existing_video_size} bytes")

        async with download_semaphore:
            try:
                await _download_stream(
                    session, stream["video_url"], video_tmp, download_id, db,
                    headers=api.headers, speed_limit_bps=speed_limit_bps,
                    existing_size=existing_video_size,
                    total_size=stream.get("video_size", 0),
                    progress=progress,
                )
            except Exception as dl_err:
                raise RuntimeError(f"视频流下载失败: {dl_err}") from dl_err

        # 4. Download audio stream with resume + speed limit
        if stream.get("audio_url"):
            existing_audio_size = 0
            if os.path.exists(audio_tmp):
                existing_audio_size = os.path.getsize(audio_tmp)
                logger.info(f"[worker] {bvid} Resuming audio from {existing_audio_size} bytes")
            async with download_semaphore:
                try:
                    await _download_stream(
                        session, stream["audio_url"], audio_tmp, download_id, db,
                        headers=api.headers, speed_limit_bps=speed_limit_bps,
                        existing_size=existing_audio_size,
                        total_size=stream.get("audio_size", 0),
                        progress=progress,
                    )
                except Exception as dl_err:
                    raise RuntimeError(f"音频流下载失败: {dl_err}") from dl_err

        # 5. Merge audio + video with ffmpeg
        if stream.get("audio_url") and os.path.exists(video_tmp) and os.path.exists(audio_tmp):
            await progress.merging()
            merged = await _merge_audio_video(video_tmp, audio_tmp, save_path, download_id, db)
            if not merged and os.path.exists(video_tmp):
                # ffmpeg failed or not found — save video only, keep audio separately
                audio_final = save_path + ".audio.mp4"
                if not os.path.exists(audio_final):
                    os.rename(audio_tmp, audio_final)
                os.rename(video_tmp, save_path)
                logger.warning(f"[worker] {bvid} ffmpeg不可用，视频已保存为 {save_path}，音频另存为 {audio_final}")
        elif not stream.get("audio_url") and os.path.exists(video_tmp):
            os.rename(video_tmp, save_path)

        # 6. Update final status
        if os.path.exists(save_path):
            final_size = os.path.getsize(save_path)
            await db.update_download_progress(download_id, final_size, stream["resolution"])
            await db.update_download_status(download_id, "completed")
            await progress.completed(file_size=final_size)
            logger.info(f"Completed {filename} ({stream['resolution']}, {final_size} bytes)")
        else:
            await db.update_download_status(download_id, "failed", "下载完成但输出文件丢失")
            await progress.failed()

    except Exception as e:
        logger.error(f"Failed {bvid}: {e}", exc_info=True)
        await db.update_download_status(download_id, "failed", str(e))
        await progress.failed()
