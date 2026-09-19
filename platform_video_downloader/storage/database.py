import json
import logging
import re

import aiosqlite
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS platform (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    base_url    TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS creator (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_id INTEGER NOT NULL REFERENCES platform(id),
    remote_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    avatar_url  TEXT,
    space_url   TEXT NOT NULL,
    last_sync   DATETIME,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform_id, remote_id)
);

CREATE TABLE IF NOT EXISTS video (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id  INTEGER NOT NULL REFERENCES creator(id),
    remote_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    duration    INTEGER,
    pubdate     DATETIME,
    extra       TEXT,
    section_id  TEXT,
    section_name TEXT,
    tags        TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(creator_id, remote_id)
);

CREATE TABLE IF NOT EXISTS download (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    INTEGER NOT NULL REFERENCES video(id),
    save_path   TEXT NOT NULL,
    resolution  TEXT NOT NULL,
    file_size   INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending',
    error_msg   TEXT,
    started_at  DATETIME,
    finished_at DATETIME,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(video_id, resolution)
);

CREATE TABLE IF NOT EXISTS task (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_id         INTEGER NOT NULL REFERENCES platform(id),
    creator_id          INTEGER,
    space_url           TEXT NOT NULL,
    space_uid           TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    total_videos        INTEGER DEFAULT 0,
    scraped_videos      INTEGER DEFAULT 0,
    downloaded_videos   INTEGER DEFAULT 0,
    total_downloads     INTEGER DEFAULT 0,
    error_message       TEXT,
    cookie_status       TEXT DEFAULT 'valid',
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def _fresh_conn(self):
        """创建全新的数据库连接。"""
        if self._conn:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        logger.info("数据库连接已建立")

    async def _xq(self, sql, params=()):
        """执行 SQL，写操作自动 commit。"""
        cur = await self._conn.execute(sql, params)
        if sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "DROP")):
            await self._conn.commit()
        return cur

    async def init(self):
        await self._fresh_conn()
        await self._conn.executescript(_SCHEMA)
        # Migration: add tags column if missing
        try:
            await self._xq("ALTER TABLE video ADD COLUMN tags TEXT")
        except Exception:
            pass  # column already exists
        # Migration: add audio_url and merge_status to download table
        for col, col_type in [("audio_url", "TEXT"), ("merge_status", "TEXT DEFAULT NULL")]:
            try:
                await self._xq(f"ALTER TABLE download ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # column already exists
        # Migration: add total_size to download table
        try:
            await self._xq("ALTER TABLE download ADD COLUMN total_size INTEGER")

        except Exception:
            pass  # column already exists
        # Migration: add display_name to task table
        try:
            await self._xq("ALTER TABLE task ADD COLUMN display_name TEXT")

        except Exception:
            pass  # column already exists

    async def close(self):
        if self._conn:
            await self._conn.close()

    # --- Platform ---

    async def insert_platform(self, name: str, base_url: str | None = None) -> int:
        cur = await self._xq(
            "INSERT INTO platform (name, base_url) VALUES (?, ?)", (name, base_url)
        )
        return cur.lastrowid

    async def get_all_platforms(self) -> list[dict]:
        cur = await self._xq("SELECT * FROM platform")
        return [dict(r) for r in await cur.fetchall()]

    async def get_platform(self, platform_id: int) -> dict:
        cur = await self._xq(
            "SELECT * FROM platform WHERE id=?", (platform_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_platform_by_name(self, name: str) -> dict:
        cur = await self._xq(
            "SELECT * FROM platform WHERE name=?", (name,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # --- Creator ---

    async def insert_creator(
        self,
        platform_id: int,
        remote_id: str,
        name: str,
        space_url: str,
        avatar_url: str | None = None,
    ) -> int:
        cur = await self._xq(
            "INSERT INTO creator (platform_id, remote_id, name, space_url, avatar_url) "
            "VALUES (?, ?, ?, ?, ?)",
            (platform_id, remote_id, name, space_url, avatar_url),
        )
        return cur.lastrowid

    async def get_creator(self, creator_id: int) -> dict:
        cur = await self._xq(
            "SELECT * FROM creator WHERE id=?", (creator_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_creator_by_remote(self, platform_id: int, remote_id: str) -> dict:
        cur = await self._xq(
            "SELECT * FROM creator WHERE platform_id=? AND remote_id=?",
            (platform_id, remote_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_creator_sync(self, creator_id: int):
        now = datetime.now(timezone.utc).isoformat()
        await self._xq(
            "UPDATE creator SET last_sync=? WHERE id=?", (now, creator_id)
        )

    # --- Video ---

    async def insert_video(
        self,
        creator_id: int,
        remote_id: str,
        title: str,
        duration: int | None = None,
        pubdate: str | None = None,
        extra: str | None = None,
        section_id: str | None = None,
        section_name: str | None = None,
        tags: list[str] | None = None,
    ) -> int:
        tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
        cur = await self._xq(
            "INSERT INTO video "
            "(creator_id, remote_id, title, duration, pubdate, extra, section_id, section_name, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                creator_id,
                remote_id,
                title,
                duration,
                pubdate,
                extra,
                section_id,
                section_name,
                tags_json,
            ),
        )
        return cur.lastrowid

    async def get_video(self, video_id: int) -> dict:
        cur = await self._xq(
            "SELECT * FROM video WHERE id=?", (video_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_video_by_remote(self, creator_id: int, remote_id: str) -> dict:
        cur = await self._xq(
            "SELECT * FROM video WHERE creator_id=? AND remote_id=?",
            (creator_id, remote_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_videos_by_creator(self, creator_id: int) -> list[dict]:
        cur = await self._xq(
            "SELECT * FROM video WHERE creator_id=?", (creator_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_video_tags(self, video_id: int, tags: list[str]):
        tags_json = json.dumps(tags, ensure_ascii=False)
        await self._xq(
            "UPDATE video SET tags=? WHERE id=?", (tags_json, video_id)
        )

    async def get_tags(self) -> list[dict]:
        cur = await self._xq(
            "SELECT v.tags, COUNT(*) as video_count "
            "FROM video v WHERE v.tags IS NOT NULL AND v.tags != '' "
            "GROUP BY v.tags ORDER BY video_count DESC"
        )
        rows = await cur.fetchall()
        tag_counts: dict[str, int] = {}
        for row in rows:
            try:
                for tag in json.loads(row[0]):
                    tag_counts[tag] = tag_counts.get(tag, 0) + row[1]
            except (json.JSONDecodeError, TypeError):
                continue
        return [{"name": tag, "count": count} for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1])]

    # --- Download ---

    async def insert_download(
        self, video_id: int, save_path: str, resolution: str
    ) -> int:
        cur = await self._xq(
            "INSERT OR IGNORE INTO download (video_id, save_path, resolution) "
            "VALUES (?, ?, ?)",
            (video_id, save_path, resolution),
        )
        if cur.rowcount == 0:
            # Record exists (INSERT OR IGNORE 冲突时 rowcount=0；lastrowid 会保留
            # 上一次插入的值而非 0，不能作为冲突判据) — reset to pending if terminal
            cur2 = await self._xq(
                "SELECT status FROM download WHERE video_id=? AND resolution=?",
                (video_id, resolution),
            )
            old_row = await cur2.fetchone()
            await self._xq(
                "UPDATE download SET status='pending', save_path=?, error_msg=NULL, "
                "started_at=NULL, finished_at=NULL, file_size=NULL, total_size=NULL "
                "WHERE video_id=? AND resolution=? AND status IN ('failed', 'skipped')",
                (save_path, video_id, resolution),
            )

            if old_row and old_row[0] in ("failed", "skipped"):
                logger.info(f"重置下载 video_id={video_id} 分辨率={resolution}: {old_row[0]} → pending")
            cur = await self._xq(
                "SELECT id FROM download WHERE video_id=? AND resolution=?",
                (video_id, resolution),
            )
            row = await cur.fetchone()
            return dict(row)["id"]
        return cur.lastrowid

    async def get_download(self, download_id: int) -> dict:
        cur = await self._xq(
            "SELECT * FROM download WHERE id=?", (download_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_download_status(
        self, download_id: int, status: str, error_msg: str | None = None
    ):
        now = datetime.now(timezone.utc).isoformat()
        if status == "downloading":
            await self._xq(
                "UPDATE download SET status=?, started_at=? WHERE id=?",
                (status, now, download_id),
            )
        elif status in ("completed", "skipped", "failed"):
            await self._xq(
                "UPDATE download SET status=?, finished_at=?, error_msg=? WHERE id=?",
                (status, now, error_msg, download_id),
            )
        else:
            await self._xq(
                "UPDATE download SET status=?, error_msg=NULL, started_at=NULL, finished_at=NULL, file_size=NULL WHERE id=?",
                (status, download_id),
            )

    async def update_download_progress(self, download_id: int, file_size: int, resolution: str | None = None):
        if resolution:
            await self._xq(
                "UPDATE download SET file_size=?, resolution=? WHERE id=?",
                (file_size, resolution, download_id),
            )
        else:
            await self._xq(
                "UPDATE download SET file_size=? WHERE id=?",
                (file_size, download_id),
            )

    async def update_download_total_size(self, download_id: int, total_size: int):
        await self._xq(
            "UPDATE download SET total_size=? WHERE id=?", (total_size, download_id)
        )

    async def update_download_save_path(self, download_id: int, save_path: str):
        await self._xq(
            "UPDATE download SET save_path=? WHERE id=?", (save_path, download_id)
        )

    async def get_creator_name_by_video(self, video_id: int) -> str | None:
        cur = await self._xq(
            "SELECT c.name FROM creator c JOIN video v ON v.creator_id = c.id WHERE v.id=?",
            (video_id,),
        )
        row = await cur.fetchone()
        return row["name"] if row else None

    async def count_pending_downloads_by_creator(self, creator_id: int) -> int:
        cur = await self._xq(
            "SELECT COUNT(*) FROM download WHERE video_id IN (SELECT id FROM video WHERE creator_id=?) AND status='pending'",
            (creator_id,),
        )
        row = await cur.fetchone()
        return row[0]

    async def get_all_creators(self) -> list[dict]:
        cur = await self._xq("SELECT * FROM creator")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_existing_downloads(self, creator_id: int) -> set:
        cur = await self._xq(
            "SELECT v.remote_id, d.resolution FROM download d "
            "JOIN video v ON d.video_id = v.id "
            "JOIN creator c ON v.creator_id = c.id "
            "WHERE c.id=? AND d.status IN ('completed', 'skipped', 'downloading', 'pending')",
            (creator_id,),
        )
        rows = await cur.fetchall()
        return {(row[0], row[1]) for row in rows}

    async def get_all_downloads(
        self, status: str | None = None, page: int = 1, page_size: int = 20,
        creator_id: int | None = None, platform_id: int | None = None,
        section_name: str | None = None,
        tags: list[str] | None = None, keyword: str | None = None,
        sort_by: str = "created_at", sort_order: str = "desc",
    ) -> dict:
        conditions = []
        params = []
        if status:
            conditions.append("d.status=?")
            params.append(status)
        if keyword:
            conditions.append("v.title LIKE ?")
            params.append(f"%{keyword}%")
        if platform_id:
            conditions.append("c.platform_id=?")
            params.append(platform_id)
        if creator_id:
            conditions.append("c.id=?")
            params.append(creator_id)
        if section_name:
            conditions.append("v.section_name=?")
            params.append(section_name)
        if tags:
            tag_conds = []
            for tag in tags:
                tag_conds.append("v.tags LIKE ?")
                params.append(f'%"{tag}"%')
            conditions.append(f"({' OR '.join(tag_conds)})")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * page_size
        # Sort mapping
        sort_map = {
            "title": "v.title",
            "duration": "v.duration",
            "resolution": "d.resolution",
            "file_size": "d.file_size",
            "created_at": "d.created_at",
        }
        sort_col = sort_map.get(sort_by, "d.created_at")
        sort_dir = "DESC" if sort_order.lower() == "desc" else "ASC"
        order_by = f"ORDER BY {sort_col} IS NULL, {sort_col} {sort_dir}"
        # Count total
        params_count = params[:]
        cur_count = await self._xq(
            f"SELECT COUNT(*) FROM download d "
            f"JOIN video v ON d.video_id = v.id "
            f"JOIN creator c ON v.creator_id = c.id "
            f"{where}", params_count,
        )
        total = (await cur_count.fetchone())[0]
        # Fetch page
        params.extend([page_size, offset])
        cur = await self._xq(
            f"SELECT d.*, v.title, v.section_name, v.remote_id as bvid, v.tags, v.duration, "
            f"c.name as creator_name, c.id as creator_id, "
            f"p.name as platform_name, p.base_url as platform_url "
            f"FROM download d JOIN video v ON d.video_id = v.id "
            f"JOIN creator c ON v.creator_id = c.id "
            f"JOIN platform p ON c.platform_id = p.id "
            f"{where} {order_by} LIMIT ? OFFSET ?",
            params,
        )
        rows = await cur.fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    async def get_creators_with_stats(self) -> list[dict]:
        cur = await self._xq(
            "SELECT c.id, c.name, c.avatar_url, "
            "COUNT(v.id) as video_count, "
            "COUNT(CASE WHEN d.status='completed' THEN 1 END) as completed, "
            "COUNT(CASE WHEN d.status='failed' THEN 1 END) as failed, "
            "COUNT(CASE WHEN d.status='downloading' OR d.status='pending' THEN 1 END) as active "
            "FROM creator c "
            "LEFT JOIN video v ON v.creator_id = c.id "
            "LEFT JOIN download d ON d.video_id = v.id "
            "GROUP BY c.id ORDER BY c.name"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_sections(self) -> list[dict]:
        cur = await self._xq(
            "SELECT DISTINCT v.section_name, c.name as creator_name, c.id as creator_id "
            "FROM video v JOIN creator c ON v.creator_id = c.id "
            "WHERE v.section_name IS NOT NULL AND v.section_name != '' "
            "ORDER BY v.section_name"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_stats(self) -> dict:
        result = {}
        cur = await self._xq("SELECT COUNT(*) FROM creator")
        result["total_creators"] = (await cur.fetchone())[0]
        cur = await self._xq("SELECT COUNT(*) FROM video")
        result["total_videos"] = (await cur.fetchone())[0]
        for status in ("pending", "downloading", "completed", "skipped", "failed"):
            cur = await self._xq(
                "SELECT COUNT(*) FROM download WHERE status=?", (status,)
            )
            result[status] = (await cur.fetchone())[0]
        # 下载管理总数 = download 表所有记录（等于各状态之和）
        cur = await self._xq("SELECT COUNT(*) FROM download")
        result["total_downloads"] = (await cur.fetchone())[0]
        return result

    # --- Task ---

    async def insert_task(
        self, platform_id: int, space_url: str, space_uid: str | None = None,
    ) -> int:
        cur = await self._xq(
            "INSERT INTO task (platform_id, space_url, space_uid) VALUES (?, ?, ?)",
            (platform_id, space_url, space_uid),
        )
        return cur.lastrowid

    async def get_task(self, task_id: int) -> dict | None:
        cur = await self._xq("SELECT * FROM task WHERE id=?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_all_tasks(
        self, status: str | None = None, page: int = 1, page_size: int = 50,
    ) -> dict:
        conditions = []
        params = []
        if status:
            conditions.append("t.status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * page_size
        params_count = params[:]
        cur_count = await self._xq(
            f"SELECT COUNT(*) FROM task t {where}", params_count,
        )
        total = (await cur_count.fetchone())[0]
        params.extend([page_size, offset])
        cur = await self._xq(
            f"SELECT t.*, c.name as creator_name, c.avatar_url, t.display_name, "
            f"p.name as platform_name, p.base_url as platform_url, "
            f"(SELECT COALESCE(SUM(d.file_size), 0) FROM download d "
            f"JOIN video v ON d.video_id = v.id WHERE v.creator_id = t.creator_id AND d.status = 'completed') as total_file_size "
            f"FROM task t LEFT JOIN creator c ON t.creator_id = c.id "
            f"LEFT JOIN platform p ON t.platform_id = p.id "
            f"{where} ORDER BY t.created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cur.fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    async def update_task_status(
        self, task_id: int, status: str,
        total_videos: int | None = None, scraped_videos: int | None = None,
        downloaded_videos: int | None = None, total_downloads: int | None = None,
        error_message: str | None = None, creator_id: int | None = None,
        display_name: str | None = None,
        cookie_status: str | None = None,
    ):
        sets = ["status=?"]
        params: list = [status]
        now = datetime.now(timezone.utc).isoformat()
        sets.append("updated_at=?")
        params.append(now)
        if total_videos is not None:
            sets.append("total_videos=?")
            params.append(total_videos)
        if scraped_videos is not None:
            sets.append("scraped_videos=?")
            params.append(scraped_videos)
        if downloaded_videos is not None:
            sets.append("downloaded_videos=?")
            params.append(downloaded_videos)
        if total_downloads is not None:
            sets.append("total_downloads=?")
            params.append(total_downloads)
        if error_message is not None:
            sets.append("error_message=?")
            params.append(error_message)
        if creator_id is not None:
            sets.append("creator_id=?")
            params.append(creator_id)
        if cookie_status is not None:
            sets.append("cookie_status=?")
            params.append(cookie_status)
        if display_name is not None:
            sets.append("display_name=?")
            params.append(display_name)
        params.append(task_id)
        await self._xq(
            f"UPDATE task SET {', '.join(sets)} WHERE id=?", params,
        )

    async def get_task_by_url(self, space_url: str) -> dict | None:
        cur = await self._xq("SELECT * FROM task WHERE space_url=?", (space_url,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_task_url(self, task_id: int, space_url: str, space_uid: str | None = None):
        if not space_uid:
            # 尝试从 URL 提取 space_uid（支持多平台）
            from platform_video_downloader.platforms.base import PlatformRegistry
            registry = PlatformRegistry()
            platform = registry.identify(space_url)
            if platform:
                try:
                    parsed = platform.parse_url(space_url)
                    space_uid = parsed["creator_id"]
                except Exception:
                    pass
            else:
                # 回退到 bilibili 硬编码模式
                match = re.search(r"https?://space\.bilibili\.com/(\d+)", space_url)
                space_uid = match.group(1) if match else None
        await self._xq(
            "UPDATE task SET space_url=?, space_uid=?, status='pending', error_message=NULL, "
            "cookie_status='valid', total_videos=0, scraped_videos=0, downloaded_videos=0, total_downloads=0 WHERE id=?",
            (space_url, space_uid, task_id),
        )

    async def cleanup_stale_downloads(self):
        """Clean up downloads stuck in 'downloading' after process kill.
        Check file existence: if file exists → completed, else → failed.
        Never resets to 'pending' to avoid unexpected re-downloads.
        """
        import os
        cur = await self._xq(
            "SELECT id, save_path FROM download WHERE status='downloading'"
        )
        rows = await cur.fetchall()
        completed = 0
        failed = 0
        for row in rows:
            dl_id, save_path = row["id"], row["save_path"]
            if save_path and os.path.exists(save_path):
                await self._xq(
                    "UPDATE download SET status='completed', finished_at=datetime('now'), "
                    "error_msg=NULL WHERE id=?", (dl_id,)
                )
                completed += 1
            else:
                await self._xq(
                    "UPDATE download SET status='failed', error_msg='进程中断，下载未完成', "
                    "started_at=NULL WHERE id=?", (dl_id,)
                )
                failed += 1
        if completed or failed:
            logger.info(f"清理卡死下载: {completed} 个已完成(文件存在), {failed} 个标记失败")
        return completed, failed

    async def check_storage(self, new_output_dir: str) -> dict:
        """Check downloads' file existence against filesystem.

        1. Scan non-completed downloads: if file exists, mark as completed.
        2. Scan completed downloads: verify path validity, fix moved/missing.
        Returns {recovered, moved, missing, total_checked}.
        """
        import os
        new_output_dir = os.path.normpath(new_output_dir)
        recovered = 0
        recovered_task_ids = set()

        # Phase 1: scan non-completed downloads where file exists on disk
        cur = await self._xq(
            "SELECT d.id, d.save_path, v.creator_id FROM download d "
            "JOIN video v ON d.video_id = v.id WHERE d.status != 'completed'"
        )
        for row in await cur.fetchall():
            dl_id, save_path, creator_id = row["id"], row["save_path"], row["creator_id"]
            file_found = False
            actual_path = save_path

            if os.path.exists(save_path):
                file_found = True
            else:
                # Try new output directory
                filename = os.path.basename(save_path)
                new_path = os.path.join(new_output_dir, filename)
                if os.path.exists(new_path):
                    file_found = True
                    actual_path = new_path

            if file_found:
                size = os.path.getsize(actual_path)
                await self._xq(
                    "UPDATE download SET status='completed', save_path=?, file_size=?, "
                    "error_msg=NULL, finished_at=datetime('now') WHERE id=?",
                    (actual_path, size, dl_id),
                )
                recovered += 1
                # Find associated task
                tcur = await self._xq("SELECT id FROM task WHERE creator_id=?", (creator_id,))
                for trow in await tcur.fetchall():
                    recovered_task_ids.add(trow[0])

        # Phase 2: scan completed downloads for path validity
        cur = await self._xq(
            "SELECT id, save_path FROM download WHERE status='completed'"
        )
        rows = await cur.fetchall()
        moved = 0
        missing = 0
        for row in rows:
            dl_id, save_path = row["id"], row["save_path"]
            if os.path.exists(save_path):
                await self._xq(
                    "UPDATE download SET file_size=COALESCE(file_size, ?) WHERE id=? AND file_size IS NULL",
                    (os.path.getsize(save_path), dl_id),
                )
                continue
            filename = os.path.basename(save_path)
            new_path = os.path.join(new_output_dir, filename)
            if os.path.exists(new_path):
                size = os.path.getsize(new_path)
                await self._xq(
                    "UPDATE download SET save_path=?, file_size=COALESCE(file_size, ?) WHERE id=?",
                    (new_path, size, dl_id),
                )
                moved += 1
            else:
                await self._xq(
                    "UPDATE download SET status='pending', error_msg='文件不存在', "
                    "started_at=NULL, finished_at=NULL, file_size=NULL WHERE id=?", (dl_id,)
                )
                missing += 1

        changed = recovered or moved or missing
        if changed:
            parts = []
            if recovered:
                parts.append(f"{recovered} 个恢复为已完成")
            if moved:
                parts.append(f"{moved} 个路径已更新")
            if missing:
                parts.append(f"{missing} 个标记为待下载")
            logger.info(f"存储检测: {', '.join(parts)}")

        return {
            "recovered": recovered,
            "moved": moved,
            "missing": missing,
            "total_checked": len(rows),
        }, recovered_task_ids

    async def reset_task_downloads(self, creator_id: int):
        """Reset non-completed downloads for a creator to pending status (skip completed)."""
        await self._xq(
            "UPDATE download SET status='pending', error_msg=NULL, started_at=NULL, finished_at=NULL, file_size=NULL "
            "WHERE video_id IN (SELECT id FROM video WHERE creator_id=?) AND status != 'completed'",
            (creator_id,),
        )

    async def delete_task(self, task_id: int):
        """Delete a task and cascade delete its downloads, videos, and creator (if no other tasks reference it)."""
        # 获取 creator_id
        task = await self.get_task(task_id)
        cid = task.get("creator_id") if task else None

        if cid:
            # 删除该 creator 下所有下载记录
            await self._xq("DELETE FROM download WHERE video_id IN "
                                     "(SELECT id FROM video WHERE creator_id=?)", (cid,))
            # 删除该 creator 下所有视频
            await self._xq("DELETE FROM video WHERE creator_id=?", (cid,))
            # 删除任务
            await self._xq("DELETE FROM task WHERE id=?", (task_id,))
            # 如果该 creator 不再有其他任务，一并删除 creator 记录
            cur = await self._xq(
                "SELECT COUNT(*) FROM task WHERE creator_id=?", (cid,))
            row = await cur.fetchone()
            if row and row[0] == 0:
                await self._xq("DELETE FROM creator WHERE id=?", (cid,))
        else:
            # 无 creator_id 的任务，直接删除
            await self._xq("DELETE FROM task WHERE id=?", (task_id,))

    async def delete_download(self, download_id: int):
        """Delete a single download record and sync related task status."""
        dl = await self.get_download(download_id)
        if not dl:
            return
        video_id = dl["video_id"]
        await self._xq("DELETE FROM download WHERE id=?", (download_id,))
        # 查找关联的 creator → task，同步任务状态
        cur = await self._xq(
            "SELECT v.creator_id FROM video v WHERE v.id=?", (video_id,))
        row = await cur.fetchone()
        if row:
            cid = row[0]
            tcur = await self._xq(
                "SELECT id FROM task WHERE creator_id=?", (cid,))
            for trow in await tcur.fetchall():
                await self.sync_task_status_from_downloads(trow[0])

    async def remove_paid_video(self, download_id: int):
        """Remove a paid/exclusive video entirely: delete download + video records, sync task."""
        dl = await self.get_download(download_id)
        if not dl:
            return
        video_id = dl["video_id"]
        await self._xq("DELETE FROM download WHERE id=?", (download_id,))
        # Delete video only if no other downloads reference it
        cur = await self._xq("SELECT COUNT(*) FROM download WHERE video_id=?", (video_id,))
        cnt = (await cur.fetchone())[0]
        if cnt == 0:
            await self._xq("DELETE FROM video WHERE id=?", (video_id,))
        # Sync task status
        cur = await self._xq(
            "SELECT v.creator_id FROM video v WHERE v.id=?", (video_id,))
        row = await cur.fetchone()
        if row:
            cid = row[0]
            tcur = await self._xq(
                "SELECT id FROM task WHERE creator_id=?", (cid,))
            for trow in await tcur.fetchall():
                await self.sync_task_status_from_downloads(trow[0])

    async def sync_task_status_from_downloads(self, task_id: int):
        """根据下载状态同步任务状态。"""
        task = await self.get_task(task_id)
        if not task or not task.get("creator_id"):
            return
        cid = task["creator_id"]
        # 统计该 creator 下所有 download 的状态
        cur = await self._xq(
            "SELECT status, COUNT(*) as cnt FROM download d "
            "JOIN video v ON d.video_id = v.id WHERE v.creator_id=? GROUP BY d.status",
            (cid,),
        )
        rows = await cur.fetchall()
        counts = {row[0]: row[1] for row in rows}
        total = sum(counts.values())
        if total == 0:
            return
        downloading = counts.get("downloading", 0)
        completed = counts.get("completed", 0)
        skipped = counts.get("skipped", 0)
        failed = counts.get("failed", 0)
        pending = counts.get("pending", 0)

        # 同步实际视频总数
        cur_v = await self._xq("SELECT COUNT(*) FROM video WHERE creator_id=?", (cid,))
        actual_video_count = (await cur_v.fetchone())[0]

        if downloading > 0:
            new_status = "downloading"
        elif pending > 0:
            new_status = "pending"
        elif failed == total:
            new_status = "failed"
        elif completed + skipped == total:
            new_status = "completed"
        elif completed + skipped + failed == total:
            new_status = "completed"
        else:
            new_status = "pending"

        if new_status != task["status"] or task["total_videos"] != actual_video_count or task["downloaded_videos"] != completed:
            error_msg = None if new_status in ("pending", "downloading") else task.get("error_message")
            await self.update_task_status(
                task_id, new_status,
                total_videos=actual_video_count,
                scraped_videos=actual_video_count,
                downloaded_videos=completed,
                total_downloads=total,
                error_message=error_msg,
            )

    async def clear_all_downloads(self):
        """Delete all downloads, videos, creators, and tasks."""
        await self._xq("DELETE FROM download")
        await self._xq("DELETE FROM video")
        await self._xq("DELETE FROM creator")
        await self._xq("DELETE FROM task")