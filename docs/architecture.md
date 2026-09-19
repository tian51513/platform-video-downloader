# Platform Video Downloader V4 设计文档

> 版本: 4.0 | 日期: 2026-06-07 | 状态: 已发布

---

## 1. 项目概述

多平台视频批量下载器。用户输入UP主/播放列表地址，程序自动识别平台，管理登录态，通过平台特定策略采集视频列表并异步并发下载到本地。支持B站（Playwright采集 + aiohttp下载）和YouTube（yt-dlp采集 + yt-dlp下载）两大平台，通过统一的平台抽象层实现可扩展架构。

V1 核心功能：Playwright浏览器被动采集、QR扫码登录、aiohttp异步下载、双流合并+ffmpeg、断点续传、限速、SQLite状态追踪、Web仪表盘。
V2 新增：Web任务管理、API补全采集、WebSocket实时推送、视频播放器、多分辨率+标签筛选。
V3 新增：多平台架构（BasePlatform + PlatformRegistry）、YouTube集成（yt-dlp）、统一Cookie管理（Bilibili/YouTube双Tab）、Chrome Cookie导入（browser-cookie3）、系统代理自动检测、下载引擎增强（87008/416错误处理、平台分离下载、僵尸进程检测）。
V4 新增：项目重命名（bilibili_downloader → platform_video_downloader / pvd）、付费视频记录彻底删除、存储检测两阶段恢复、视频标题模糊搜索、UP主筛选动态刷新、扫码登录按钮智能显隐、终端日志降噪。

### 1.1 设计目标

| 目标 | 实现方式 |
|------|----------|
| 零配置登录 | B站自动扫码 + 本地缓存；YouTube支持Chrome Cookie一键导入 |
| 反爬绕过 | Playwright真实浏览器环境，页面JS自动处理WBI签名 |
| 多平台扩展 | BasePlatform抽象 + PlatformRegistry注册中心，URL自动识别平台 |
| 高并发下载 | asyncio + aiohttp + yt-dlp Worker池，信号量控制并发数 |
| 高可靠 | 指数退避重试 + 永久错误识别(87008/62002/-404) + SQLite状态持久化 |
| 标签管理 | B站：采集阶段从 arc/search vlist.tag 零额外请求提取；YouTube：yt-dlp extract_info |
| Web管理 | 浏览器端实时监控 + 任务管理 + 视频播放 + 多平台Cookie管理 + 设置面板 |
| 采集完整 | B站滚动采集 + API补全（WBI签名arc/search主动分页）；YouTube yt-dlp extract_flat |

### 1.2 运行形态

- **CLI模式**: `pvd <URL>` 命令行触发下载（自动识别B站/YouTube URL）
- **Web模式**: `pvd web` 或 `start.bat` 启动监控仪表盘 + 任务管理

---

## 2. 技术架构

### 2.1 两阶段分离架构 + 多平台 + 后台任务服务

```
┌─────────────────────────────────────────────────────┐
│                    CLI Entry Point                   │
│  parse_args → _resolve_cookies → download_command    │
└──────────────────────┬──────────────────────────────┘
                       │
          ┌────────────┴────────────┐
          │  PlatformRegistry       │  ← URL自动识别
          │  identify(url) → 平台    │
          └────────────┬────────────┘
                       │
          ┌────────────┴────────────┐
          │   Cookie 解析           │
          │  CLI > ENV > 缓存 > 扫码  │
          │  YouTube: Chrome导入 > 文件│
          └────────────┬────────────┘
                       │
     ┌─────────────────┼─────────────────┐
     │                 │                 │
┌────┴─────┐   ┌──────┴───────┐  ┌──────┴───────┐
│ Bilibili │   │   YouTube    │  │  Future...   │
│ Platform │   │   Platform   │  │  Platform    │
└────┬─────┘   └──────┬───────┘  └──────────────┘
     │                │
     │ Phase 1: 采集  │
     │ Playwright     │ yt-dlp extract_flat
     │ on_response     │ (run_in_executor)
     │ + API补全(WBI)  │
     │                │
     └────────┬───────┘
              │ 关闭浏览器(仅Bilibili)
     ┌────────┴────────────┐
     │   TaskService       │  ← asyncio.Queue 串行
     │  采集队列+下载调度   │
     │  Cookie检测+QR登录  │
     └────────┬────────────┘
              │
     ┌────────┴────────────┐
     │   Phase 2: 下载     │  ← 平台分离
     │                    │
     │ Bilibili:          │  YouTube:
     │  DownloadManager   │  _download_youtube_videos
     │  (aiohttp Workers) │  (yt-dlp + Semaphore)
     │  exclude_youtube   │
     └────────┬────────────┘
              │
     ┌────────┴────────────┐
     │   SQLite Database   │
     │  platform/creator/  │
     │  video/download/task│
     └─────────────────────┘

┌────────────────────────────────────────────┐
│         Web Dashboard + WebSocket          │
│       FastAPI + Jinja2 + 原生JS          │
│  多平台任务管理 + 下载管理 + 视频播放      │
│  双Tab Cookie管理 + 设置面板              │
│  WebSocket 精确状态推送（无轮询）         │
└────────────────────────────────────────────┘
```

### 2.2 多平台架构

```
┌─────────────────────────────┐
│      BasePlatform (ABC)      │
│  - parse_url(url) → dict   │
│  - scrape(id) → dict       │
│  - download_single(...)     │
│  - needs_cookie() → bool   │
│  - needs_browser() → bool  │
└──────────┬──────────────────┘
           │ implements
    ┌──────┴──────┐
    │             │
┌───┴──────┐  ┌───┴──────────┐
│ Bilibili │  │   YouTube     │
│ Platform │  │   Platform    │
│          │  │              │
│ scrape:  │  │ scrape:      │
│  Playwright│ │  yt-dlp      │
│  on_response│ │ extract_flat │
│ + API补全  │  │              │
│          │  │ download:    │
│ download:│  │  yt-dlp      │
│  aiohttp │  │  best[ext=mp4]│
│ 双流+ffmpeg│ │ + Node.js    │
└──────────┘  └──────────────┘

┌─────────────────────────────┐
│    PlatformRegistry         │
│  - register(platform)      │
│  - get(name) → Platform    │
│  - identify(url) → Platform│
│  - list_platforms()         │
│  - get_by_db_platform_id() │
└─────────────────────────────┘
```

### 2.3 关键设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| on_response vs page.route | 仅 on_response | page.route 的 route.fetch 重复请求被B站拒绝，导致分页丢失 |
| 被动读取 vs 主动拦截 | 被动读取 | 不干扰页面JS行为，WBI签名和分页由页面自行处理 |
| 两阶段 vs 全程浏览器 | 两阶段分离 | 浏览器资源消耗大，下载阶段aiohttp/yt-dlp更高效 |
| 滚动 + API补全 | 双重策略 | 滚动可能漏视频，API主动分页补全确保完整（仅Bilibili） |
| 标签筛选关系 | OR关系 | 多标签筛选时包含任意一个即匹配，更实用 |
| WebSocket vs 轮询 | WebSocket广播 | 精确状态驱动，减少无意义请求，所有状态变化立即刷新 |
| 下载状态广播 | Worker每个状态点广播 | downloading/skipped/failed/completed/merging 全量覆盖 |
| 重置下载策略 | 跳过已完成 | 重置时保留已下载文件，仅补全+重试 |
| 平台抽象 | BasePlatform ABC + Registry | 统一接口，URL自动识别，新平台只需实现3个方法 |
| B站/YouTube下载分离 | exclude_platforms | B站用aiohttp Workers，YouTube用yt-dlp线程池，避免信号量冲突 |
| Chrome Cookie导入 | browser-cookie3 | 处理Chrome v127+ App-Bound Encryption，Chrome运行中也可提取 |
| 87008错误处理 | 直接skip | 付费专属视频无需重试 |
| 416错误处理 | 删临时文件重下 | Range不满足说明流URL过期，需全新下载 |
| db 生命周期 | 命令级 try/finally 收口 | aiosqlite 工作线程非 daemon，任何退出路径不关连接会永久阻塞进程退出（V4修复：报错后挂死） |
| restart.py | subprocess.Popen | os.execv会替换进程导致stdout丢失，Popen让新进程独立运行 |

---

## 3. Cookie与登录设计

### 3.1 B站 Cookie（4级优先级）

```
1. --cookie CLI参数（最高优先级）
2. 环境变量 BILIBILI_SESSDATA / BILIBILI_BILI_JCT
3. 缓存文件 cookies/bilibili_cookies.json
4. 浏览器内QR码扫码登录（最低优先级，触发后自动缓存）
```

### 3.2 YouTube Cookie

```
1. Chrome Cookie一键导入（browser-cookie3，Netscape格式）
   - 自动提取 .youtube.com + .google.com 域名Cookie
   - 支持 Chrome v127+ App-Bound Encryption
   - Chrome 运行中也可提取
2. 手动导入 Netscape 格式 Cookie 文本
3. 缓存文件 cookies/youtube_cookies.txt
4. 无 Cookie 也可以下载（部分视频可能受限）
```

### 3.3 Cookie 文件目录

所有 Cookie 文件统一存储在 `cookies/` 目录：

```
cookies/
├── bilibili_cookies.json    # B站Cookie（JSON格式，Playwright标准）
└── youtube_cookies.txt      # YouTube Cookie（Netscape格式，yt-dlp标准）
```

### 3.4 QR码扫码登录流程（B站）

```
1. 启动headed浏览器（--headed 或需要扫码时）
2. 导航到B站页面 → 点击登录按钮/导航登录页
3. 终端打印提示"请扫描二维码"
4. 每2秒轮询 context.cookies() 检测 SESSDATA
5. 检测到SESSDATA → 登录成功 → 缓存cookie
6. 超时120秒抛出 TimeoutError
```

### 3.5 自动Cookie检测

TaskService 在每次任务执行前按平台检测Cookie有效性：
1. B站：调用 `BilibiliAPI.validate_cookie()` 验证SESSDATA
2. YouTube：检查Cookie文件是否存在（不验证有效性）
3. 检测到Cookie失效 → 广播 `login_required` → 前端弹窗提示
4. 用户点击扫码 → 调用 `POST /api/trigger-login` → 弹出headed浏览器
5. 登录成功 → 广播 `login_success` → 前端关闭弹窗
6. 等待最多120秒

### 3.6 系统代理自动检测

YouTube 下载时可能需要代理。系统通过以下方式自动检测：

- **Windows**: 读取注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings` 中的 ProxyEnable/ProxyServer
- **Unix**: 读取环境变量 `https_proxy` / `HTTPS_PROXY` / `http_proxy` / `HTTP_PROXY`

检测到的代理自动应用于 yt-dlp 的 `proxy` 参数。用户也可在设置面板手动配置 `youtube_proxy`。

---

## 4. 数据采集设计（Phase 1）

### 4.1 B站：on_response 被动读取

```python
async def on_response(response):
    if '/x/space/wbi/acc/info' in url:    # UP主信息
        data = await response.json()
    elif '/x/space/section/index' in url: # 合集信息
        data = await response.json()
    elif '/arc/search' in url and 'mid' in url:  # 视频列表(含标签)
        data = await response.json()
```

### 4.2 B站：滚动加载 + API补全

1. 导航到空间页 → 等3秒初始加载 → 滚动加载分页
2. 滚动结束后，用 aiohttp + WBI签名调用 `arc/search` API 主动分页
3. `fetch_videos_by_api` 按 `ps=30` 分页获取，去重后合并
4. API补全失败不阻塞任务，使用滚动采集数据

### 4.3 YouTube：yt-dlp extract_flat

1. 构造播放列表URL → yt-dlp `extract_flat=True`
2. 同步调用放到 `run_in_executor`（不阻塞事件循环）
3. 返回 entries 列表（id, title, duration, upload_date, tags, url, channel）
4. 无需浏览器、无需Cookie（有Cookie可访问更多视频）

### 4.4 标签提取

- **B站**: arc/search 的 vlist 中每个视频包含 `tag` 字段（逗号分隔字符串），解析为 tags 列表存入 video.tags
- **YouTube**: yt-dlp extract_info 的 entries 中直接包含 `tags` 列表

### 4.5 WBI签名（仅B站）

`bilibili/wbi.py` 实现 B站 WBI 签名鉴权：
- 从 `/x/web-interface/nav` 获取 img_key + sub_key
- 混淆映射表重排 → 截取前32位
- MD5签名生成 w_rid

### 4.6 Node.js 签名解析（仅YouTube）

YouTube 视频下载时 yt-dlp 需要 Node.js 运行时来执行 JS 签名解析：
- 自动搜索 `node` 可执行文件（PATH + 常见安装路径）
- 配置 `js_runtimes` 和 `remote_components` 参数
- 下载进度通过 `progress_hooks` 回调获取

---

## 5. 下载引擎设计（Phase 2）

### 5.1 平台分离下载

TaskService.start_downloads() 将B站和YouTube的 pending 下载分开发送：

```
start_downloads()
    │
    ├─ Bilibili downloads → DownloadManager.run(exclude_platforms={"youtube"})
    │   aiohttp Workers + 信号量
    │
    └─ YouTube downloads → _download_youtube_videos()
        yt-dlp Workers + Semaphore
        顺序执行（B站完成后才开始YouTube）
```

### 5.2 B站：单视频下载流程

```
1. 广播 downloading 状态
2. 解析extra获取cid（缺失则调用 get_video_info）
3. 获取标签（缺失则通过 get_video_info 补充）
4. 检查充电专属视频（is_upower_exclusive）→ skip
5. async with api_semaphore: 获取流URL（retry 3次）
6. 87008/62002/充值 错误 → 直接 skip（永久错误，无需重试）
7. build_filename（含bvid防重名）
8. 双流下载：video流(.video.tmp) + audio流(.audio.tmp)
9. 检查已有临时文件 → Range请求断点续传
10. 416 Range Not Satisfiable → 删除临时文件 → 全新下载
11. 速度限制：chunk写入后计算耗时，不足则sleep补足
12. ffmpeg合并 → 输出最终文件 → 删除临时文件
13. 记录实际分辨率（stream resolution）
14. 广播 completed/failed/skipped 状态
```

### 5.3 YouTube：单视频下载流程

```
1. 广播 downloading 状态
2. 构建文件名（sanitize_filename + video_id防重名）
3. yt-dlp extract_info → 获取实际分辨率
4. yt-dlp download（run_in_executor）
5. 限速（ratelimit参数）
6. 代理（proxy参数）
7. Cookie优先级：手动传入 > 文件 > 无
8. 定期广播进度（每秒轮询 progress_data）
9. 检测文件实际扩展名（mp4/mkv/webm/avi）
10. 记录实际分辨率（extract_info 的 height）
11. 广播 completed/failed 状态
12. 清理临时Cookie文件
```

### 5.4 状态广播覆盖

Worker 在以下状态变化点通过 WebSocket 广播：

| 状态变化 | 广播消息 |
|----------|----------|
| 开始下载 | `{type: "download_progress", status: "downloading"}` |
| 进度更新 | `{type: "download_progress", download_id, file_size, total_size}` |
| 开始合并（B站） | `{type: "download_progress", status: "merging"}` |
| 下载完成 | `{type: "download_progress", status: "completed", file_size}` |
| 跳过/移除 | `{type: "download_progress", status: "removed"}` |
| 失败 | `{type: "download_progress", status: "failed"}` |

### 5.5 断点续传

- 检测 `.video.tmp` / `.audio.tmp` 临时文件
- 获取已有文件大小作为 Range 请求起点
- Content-Range 响应解析总大小
- append 模式写入（existing_size > 0 时用 "ab"）
- **416处理**: Range 请求返回 416 时，删除临时文件，去掉 Range header 全新下载

### 5.6 下载限速

- `speed_limit_bps` 参数传入 Worker（单位：bytes/s）
- **B站**: 每个 chunk 写入后：`expected_time = chunk_size / speed_limit_bps`，若实际耗时 < expected_time，sleep 补足差值
- **YouTube**: yt-dlp 的 `ratelimit` 参数
- 最低限速 100KB/s（config.py 常量保护）

### 5.7 错误处理

| 错误码 | 处理方式 | 说明 |
|--------|----------|------|
| 87008 | 删除记录（永久错误） | 付费专属视频 |
| 62002 | 删除记录（永久错误） | 视频不可用 |
| -404 | 删除记录（永久错误） | 视频已被删除 |
| is_upower_exclusive | 删除记录 | UP主充电专属视频 |
| 416 Range Not Satisfiable | 删临时文件重下 | 流URL过期/变更 |
| -403 | 触发QR登录重试 | Cookie过期 |
| 网络超时 | 指数退避重试(3次) | 临时性错误 |

---

## 6. 任务管理系统

### 6.1 TaskService

后台任务服务，管理多平台采集队列和下载调度：

- `submit_task(space_url)` — 提交任务，自动识别平台（PlatformRegistry.identify），去重
- `_process_queue()` — 串行处理采集队列（asyncio.Queue + asyncio.create_task）
- `_execute_task(task_id)` — 单个任务：平台识别 → Cookie检测 → 平台采集 → 创建下载记录
- `start_downloads()` — 启动下载，分离B站/YouTube：B站用DownloadManager，YouTube用_download_youtube_videos
- `_download_youtube_videos()` — YouTube下载：yt-dlp + Semaphore信号量
- `pause_downloads()` — 通过 cancel_event 中断下载
- `trigger_qr_login()` — 弹出headed浏览器QR登录（仅B站）
- 僵尸进程检测：start_downloads 检查 _download_running 状态与实际任务的同步

### 6.2 任务状态机

```
init → scraping → pending → downloading → completed
                  ↓           ↓
               paused      failed
```

- **init**: 任务已入队，等待Cookie检测
- **scraping**: 正在采集视频列表（含API补全/实时进度广播）
- **pending**: 视频已采集，等待下载
- **downloading**: 下载进行中
- **completed**: 全部视频已下载
- **failed**: 任务执行失败（Cookie过期超时/平台不支持等）
- **paused**: 用户暂停

### 6.3 任务状态自动同步

`sync_task_status_from_downloads(task_id)` 从子下载记录聚合推导任务状态：
- 有 downloading → downloading
- 全部 pending → pending
- 全部 failed → failed
- 全部 completed → completed
- 混合状态 → downloading
- 即使状态不变，total_videos/downloaded_videos 数值变化时也触发更新

### 6.4 重置下载策略

`reset_task_downloads(creator_id)`:
- 仅重置 failed/skipped 状态的下载记录
- **跳过 completed 记录**（保留已下载文件）
- API补全新增的视频自动创建 pending 下载记录

---

## 7. Web仪表盘设计

### 7.1 技术栈

FastAPI + Jinja2 + 原生JS + WebSocket

### 7.2 功能

| 功能 | 实现方式 |
|------|----------|
| 任务管理 | 多行提交/列表/进度/重试/暂停/强制重抓/编辑URL/删除 |
| 任务状态显示 | init/scraping/pending/downloading/completed/paused/failed |
| 文件大小 | 每个任务显示已完成视频的文件总大小（子查询SUM） |
| 下载管理 | 单个/批量下载（复选框+全选）/批量重试失败/状态实时刷新 |
| 视频播放 | 模态播放器（自动播放+动态播放列表+上/下一个+自动连播） |
| 视频跳转 | B站视频链接到bilibili.com，YouTube视频链接到youtube.com |
| 统计卡片 | 6个stat-card（总数/各状态数） |
| 状态Tab | 6个tab（全部/下载中/已完成/等待/已跳过/失败） |
| UP主筛选 | 下拉框，显示 已下载/总数，点击时刷新最新数据 |
| 平台筛选 | 按平台过滤（B站/YouTube） |
| 标签筛选 | OR关系，搜索/全选/反选/清除，点击标签快速筛选 |
| 下载进度 | downloading行高亮 + 进度条 + merging状态 |
| 合并状态 | 紫色标签显示"合并中" |
| 按钮系统 | .btn修饰符（primary/success/warning/danger/ghost），日间/夜间双主题 |
| 设置面板 | 居中布局（下载设置/界面设置/YouTube设置/Cookie管理分组） |
| WebSocket | 精确状态驱动（download_progress/task_status/scrape_progress触发UI刷新，无轮询） |
| 分页 | 下载列表20条/页 + 任务列表50条/页 |
| 排序 | 标题/时长/分辨率/大小，点击表头切换升降序 |
| 目录选择器 | tkinter 原生目录选择器（subprocess方式避免主线程冲突） |
| Cookie管理 | 双Tab（Bilibili/YouTube），B站自动检测+扫码登录+清除，YouTube状态查看+手动导入+Chrome导入+清除 |
| Chrome导入 | browser-cookie3一键提取YouTube Cookie（支持Chrome v127+ ABE） |
| 代理检测 | 系统代理自动检测（Windows注册表/Unix环境变量） |
| 设置保存反馈 | 保存设置时显示成功提示 |
| 采集进度 | 实时显示已采集/总视频数（scrape_progress WebSocket推送） |
| 标题搜索 | 模糊搜索筛选（LIKE匹配，300ms防抖） |
| Cookie状态 | 扫码登录按钮根据Cookie有效性自动显隐 |
| 存储检测 | 扫描文件路径有效性，Phase1恢复非completed但文件存在的下载，Phase2验证completed路径有效性 |

### 7.3 REST API

| 路由 | 方法 | 说明 |
|------|------|------|
| `/ws` | WS | WebSocket实时推送 |
| `/api/stats` | GET | 统计数据 |
| `/api/downloads` | GET | 下载列表（分页+筛选+排序） |
| `/api/downloads/start` | POST | 开始下载 |
| `/api/downloads/pause` | POST | 暂停下载 |
| `/api/downloads/batch-start` | POST | 批量下载选中 |
| `/api/downloads/batch-retry-failed` | POST | 批量重试失败下载 |
| `/api/downloads/{id}` | GET | 下载详情 |
| `/api/downloads/{id}/play` | GET | 视频文件播放（FileResponse） |
| `/api/downloads/{id}/retry` | POST | 重试单个 |
| `/api/downloads/{id}/delete` | DELETE | 删除记录 |
| `/api/downloads/clear` | POST | 清空全部 |
| `/api/storage-check` | POST | 存储路径检测 |
| `/api/tasks` | GET | 任务列表 |
| `/api/tasks/submit` | POST | 提交任务 |
| `/api/tasks/{id}` | GET | 任务详情 |
| `/api/tasks/{id}/retry` | POST | 重试任务 |
| `/api/tasks/{id}/force` | POST | 强制重抓 |
| `/api/tasks/{id}/pause` | POST | 暂停 |
| `/api/tasks/{id}/resume` | POST | 恢复 |
| `/api/tasks/{id}/reset-downloads` | POST | 重置下载 |
| `/api/tasks/{id}/url` | POST | 修改URL |
| `/api/tasks/{id}/delete` | POST | 删除任务 |
| `/api/creators` | GET | 创作者列表 |
| `/api/platforms` | GET | 平台列表 |
| `/api/sections` | GET | 合集列表 |
| `/api/tags` | GET | 标签列表及计数 |
| `/api/settings` | GET/POST | 设置读写 |
| `/api/cookie/status` | GET | B站Cookie状态 |
| `/api/cookie/clear` | POST | 清除B站Cookie |
| `/api/cookie/youtube/status` | GET | YouTube Cookie状态 |
| `/api/cookie/youtube/import` | POST | 手动导入YouTube Cookie |
| `/api/cookie/youtube/import-chrome` | POST | 从Chrome导入YouTube Cookie |
| `/api/cookie/youtube/clear` | POST | 清除YouTube Cookie |
| `/api/detect-proxy` | GET | 系统代理检测 |
| `/api/trigger-login` | POST | 触发B站扫码登录 |
| `/api/pick-directory` | POST | 目录选择器 |
| `/api/directories` | POST | 目录列表 |
| `/close-tab` | GET | 通知浏览器关闭标签页 |

### 7.4 视频播放器

- 点击已完成视频的"播放"按钮 → 打开模态框
- 后端 `GET /api/downloads/{id}/play` 返回 FileResponse（video/mp4）
- 自动根据当前筛选条件（UP主/标签）动态构建播放列表
- 播放器 autoplay 自动播放，ended 事件自动切换下一个
- 播放列表显示当前项高亮，点击列表项可跳转
- ◀◀上一个 / ▶▶下一个 / 关闭按钮，首尾按钮 disabled

---

## 8. 数据模型

### 8.1 表结构

#### platform 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK | |
| name | TEXT | UNIQUE NOT NULL | 平台名称（bilibili/youtube/unknown） |
| base_url | TEXT | | 基础URL |
| created_at | DATETIME | | |

#### creator 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK | |
| platform_id | INTEGER | FK, NOT NULL | |
| remote_id | TEXT | NOT NULL | 平台用户ID（B站mid/YouTube playlist ID） |
| name | TEXT | NOT NULL | 创作者/播放列表名称 |
| avatar_url | TEXT | | 头像URL |
| space_url | TEXT | NOT NULL | 空间/播放列表地址 |
| last_sync | DATETIME | | 最后同步时间 |
| created_at | DATETIME | | |

UNIQUE(platform_id, remote_id)

#### video 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK | |
| creator_id | INTEGER | FK, NOT NULL | |
| remote_id | TEXT | NOT NULL | BV号/YouTube视频ID |
| title | TEXT | NOT NULL | 视频标题 |
| duration | INTEGER | | 时长(秒) |
| pubdate | DATETIME | | 发布时间 |
| extra | TEXT | | JSON: cid, aid / thumbnail, url, channel |
| section_id | TEXT | | 合集ID |
| section_name | TEXT | 合集名称 |
| **tags** | TEXT | | JSON数组: ["可爱","纯欲"] |
| created_at | DATETIME | | |

UNIQUE(creator_id, remote_id)

#### download 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK | |
| video_id | INTEGER | FK, NOT NULL | |
| save_path | TEXT | NOT NULL | 文件路径 |
| resolution | TEXT | NOT NULL | 分辨率（实际分辨率） |
| file_size | INTEGER | | 文件大小 |
| total_size | INTEGER | | 总大小 |
| status | TEXT | DEFAULT 'pending' | 状态 |
| error_msg | TEXT | 失败原因 |
| started_at | DATETIME | 开始时间 |
| finished_at | DATETIME | 完成时间 |
| created_at | DATETIME | |

UNIQUE(video_id, resolution)

#### task 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK | |
| platform_id | INTEGER | FK | |
| creator_id | INTEGER | FK | NULLABLE |
| space_url | TEXT | NOT NULL | 空间/播放列表地址 |
| space_uid | TEXT | | 平台用户ID |
| display_name | TEXT | | 显示名称 |
| status | TEXT | DEFAULT 'pending' | 状态 |
| cookie_status | TEXT | DEFAULT 'valid' | Cookie状态 |
| total_videos | INTEGER | DEFAULT 0 | 视频总数 |
| scraped_videos | INTEGER | DEFAULT 0 | 已采集数 |
| downloaded_videos | INTEGER | DEFAULT 0 | 已下载数 |
| total_downloads | INTEGER | DEFAULT 0 | 下载记录总数 |
| error_message | TEXT | NULL | 错误信息 |
| created_at | DATETIME | | |
| updated_at | DATETIME | | |

### 8.2 状态机

```
download: pending → downloading → merging → completed
                                   ↓
                                failed
         （付费视频直接删除记录，不进入状态机）

task: init → scraping → pending → downloading → completed
                   ↓           ↓
                paused      failed
```

---

## 9. 模块结构

```
platform_video_downloader/
├── config.py          # 配置常量 + load_settings/save_settings（JSON持久化）
│                      # V3: COOKIE_DIR, DEFAULT_YOUTUBE_COOKIE_CACHE_PATH, youtube_proxy
├── main.py            # 程序入口（委托cli.main）
├── browser.py         # PlaywrightBrowser — 浏览器生命周期管理 + Cookie文件I/O
├── platforms/
│   ├── __init__.py    # 包导出
│   ├── base.py        # BasePlatform 抽象类 + PlatformRegistry 注册中心 + create_registry
│   ├── bilibili.py    # BilibiliPlatform — 封装 bilibili/ 模块（Playwright采集 + aiohttp双流下载）
│   └── youtube.py     # YouTubePlatform — yt-dlp extract_flat采集 + yt-dlp下载 + Node.js签名
├── youtube/
│   └── __init__.py    # 便捷导入
├── bilibili/
│   ├── api.py         # BilibiliAPI — 获取视频流URL + get_video_info(含标签) + fetch_videos_by_api(API补全)
│   ├── parser.py      # 纯函数 — 解析API响应（空间信息/视频列表(含tag)/合集/DASH流）
│   ├── scraper.py     # BilibiliScraper — Playwright on_response被动采集UP主数据
│   └── wbi.py         # WBI签名鉴权（img_key/sub_key混排 + md5签名）
├── core/
│   ├── manager.py     # DownloadManager — 队列构建、Worker池调度、并发信号量、取消事件
│   │                  # V3: run() 支持 exclude_platforms 分离B站/YouTube下载
│   ├── worker.py      # download_video — 单视频下载（cid/标签补充 + 双流下载 + ffmpeg合并 + 断点续传 + 限速 + WS广播）
│   │                  # V3: 87008付费专属skip, 416 Range错误自动重下, 实际分辨率记录
│   └── retry.py       # retry_async — 指数退避重试，自动识别永久错误
├── storage/
│   ├── database.py    # Database类 — SQLite异步CRUD（platform/creator/video/download/task五表 + 标签）
│   └── files.py       # 文件命名模板 + 路径解析 + 非法字符过滤（含bvid防重名）
├── cli/
│   └── main.py        # argparse命令解析 + Cookie解析 + QR登录 + 两阶段流程
├── web/
│   ├── app.py          # FastAPI应用工厂 + Jinja2 Environment
│   ├── routes.py       # REST API（35+端点: stats/downloads/tasks/creators/platforms/sections/tags/settings/cookie/视频文件服务/存储检测/代理检测）
│   │                  # V3: YouTube Cookie API, Chrome导入, 代理检测, 批量重试失败, close-tab
│   ├── task_service.py  # TaskService — 多平台后台任务运行（PlatformRegistry识别 → 按平台策略采集+下载）
│   │                  # V3: B站/YouTube分离下载, YouTube Cookie管理, 僵尸进程检测
│   ├── ws_manager.py    # WSManager — WebSocket连接管理与广播
│   └── templates/
│       └── index.html  # 仪表盘（多平台支持+双Tab Cookie+YouTube设置+标签点击筛选+批量重试+设置保存反馈）
├── cookies/            # V3: Cookie文件目录
│   ├── bilibili_cookies.json    # B站Cookie缓存
│   └── youtube_cookies.txt      # YouTube Cookie缓存（Netscape格式）
├── restart.py          # 重启脚本（subprocess.Popen启动新进程）
└── start.bat           # Windows快捷启动脚本
```

---

## 10. 测试覆盖

82个测试（V1→V4），覆盖：

| 模块 | 测试数 | 覆盖内容 |
|------|--------|----------|
| test_browser.py | 12 | Cookie文件I/O、环境变量、解析优先级 |
| test_cli.py | 11 | 参数解析、子命令、默认值、异常路径db关闭 |
| test_database.py | 9 | CRUD、唯一约束、状态更新、聚合查询 |
| test_files.py | 7 | 命名模板、非法字符、路径解析 |
| test_parser.py | 8 | 各类解析、时长转换、流URL选择 |
| test_retry.py | 5 | 退避重试、永久错误、跳过错误 |
| test_worker.py | 2 | 下载成功mock、付费视频删除记录 |
| test_manager.py | 2 | 队列处理、跳过已存在 |

---

## 11. 运行时产物

| 文件 | 说明 |
|------|------|
| platform_video_downloader.db | SQLite数据库 |
| platform_video_downloader_settings.json | 用户设置（Web面板修改后生成） |
| cookies/bilibili_cookies.json | B站Cookie缓存（自动生成） |
| cookies/youtube_cookies.txt | YouTube Cookie缓存（手动导入/Chrome提取） |
| ./downloads/ | 默认下载目录 |
| start.bat | Windows快捷启动脚本 |
| restart.py | 重启脚本（Popen方式，新进程独立运行） |

---

## 12. 依赖变更（V3 新增）

| 依赖 | 用途 |
|------|------|
| yt-dlp | YouTube视频采集(extract_info)和下载 |
| browser-cookie3 (可选) | Chrome Cookie导入（支持Chrome v127+ App-Bound Encryption） |
| Node.js (系统依赖) | YouTube JS签名解析运行时 |

---

## 13. V4 变更（项目重命名 + 增强功能）

### 13.1 项目重命名

| 上下文 | 旧名 | 新名 |
|--------|------|------|
| pip 包名 | `bilibili-downloader` | `platform-video-downloader` |
| Python 包目录 | `bilibili_downloader/` | `platform_video_downloader/` |
| CLI 命令 | `bilibili-dl` | `pvd` |
| 数据库文件 | `bilibili_downloader.db` | `platform_video_downloader.db` |
| 设置文件 | `bilibili_settings.json` | `platform_video_downloader_settings.json` |

未改名内容：`bilibili/` 子包、`youtube/` 子包、BilibiliAPI 等类名、Cookie 文件名。

### 13.2 数据库/设置自动迁移

`config.py` 中 `get_effective_db_path()` 和 `get_effective_settings_path()` 在启动时检测旧文件存在且新文件不存在时自动重命名，确保用户升级后无缝过渡。

### 13.3 付费视频记录彻底删除

付费/充电专属视频（87008、62002、is_upower_exclusive、No video streams available）不再标记为 skipped，而是直接删除 download 和 video 记录。WebSocket 广播 `status: "removed"` 通知前端移除对应行。

### 13.4 存储检测两阶段增强

`check_storage()` 重写为两阶段：
- **Phase 1**：扫描所有非 completed 下载，若文件已存在则恢复为 completed（含卡死的 downloading）
- **Phase 2**：验证 completed 下载的路径有效性，更新路径变化/标记缺失

返回 `(result_dict, recovered_task_ids_set)`，调用方自动刷新相关任务状态。

### 13.5 下载管理增强

- **标题搜索**：`get_all_downloads()` 新增 `keyword` 参数，LIKE 模糊匹配标题
- **UP主筛选动态刷新**：下拉列表 `onfocus` 时调用 `refreshCreatorFilter()` 获取最新视频数量
- **任务状态同步修正**：`sync_task_status_from_downloads()` 即使状态不变，total_videos/downloaded_videos 数值变化时也正确更新

### 13.6 Web UI 增强

- **扫码登录按钮智能显隐**：`checkCookieStatus()` 检测到有效 Cookie 时隐藏 `.btn-warning` 扫码按钮，清除 Cookie 后恢复显示
- **终端日志降噪**：aiosqlite 和 asyncio logger 设为 WARNING 级别
