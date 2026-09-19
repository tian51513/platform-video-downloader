"""下载进度上报接缝：worker 与 WS 广播之间的小接口。

ws_manager 缺失时全部 no-op，让 worker 的下载逻辑可以脱离 WebSocket 测试。

注意：``__call__`` 的 status 为可选（None 时不输出 status 键），用于透传
``_download_stream`` 的遗留进度形状 ``{"file_size": ..., "total_size": ...}``
（无 status 键；前端据此路由到进度条原地更新分支，消息形状不可变更）。
"""


class ProgressReporter:
    def __init__(self, ws_manager, download_id: int):
        self._ws = ws_manager
        self._id = download_id

    async def __call__(self, status: str | None = None, **fields):
        if self._ws is None:
            return
        message: dict = {
            "type": "download_progress",
            "download_id": self._id,
        }
        if status is not None:
            message["status"] = status
        message.update(fields)
        await self._ws.broadcast(message)

    async def downloading(self, **fields):
        await self("downloading", **fields)

    async def merging(self):
        await self("merging")

    async def completed(self, file_size: int):
        await self("completed", file_size=file_size)

    async def failed(self):
        await self("failed")

    async def removed(self):
        await self("removed")


def make_progress(ws_manager, download_id: int) -> ProgressReporter:
    return ProgressReporter(ws_manager, download_id)
