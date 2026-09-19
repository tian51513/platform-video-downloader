"""YouTube 平台实现 — 基于 yt-dlp。"""

from __future__ import annotations

import logging
import os
import re
import shutil

from platform_video_downloader.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

_PLAYLIST_PATTERN = re.compile(
    r'https?://(?:www\.)?youtube\.com/watch\?.*list=([A-Za-z0-9_-]+)'
)
_DIRECT_PLAYLIST_PATTERN = re.compile(
    r'https?://(?:www\.)?youtube\.com/playlist\?list=([A-Za-z0-9_-]+)'
)


class YouTubePlatform(BasePlatform):
    name = "youtube"
    display_name = "YouTube"
    base_url = "https://www.youtube.com"
    cookie_domain = ".youtube.com"
    cookie_cache_path = ""

    def parse_url(self, url: str) -> dict:
        match = re.search(_PLAYLIST_PATTERN, url) or re.search(_DIRECT_PLAYLIST_PATTERN, url)
        if not match:
            raise ValueError(f"无效的 YouTube URL（需包含播放列表）: {url}")
        playlist_id = match.group(1)
        return {
            "creator_id": playlist_id,
            "creator_name": None,
            "space_url": f"https://www.youtube.com/playlist?list={playlist_id}",
        }

    async def scrape(self, creator_id: str, **kwargs) -> dict:
        """使用 yt-dlp extract_info 获取播放列表视频元数据。"""
        import asyncio
        import yt_dlp

        playlist_url = f"https://www.youtube.com/playlist?list={creator_id}"
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
        }

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(playlist_url, download=False)

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, _extract)
        except Exception as e:
            logger.error(f"[YouTubePlatform] 播放列表获取失败: {e}")
            raise

        videos: list[dict] = []
        playlist_title = None
        if info:
            playlist_title = info.get("title")
            entries = info.get("entries", [])
            for entry in entries:
                if entry is None:
                    continue
                videos.append({
                    "remote_id": entry.get("id", ""),
                    "title": entry.get("title", ""),
                    "duration": entry.get("duration", 0) or 0,
                    "pubdate": entry.get("upload_date") or entry.get("upload_date_iso8601"),
                    "extra": {
                        "thumbnail": entry.get("thumbnail"),
                        "url": entry.get("url"),
                        "channel": entry.get("channel"),
                    },
                    "tags": entry.get("tags", []) or [],
                })

        logger.info(f"[YouTubePlatform] 播放列表 '{playlist_title}': {len(videos)} 个视频")

        creator_info = None
        if playlist_title:
            creator_info = {
                "name": playlist_title,
                "avatar_url": None,
                "remote_id": creator_id,
            }

        return {
            "creator_info": creator_info,
            "videos": videos,
        }

    async def download_single(
        self,
        video: dict,
        save_dir: str,
        name_template: str,
        download_id: int,
        db,
        ws_manager=None,
        cancel_event=None,
        speed_limit_bps: int = 0,
        **kwargs,
    ) -> dict:
        """使用 yt-dlp 下载单个 YouTube 视频。"""
        import asyncio
        import yt_dlp

        from platform_video_downloader.storage.files import sanitize_filename

        video_id = video["remote_id"]
        title = video["title"]
        creator_name = kwargs.get("creator_name", "")
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        # 构建文件名
        safe_title = sanitize_filename(title)
        safe_creator = sanitize_filename(creator_name)
        filename_base = f"{safe_title}"
        if safe_creator:
            filename_base = f"{filename_base}【{safe_creator}】"
        filename_base = f"{filename_base}_{video_id}"

        save_path = os.path.join(save_dir, f"{filename_base}.mp4")
        os.makedirs(save_dir, exist_ok=True)

        # 进度状态
        progress_data = {"downloaded": 0, "total": 0}

        async def _broadcast_progress():
            if ws_manager and progress_data["total"] > 0:
                await ws_manager.broadcast({
                    "type": "download_progress",
                    "download_id": download_id,
                    "file_size": progress_data["downloaded"],
                    "total_size": progress_data["total"],
                })

        def _progress_hook(d):
            if d["status"] == "downloading":
                progress_data["downloaded"] = d.get("downloaded_bytes", 0)
                progress_data["total"] = d.get("total_bytes", 0) or d.get("total_bytes_estimate", 0)
                if progress_data["total"] == 0:
                    total_str = d.get("total_bytes_estimate", 0)
                    progress_data["total"] = total_str if total_str else 0
            elif d["status"] == "finished":
                progress_data["downloaded"] = d.get("total_bytes", 0) or os.path.getsize(save_path, 0)
                progress_data["total"] = progress_data["downloaded"]
                logger.info(f"[YouTubePlatform] 下载完成: {title}")

        # 检查是否已取消
        if cancel_event and cancel_event.is_set():
            return {"status": "failed", "error": "已取消"}

        await db.update_download_status(download_id, "downloading")
        if ws_manager:
            await ws_manager.broadcast({
                "type": "download_progress",
                "download_id": download_id,
                "status": "downloading",
            })

        # 构建 yt-dlp 选项
        outtmpl = os.path.join(save_dir, f"{filename_base}.%(ext)s")
        ydl_opts = {
            'outtmpl': outtmpl,
            'format': 'best[ext=mp4]/best',
            'progress_hooks': [_progress_hook],
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
        }

        # JS 运行时（YouTube 签名解析需要）
        for candidate in [
            shutil.which("node") or "node",
            r"D:\program\nodejs\node.exe",
            os.path.join(os.environ.get("PROGRAMFILES", ""), "nodejs", "node.exe"),
        ]:
            if os.path.isfile(candidate) or shutil.which(candidate):
                ydl_opts['js_runtimes'] = {'node': {'path': candidate if os.path.isfile(candidate) else shutil.which(candidate)}}
                ydl_opts['remote_components'] = {'ejs:github'}
                break

        # 限速
        if speed_limit_bps > 0:
            ydl_opts['ratelimit'] = speed_limit_bps

        # 代理
        proxy = kwargs.get("proxy", "")
        if proxy:
            ydl_opts['proxy'] = proxy

        # Cookie 优先级：手动传入 > YouTube cookie 文件 > Chrome 浏览器 > 无 cookie
        cookies = kwargs.get("cookies")
        cookie_used = False
        if cookies:
            # 手动传入的 cookie（Bilibili 格式 list[dict]）
            cookie_file = os.path.join(save_dir, ".yt-dlp-cookies.txt")
            try:
                with open(cookie_file, "w", encoding="utf-8") as f:
                    for c in cookies:
                        domain = c.get("domain", ".youtube.com")
                        f.write(f"{domain}\tTRUE\t/\tTRUE\t{c.get('expires', '0')}\t{c.get('name', '')}\t{c.get('value', '')}\n")
                ydl_opts['cookiefile'] = cookie_file
                cookie_used = True
                logger.info("[YouTubePlatform] 使用手动传入 cookie")
            except Exception as e:
                logger.debug(f"[YouTubePlatform] Cookie 文件写入失败: {e}")

        if not cookie_used:
            # 检查 YouTube cookie 文件
            cookie_file_path = kwargs.get("cookie_file", "")
            if cookie_file_path and os.path.exists(cookie_file_path):
                ydl_opts['cookiefile'] = cookie_file_path
                cookie_used = True
                logger.info(f"[YouTubePlatform] 使用 cookie 文件: {cookie_file_path}")

        if not cookie_used:
            logger.info("[YouTubePlatform] 未配置 cookie，以未登录状态下载")

        try:
            # yt-dlp 下载是同步的，放到线程池中执行
            actual_resolution = "best"

            def _download():
                nonlocal actual_resolution
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # 先提取格式信息获取实际分辨率
                    try:
                        info = ydl.extract_info(video_url, download=False)
                        if info:
                            height = info.get("height") or info.get("resolution")
                            if height:
                                actual_resolution = f"{height}p"
                            fmt = info.get("format", "")
                            # 从 format 字符串提取分辨率
                            if not height and fmt:
                                import re as _re
                                m = _re.search(r"(\d{3,4})x(\d{3,4})", fmt)
                                if m:
                                    actual_resolution = f"{m.group(2)}p"
                    except Exception as e:
                        logger.debug(f"[YouTubePlatform] 获取分辨率信息失败: {e}")
                    ydl.download([video_url])

            loop = asyncio.get_event_loop()
            # 定期广播进度
            download_task = loop.run_in_executor(None, _download)

            while not download_task.done():
                await asyncio.sleep(1)
                await _broadcast_progress()
                if cancel_event and cancel_event.is_set():
                    return {"status": "failed", "error": "已取消"}

            # 等待完成（可能已经完成了）
            try:
                await asyncio.wait_for(download_task, timeout=5)
            except asyncio.TimeoutError:
                pass

            # 确定最终文件路径（yt-dlp 可能更改扩展名）
            final_path = save_path
            if not os.path.exists(final_path):
                for ext in [".mkv", ".webm", ".mp4", ".avi"]:
                    candidate = os.path.join(save_dir, f"{filename_base}{ext}")
                    if os.path.exists(candidate):
                        final_path = candidate
                        break

            if not os.path.exists(final_path):
                await db.update_download_status(download_id, "failed", "下载完成但文件未找到")
                return {"status": "failed", "error": "文件未找到"}

            final_size = os.path.getsize(final_path)

            # 如果扩展名不是 mp4，更新 save_path
            if final_path != save_path:
                save_path = final_path
                await db.update_download_save_path(download_id, save_path)

            await db.update_download_progress(download_id, final_size, actual_resolution)
            await db.update_download_status(download_id, "completed")
            if ws_manager:
                await ws_manager.broadcast({
                    "type": "download_progress",
                    "download_id": download_id,
                    "status": "completed",
                    "file_size": final_size,
                })

            # 清理临时 cookie 文件
            if cookies:
                try:
                    cookie_file = os.path.join(save_dir, ".yt-dlp-cookies.txt")
                    os.remove(cookie_file)
                except Exception:
                    pass

            return {"status": "completed", "error": None, "file_size": final_size}

        except Exception as e:
            logger.error(f"[YouTubePlatform] 下载失败 {title}: {e}")
            await db.update_download_status(download_id, "failed", str(e))
            if ws_manager:
                await ws_manager.broadcast({
                    "type": "download_progress",
                    "download_id": download_id,
                    "status": "failed",
                })
            return {"status": "failed", "error": str(e)}
