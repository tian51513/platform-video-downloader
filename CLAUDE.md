# Platform Video Downloader - Claude Code 项目上下文

## 项目概述

多平台视频批量下载器（B站 + YouTube）。两阶段架构：Phase 1 用 Playwright/yt-dlp 采集数据，Phase 2 用 aiohttp/yt-dlp 异步下载视频流。无cookie时自动弹出浏览器扫码登录并缓存。CLI + Web 仪表盘双模式，SQLite 本地状态追踪，平台抽象层可扩展。

## 技术栈

- **语言**: Python 3.11+（开发环境 3.12）
- **数据采集**: Playwright (Chromium headless browser) + yt-dlp + browser-cookie3
- **JS运行时**: Node.js（YouTube 签名解析，可选）
- **异步框架**: asyncio + aiohttp
- **Web框架**: FastAPI + Jinja2 + Uvicorn
- **数据库**: SQLite（aiosqlite 异步驱动）
- **CLI**: argparse（入口 `pvd`）
- **构建**: setuptools + pyproject.toml
- **测试**: pytest + pytest-asyncio（asyncio_mode=auto）
- **包管理**: uv（虚拟环境 `.venv/`）

## 架构

两阶段分离架构，浏览器仅在数据采集阶段运行，下载前关闭释放资源：

```
CLI入口 (cli/main.py)
    │
    ├─ Cookie解析 (_resolve_cookies)
    │   优先级: CLI参数 > 环境变量 > 缓存文件 > 扫码登录
    │
    ├─ Phase 1: 数据采集 (Playwright)
    │   PlaywrightBrowser (browser.py)
    │     启动Chromium → 注入Cookie → 导航bilibili.com建立会话
    │   [无Cookie时] _qr_code_login
    │     headed模式 → 点击登录/导航登录页 → 终端提示扫码 →
    │     轮询SESSDATA cookie → 保存到 cookies/bilibili_cookies.json
    │   BilibiliScraper (bilibili/scraper.py)
    │     导航UP主空间页 → on_response被动读取API响应 → 滚动加载 → 去重
    │   关闭浏览器释放资源
    │
    ├─ Phase 2: 下载 (aiohttp)
    │   DownloadManager (core/manager.py)
    │     信号量控制并发 → asyncio.gather调度Worker池
    │   DownloadWorker (core/worker.py)
    │     BilibiliAPI获取流URL(retry_async) → 采集阶段补充cid/标签 → CDN流式下载 → 更新SQLite
    │
    └─ Web仪表盘 (web/)
        FastAPI + Jinja2 + 原生JS
        统计卡片 + 状态Tab + UP主/标签筛选 + 下载进度条 + 设置面板
        REST API: stats/downloads/creators/sections/tags/settings
        3秒自动刷新
        Web任务管理面板（发布/列表/进度/重试）
        Cookie自动检测与QR扫码登录
        双流下载（视频+音频）+ ffmpeg合并
        断点续传（Range请求）
        单连接下载限速（最低100KB/s）
        视频采集API补全（WBI签名arc/search）
        下载列表分页（20条/页）+ 任务列表分页（50条/页）
        WebSocket实时进度推送
        原生目录选择器（tkinter）
        按平台区分cookie文件
        TaskService后台任务运行器（串行采集队列+下载调度）
        WebSocket实时进度推送（/ws端点，消息类型：task_status/scrape_progress/download_progress/login_required/login_success）
        Cookie自动检测+QR扫码登录（headless检测→失效时弹出headed浏览器→120秒超时）
        扫码登录按钮根据Cookie状态自动显隐
        UP主筛选下拉列表点击时刷新最新视频数量
        视频标题模糊搜索筛选（300ms防抖）
```

### 数据采集策略（on_response 被动读取）

B站API对非浏览器流量返回反爬错误（-352, -799, 412）。解决方案：
- 用 Playwright 打开真实浏览器，导航到UP主空间页面
- 页面自身的JS会正确处理WBI签名并发出API请求
- 通过 `page.on('response')` 被动读取所有API响应（不拦截不干扰页面）
- arc/search 的 vlist 包含视频标签（tag字段，逗号分隔）
- 视频列表通过滚动触发分页加载，用去重bvid计数避免重叠误判
- 合集视频通过 section/index 响应获取，自动补充投稿列表中缺失的视频

### Cookie机制（4级优先级）

```
1. CLI参数: --cookie SESSDATA=xxx（最高优先级）
2. 环境变量: BILIBILI_SESSDATA / BILIBILI_BILI_JCT
3. 缓存文件: cookies/bilibili_cookies.json（扫码登录后自动保存）
4. 扫码登录: 自动弹出headed浏览器，用户手机APP扫码（超时120秒）
```

- `--no-cache` 跳过缓存文件，强制重新扫码
- Cookie过期时scraper报-403，提示使用 `--no-cache` 重新登录

## 快捷启动

```bash
start.bat      # 启动Web仪表盘（自动激活venv+打开浏览器）
restart.bat    # 重启服务（自动关闭旧进程+等待端口释放+启动新服务）
```

### 流URL获取与视频标签

- `/x/player/playurl` 端点**无需WBI签名**，aiohttp直接请求即可
- 视频标签来源：采集阶段从 arc/search vlist.tag 字段提取（零额外请求）
- 下载阶段通过 `/x/web-interface/view` API 补充（采集阶段未获取到时）

## 模块结构

```
platform_video_downloader/
├── config.py          # 配置常量 + load_settings/save_settings（JSON持久化）
├── main.py            # 程序入口（委托cli.main）
├── browser.py         # PlaywrightBrowser — 浏览器生命周期管理 + Cookie文件I/O
├── platforms/
│   ├── __init__.py    # 包导出
│   ├── base.py        # BasePlatform 抽象类 + PlatformRegistry 注册中心 + create_registry
│   ├── bilibili.py    # BilibiliPlatform — 封装 bilibili/ 模块（Playwright采集 + aiohttp双流下载）
│   └── youtube.py     # YouTubePlatform — yt-dlp extract_info 采集 + yt-dlp download 下载
├── youtube/
│   └── __init__.py    # 便捷导入
├── bilibili/
│   ├── api.py         # BilibiliAPI — 获取视频流URL + get_video_info(含标签) + fetch_videos_by_api(API补全)
│   ├── parser.py      # 纯函数 — 解析API响应（空间信息/视频列表(含tag)/合集/DASH流）
│   ├── scraper.py     # BilibiliScraper — Playwright on_response被动采集UP主数据
│   └── wbi.py         # WBI签名鉴权（img_key/sub_key混排 + md5签名）
├── core/
│   ├── manager.py     # DownloadManager — 队列构建、Worker池调度、并发信号量、取消事件
│   ├── worker.py      # download_video — 单视频下载（cid/标签补充 + 双流下载 + ffmpeg合并 + 断点续传 + 限速 + WS广播）
│   └── retry.py       # retry_async — 指数退避重试，自动识别永久错误
├── storage/
│   ├── database.py    # Database类 — SQLite异步CRUD（platform/creator/video/download/task五表 + 标签）
│   └── files.py       # 文件命名模板 + 路径解析 + 非法字符过滤（含bvid防重名）
├── cli/
│   └── main.py        # argparse命令解析 + Cookie解析 + QR登录 + 两阶段流程
└── web/
    ├── app.py          # FastAPI应用工厂 + Jinja2 Environment（直接使用，绕过Starlette兼容问题）
    ├── routes.py       # REST API（30+端点: stats/downloads/tasks/creators/sections/tags/settings/cookie/视频文件服务/存储检测）
    ├── task_service.py  # TaskService — 多平台后台任务运行（PlatformRegistry识别 → 按平台策略采集+下载）
    ├── ws_manager.py    # WSManager — WebSocket连接管理与广播
    └── templates/
        └── index.html  # 仪表盘（统计+Tab+筛选+标签多选(OR)+进度条+设置+任务面板+视频播放器）
```

## 数据模型

- **platform** — 视频平台（bilibili、youtube...），支持多平台扩展
- **creator** — 创作者，UNIQUE(platform_id, remote_id)
- **video** — 视频，UNIQUE(creator_id, remote_id)，extra存平台数据JSON，tags存标签JSON数组
- **download** — 下载记录，UNIQUE(video_id, resolution)，状态机: pending → downloading → merging → completed/failed（付费视频直接删除记录）
- **task** — 抓取任务，状态机: init → scraping → pending → downloading → completed/paused/failed

## 关键配置 (config.py)

| 常量 | 默认值 | 说明 |
|------|--------|------|
| MAX_CONCURRENT_DOWNLOADS | 5 | 同时下载视频数 |
| MAX_CONCURRENT_API_REQUESTS | 10 | 同时API请求数 |
| DOWNLOAD_RETRY_COUNT | 3 | 下载重试次数 |
| RETRY_BACKOFF_BASE | 2 | 重试退避基数(秒) |
| REQUEST_TIMEOUT | 30 | 请求超时(秒) |
| MIN_SPEED_LIMIT_KB | 100 | 最低限速 KB/s |
| DEFAULT_SPEED_LIMIT_MB | 0.0 | 默认限速 MB/s（0=不限） |
| DEFAULT_RESOLUTION_PRIORITY | ["720p","480p","1080p","240p"] | 分辨率优先级 |
| DEFAULT_NAME_TEMPLATE | {title}【{creator}-{section}】 | 文件命名模板 |
| DEFAULT_DB_PATH | platform_video_downloader.db | SQLite数据库路径 |
| DEFAULT_COOKIE_CACHE_PATH | cookies/bilibili_cookies.json | B站Cookie缓存文件路径 |
| DEFAULT_YOUTUBE_COOKIE_CACHE_PATH | cookies/youtube_cookies.txt | YouTube Cookie文件路径 |
| DEFAULT_WEB_PORT | 8080 | Web仪表盘端口 |
| SETTINGS_PATH | platform_video_downloader_settings.json | 用户设置持久化文件 |

## Web设置面板

通过 platform_video_downloader_settings.json 持久化用户配置，Web面板可修改：

| 设置项 | 说明 | 默认值 |
|--------|------|--------|
| max_concurrent_downloads | 并发下载数 | 5 |
| max_concurrent_api | API并发数 | 10 |
| resolution_priority | 分辨率优先级 | 720p,480p,1080p,240p |
| name_template | 文件命名模板 | {title}【{creator}-{section}】 |
| output_dir | 输出目录 | ./downloads |
| web_port | Web端口 | 8080 |
| download_speed_limit | 下载限速 MB/s | 0 |

## CLI用法

```bash
pvd <URL...> [options]          # 下载UP主视频
pvd web [--port 8080]           # 启动Web仪表盘
start.bat                              # Windows快捷启动（自动激活venv+打开浏览器）

选项:
  -o, --output DIR       保存目录 (默认: ./downloads)
  -r, --resolution RES   目标分辨率 (默认: 按优先级自动选择)
  -n, --concurrency N    并发下载数 (默认: 5)
  --dry-run               仅分析不下载
  --force                 忽略已下载记录
  --name-template TPL     自定义文件命名模板
  --headed                显示浏览器窗口（调试/验证用）
  --no-cache              不使用缓存的cookie，强制重新登录
  --cookie NAME=VALUE     B站cookie（如 --cookie SESSDATA=xxx，可多次使用）
```

不提供cookie时自动弹出浏览器扫码登录，登录后缓存到 `cookies/bilibili_cookies.json`，后续运行自动读取。
Cookie也可通过环境变量设置：`BILIBILI_SESSDATA`、`BILIBILI_BILI_JCT`。

## 开发指南

### 环境搭建

```bash
# Windows (uv)
uv venv && .venv\Scripts\activate
pip install -e .
playwright install chromium

# 开发测试依赖
pip install pytest pytest-asyncio aioresponses
```

**本机环境注意（Windows/WSL 双侧开发）**:
- venv 必须是 Windows 侧的（`.venv\Scripts\`），start.bat 依赖它；勿在 WSL 里重建 `.venv`（Linux 的 `bin/` 布局会让 start.bat 失效）
- 本机 Windows Python（`F:\hclaw\python`）是精简发行版**无 venv 模块**，改用: `python -m pip install virtualenv && python -m virtualenv .venv`
- playwright 浏览器国内直连约 25KB/s，用 npmmirror 镜像: `set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright`（镜像缺 dbazure 路径的 ffmpeg/winldd，若 404 需从镜像 `builds/` 路径手动下载后本地 http.server 供给）
- 从 WSL 调 Windows 命令: `cmd.exe /c "..."`（cwd 自动映射）；Windows 可经 WSL2 localhost 转发访问 WSL 服务

### 运行测试

```bash
python -m pytest tests/ -v    # 82个测试（Windows venv 下已验证）
```

### 代码规范

- 全部使用async/await，同步代码仅限parse_args和纯函数
- Database类所有方法以async def开头
- 错误处理：网络错误重试，永久错误(code=-404/62002/87008)直接删除记录，日志级别：root=INFO, aiosqlite=WARNING
- 类型标注使用Python 3.10+风格（`str | None`而非`Optional[str]`）
- Web设置持久化到 platform_video_downloader_settings.json，优先级：文件 > config.py默认值
- 所有命令退出路径必须关闭数据库连接（aiosqlite 工作线程非 daemon，连接不关闭会永久阻塞进程退出）
- 付费/充电专属视频直接从数据库删除（不保留记录），避免污染统计数据
- 存储检测两阶段：Phase1 恢复非completed但文件存在的下载，Phase2 验证completed路径有效性

## 当前版本状态 (V4)

### 已完成

**数据采集**
- Playwright浏览器爬取数据（绕过B站反爬检测）
- on_response 被动读取API响应（不拦截不干扰页面）
- QR码扫码登录（自动弹出浏览器，120秒超时）
- Cookie本地缓存（cookies/bilibili_cookies.json，自动读取/保存）
- Cookie 4级优先级解析（CLI > 环境变量 > 缓存 > 扫码）
- `--no-cache` 强制重新登录
- 滚动加载分页视频，去重bvid计数
- 视频标签提取（arc/search vlist.tag，零额外请求）
- 合集信息提取，合集视频自动补充投稿列表
- 人机验证检测，提示--headed模式手动完成
- 视频列表-403错误时提示登录/重新登录
- 多UP主批量下载，单个失败不影响其他

**下载引擎**
- 两阶段架构：浏览器采集 + aiohttp下载
- 异步高并发下载（信号量控制API/下载并发数）
- 指数退避重试 + 永久错误识别
- cid缺失时自动通过API补充
- 标签缺失时通过 get_video_info 补充
- 文件名包含bvid防重名冲突
- 双流下载（视频+音频）+ ffmpeg合并（合并状态实时显示）
- 断点续传（Range请求，临时文件.video.tmp/.audio.tmp）
- 单连接下载限速（最低100KB/s）

**数据存储**
- SQLite状态追踪（platform/creator/video/download/task五表 + tags字段）
- 已存在视频自动更新标签（无需 --force）
- INSERT OR IGNORE + 状态重置（重置时跳过已完成视频）

**Web仪表盘**
- FastAPI + Jinja2 原生JS（无前端构建工具）
- 统计卡片（总数/等待/下载中/已完成/已跳过/失败）
- 状态Tab页切换（全部/下载中/已完成/等待中/已跳过/失败）
- UP主下拉筛选（显示已下载/总数）
- 标签多选筛选（OR关系，支持搜索/全选/反选/清除）
- 下载中视频进度条 + 合并状态显示
- 统一按钮系统（.btn修饰符，日间/夜间双主题）
- 设置居中面板（分组+分隔线：下载设置/界面设置/Cookie管理）
- 设置持久化（platform_video_downloader_settings.json）
- WebSocket实时推送（所有状态变化精确驱动UI刷新，无定时轮询）
- 下载列表分页（20条/页）+ 任务列表分页（50条/页）
- 单个/批量下载（复选框+全选+下载选中按钮+每行下载按钮）
- 视频标题可点击跳转B站页面（新标签页）
- 视频播放器模态框（自动播放+播放列表+上/下一个切换+自动连播）
- 视频文件服务端点（/api/downloads/{id}/play）
- 原生目录选择器（tkinter）
- 30+ REST API端点

**工程**
- 82个单元测试
- start.bat Windows快捷启动
- CLAUDE.md 项目上下文
- 完整架构设计文档

### V2 已完成

**任务管理**
- Web任务面板（多行提交/列表/进度/重试/暂停/强制重抓/编辑URL/删除）
- 任务状态机：init → scraping → pending → downloading → completed/paused/failed
- Cookie自动检测 + 失效时QR扫码登录（headless检测 → headed弹出 → 120秒超时）
- 任务状态自动同步（从子下载记录聚合推导任务状态）
- 按平台区分cookie文件

**采集增强**
- 视频采集API补全（WBI签名arc/search主动分页，解决滚动加载不全问题）
- 重置下载时保留已完成视频，仅补全遗漏 + 重试失败

**下载增强**
- 双流下载（视频+音频）+ ffmpeg合并（合并状态merging实时显示）
- 断点续传（Range请求）
- 单连接下载限速（最低100KB/s）
- 下载取消事件（cooperative cancel，优雅停止）

**Web UI**
- 统一按钮系统（.btn修饰符，日间/夜间双主题适配）
- WebSocket实时推送（所有状态变化精确驱动，无轮询）
- 下载列表分页（20条/页）+ 任务列表分页（50条/页）
- 标签筛选：OR关系 + 搜索 + 全选/反选/清除
- UP主筛选显示已下载/总数进度
- 单个/批量下载（复选框 + 全选 + 下载选中）
- 视频标题可点击跳转B站（新标签页）
- 视频播放器模态框（自动播放 + 动态播放列表 + 上/下一个 + 自动连播）
- 设置居中面板（分组布局 + 分隔线）
- 原生目录选择器（tkinter，subprocess 方式避免主线程冲突，GBK 编码兼容中文路径）
- 30+ REST API端点

**V2.1 增强**
- 下载列表排序（标题/时长/分辨率/大小，点击表头切换升降序）
- 存储检测按钮（扫描文件路径有效性，自动更新新路径/标记缺失为待下载）
- 下载完成时记录实际分辨率（stream resolution 而非请求分辨率）
- 服务启动时自动清理卡死 downloading 记录（文件存在→completed，不存在→failed）
- restart.bat 一键重启（Python 脚本杀端口进程 + 循环等待释放 + 延迟打开浏览器）
- WebSocket 依赖完善（uvicorn[standard] 包含 websockets）

### V2 待做

- 远程访问WebSocket安全
- 批量任务导入/导出

### V3 已完成

**多平台架构**
- 平台抽象层（`platforms/base.py` — BasePlatform 抽象类 + PlatformRegistry 注册中心）
- Bilibili 平台封装（`platforms/bilibili.py` — 调用现有 bilibili/ 模块）
- YouTube 平台实现（`platforms/youtube.py` — yt-dlp extract_info + download）
- URL 自动识别平台（PlatformRegistry.identify 根据 URL 匹配平台）
- TaskService 多平台任务执行（submit_task 自动识别 → 按平台策略采集和下载）
- 不支持的平台提示"开发中"（前端 + 后端双重提示）

**YouTube 增强**
- YouTube Cookie 管理（Netscape 格式，从 Chrome 导入 / 手动粘贴 / 清除）
- browser-cookie3 处理 Chrome v127+ App-Bound Encryption（无需关闭 Chrome）
- Node.js JS 运行时自动检测 + 签名解析脚本自动下载
- YouTube 下载记录实际分辨率（不再固定显示 "best"）
- YouTube 下载独立调度，不经过 BilibiliAPI（避免平台串扰）

**Cookie 管理**
- 统一 Cookie 管理面板（Tab 切换 B站/YouTube）
- Cookie 文件统一存放 `cookies/` 目录
- 系统代理自动检测（Windows 注册表 / 环境变量）
- 设置保存反馈提示

**Bug 修复**
- UP主充电专属视频自动跳过（87008 永久错误）
- 416 Range Not Satisfiable 自动清理过期临时文件重试
- 点击 pending 视频下载按钮正确触发下载
- 卡死 downloading 状态自动检测重置
- 下载总数统计修正（使用 download 表计数）
- B站/YouTube 下载完全分离（排除平台串扰导致的 -400 错误）
- restart.bat 关闭旧终端窗口（`exit` 命令）
- restart.py 使用 subprocess.Popen 替代 os.execv（避免僵尸进程）

### V4 已完成

**项目重命名**
- 包名 `bilibili_downloader/` → `platform_video_downloader/`
- pip 包名 `bilibili-downloader` → `platform-video-downloader`
- CLI 命令 `bilibili-dl` → `pvd`
- 数据库文件自动迁移（旧 .db/.settings 文件启动时自动重命名）

**下载管理增强**
- 视频标题模糊搜索筛选（300ms 防抖，LIKE 匹配）
- UP主筛选下拉列表点击时刷新最新视频数量（onfocus 触发）
- 付费/充电专属视频直接从数据库删除（download + video 记录），避免污染统计
- 存储检测两阶段增强：Phase1 恢复非 completed 但文件已存在的下载，Phase2 验证 completed 路径有效性
- 任务状态同步修正：total_videos/downloaded_videos 在状态不变时也正确更新

**Web UI 增强**
- 扫码登录按钮根据 Cookie 状态自动显隐（有效时隐藏，清除后显示）
- 终端日志降噪：aiosqlite/asyncio 日志级别设为 WARNING

**Bug 修复**
- clearCookie() fetch 调用模式修正
- cli_entry() argv 传参修正
- CLI 报错后进程挂死不退出（browser.start() 抛错时 db 未关闭，aiosqlite 非 daemon 线程阻塞退出；db.close() 收口到 download_command 的 finally）

## Agent skills

### Issue tracker

Local markdown issues under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
