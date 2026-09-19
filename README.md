# Platform Video Downloader

多平台视频批量下载器（B站 + YouTube）。Playwright 浏览器采集数据（绕过反爬），aiohttp/yt-dlp 异步高并发下载视频流。自动扫码登录，Web 仪表盘实时监控。支持双流下载、断点续传、限速下载、视频播放等。

## 功能特性

### 多平台支持（V3）
- 平台抽象层（BasePlatform + PlatformRegistry），可扩展
- 自动识别 URL 平台（B站空间页 / YouTube 播放列表）
- YouTube 基于 yt-dlp 采集（extract_flat）+ 下载
- B站/YouTube 下载完全独立，互不干扰
- 按平台区分 Cookie 文件存储（`cookies/` 目录）

### 下载引擎
- 双流下载（视频+音频）+ ffmpeg合并
- 断点续传（Range请求，网络中断后自动恢复）
- 下载限速（可设置MB/s，最低100KB/s）
- 异步高并发下载（API/下载信号量分别控制）
- 分辨率优先级自动选择
- 指数退避重试 + 永久错误自动识别
- UP主充电专属视频自动移除（87008，删除记录而非标记跳过）
- 416 Range 错误自动重试（清理过期 CDN 临时文件）

### 数据采集
- Playwright 浏览器采集，绕过B站反爬检测（-352/-799/412）
- API补全（WBI签名arc/search主动分页，解决滚动加载不全问题）
- 自动扫码登录，Cookie 4级优先级（CLI > 环境变量 > 缓存 > 扫码）
- 视频标签提取，合集分类自动提取
- 文件名含 bvid 防止同名覆盖

### Web仪表盘
- 任务管理面板（多行提交/进度/重试/暂停/强制重抓/编辑URL/删除）
- 下载管理（单个/批量下载，批量重试失败，复选框全选）
- 视频播放器（自动播放 + 动态播放列表 + 上/下一个 + 自动连播）
- 标签筛选（OR关系，搜索/全选/反选/清除，可点击快捷筛选）
- WebSocket实时推送（精确状态驱动，无轮询）
- 统一UI系统（日间/夜间双主题）
- 统一 Cookie 管理（Tab 切换 B站/YouTube，支持从 Chrome 导入）
- 代理设置（手动配置 + 系统代理自动检测）
- 设置保存反馈提示
- 分页显示、目录选择器、设置持久化
- 下载列表排序（标题/时长/分辨率/大小）
- 存储检测（扫描路径有效性，自动恢复+修复）
- YouTube 视频标题链接到 YouTube 页面
- 视频标题模糊搜索（300ms防抖）
- UP主筛选点击时刷新最新视频数量
- 扫码登录按钮根据Cookie状态自动显隐

### 重启管理
- restart.bat 一键重启（自动关闭旧服务 + 关闭旧终端 + 打开新服务）
- 关闭浏览器旧标签页后再关闭服务进程
- 杀掉服务进程及其父终端窗口

## 快速开始

```bash
# 安装
pip install -e .
playwright install chromium

# 首次运行（自动扫码登录）
pvd https://space.bilibili.com/33676449 --dry-run

# 正式下载
pvd https://space.bilibili.com/33676449 -o ./downloads

# YouTube 播放列表下载
pvd https://www.youtube.com/playlist?list=PLxxxxx -o ./downloads

# Web仪表盘（双击 start.bat 或命令行）
pvd web
```

## 安装

**要求:** Python 3.11+

```bash
git clone <repo-url>
cd platform_video_downloader

uv venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

pip install -e .
playwright install chromium
```

> **Python 无 venv 模块时**（部分精简发行版）：
> ```bash
> python -m pip install virtualenv
> python -m virtualenv .venv
> ```

> **国内网络 playwright 浏览器下载慢**（官方 CDN 直连约 25KB/s），使用 npmmirror 镜像：
> ```bash
> # Windows (cmd)
> set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
> # Linux/Mac
> export PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
> playwright install chromium
> ```
> 注：镜像可能缺少部分 `dbazure` 路径的依赖（ffmpeg/winldd）导致安装报 404，可先单独安装主浏览器，或从镜像 `builds/` 路径手动下载缺失文件。

### 依赖说明

- **基础依赖**：通过 `pip install -e .` 自动安装
- **Playwright**：需要手动安装 Chromium 浏览器
- **Node.js**（Windows YouTube 下载）：yt-dlp 需要用于 YouTube 签名解析
- **ffmpeg**：用于合并视频流和音频流（双流合并）
  - 默认通过 `imageio-ffmpeg` 包自动提供（pip 安装即带，无需手动配置）
  - 也可从 [ffmpeg官网](https://ffmpeg.org/download.html) 安装并添加到 PATH（优先使用系统版本）
  - Linux: `sudo apt install ffmpeg`
  - Mac: `brew install ffmpeg`
- **browser-cookie3**（Windows，可选）：YouTube Cookie 从 Chrome 导入

## Cookie机制

### B站

```
CLI参数 (--cookie) → 环境变量 (BILIBILI_SESSDATA) → 缓存文件 → 扫码登录
```

| 方式 | 说明 |
|------|------|
| 自动（默认） | 首次运行弹出浏览器扫码，登录后缓存到 `cookies/bilibili_cookies.json` |
| 手动Cookie | `--cookie SESSDATA=xxx` |
| 环境变量 | `export BILIBILI_SESSDATA=xxx` |
| 强制重新登录 | `--no-cache` 忽略缓存，重新扫码 |

### YouTube

| 方式 | 说明 |
|------|------|
| 从 Chrome 导入 | 设置页 Cookie 面板 → YouTube Tab → 「从 Chrome 导入」（无需关闭 Chrome） |
| 手动粘贴 | 导入 Netscape 格式 cookie 文本（可用 "Get cookies.txt" 扩展导出） |
| Cookie 文件 | `cookies/youtube_cookies.txt`（Netscape 格式，yt-dlp 原生支持） |

YouTube 下载时 cookie 优先级：手动传入 > `youtube_cookies.txt` 文件 > 无 cookie

> **注意**: YouTube 反爬机制较严格，通过代理（如 `127.0.0.1:1092`）+ Cookie 组合使用效果最佳。

## 使用方法

### 下载视频

```bash
pvd https://space.bilibili.com/33676449                          # 最简用法
pvd https://space.bilibili.com/33676449 -o E:\Video               # 指定目录
pvd https://space.bilibili.com/33676449 https://space.bilibili.com/123456  # 多UP主
pvd https://space.bilibili.com/33676449 -r 1080p                     # 指定分辨率
pvd https://space.bilibili.com/33676449 -n 10                         # 10路并发
pvd https://space.bilibili.com/33676449 --dry-run                    # 仅分析
pvd https://space.bilibili.com/33676449 --force                       # 重置已下载
pvd https://space.bilibili.com/33676449 --no-cache                   # 重新扫码
```

### Web仪表盘

```bash
pvd web                # 默认 http://localhost:8080
pvd web --port 9090  # 自定义端口
start.bat                   # Windows 一键启动（自动激活venv+打开浏览器）
restart.bat                 # Windows 一键重启（自动关闭旧进程+终端+启动新服务）
```

功能：任务管理（B站/YouTube）、下载管理、视频播放、状态筛选、Cookie管理、项目设置。

## REST API

| 路由 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 仪表盘页面 |
| `/ws` | WS | WebSocket实时推送 |
| `/api/stats` | GET | 统计数据 |
| `/api/downloads` | GET | 下载列表（分页+筛选） |
| `/api/downloads/start` | POST | 开始下载 |
| `/api/downloads/pause` | POST | 暂停下载 |
| `/api/downloads/batch-start` | POST | 批量下载选中 |
| `/api/downloads/{id}` | GET | 下载详情 |
| `/api/downloads/{id}/play` | GET | 视频文件播放 |
| `/api/downloads/{id}/retry` | POST | 重试单个 |
| `/api/downloads/{id}/delete` | DELETE | 删除记录 |
| `/api/tasks` | GET | 任务列表 |
| `/api/tasks/submit` | POST | 提交任务（自动识别平台） |
| `/api/tasks/{id}/retry` | POST | 重试任务 |
| `/api/tasks/{id}/force` | POST | 强制重抓 |
| `/api/tasks/{id}/pause` | POST | 暂停任务 |
| `/api/tasks/{id}/resume` | POST | 恢复任务 |
| `/api/tasks/{id}/reset-downloads` | POST | 重置下载 |
| `/api/tasks/{id}/url` | POST | 修改URL |
| `/api/tasks/{id}/delete` | POST | 删除任务 |
| `/api/creators` | GET | UP主列表 |
| `/api/tags` | GET | 标签列表 |
| `/api/settings` | GET/POST | 获取/保存设置 |
| `/api/cookie/status` | GET | B站Cookie状态 |
| `/api/cookie/import` | POST | 导入B站Cookie |
| `/api/cookie/clear` | POST | 清除B站Cookie |
| `/api/cookie/youtube/status` | GET | YouTube Cookie状态 |
| `/api/cookie/youtube/import` | POST | 导入YouTube Cookie（Netscape格式） |
| `/api/cookie/youtube/import-chrome` | POST | 从 Chrome 导入 YouTube Cookie |
| `/api/cookie/youtube/clear` | POST | 清除 YouTube Cookie |
| `/api/trigger-login` | POST | 触发B站扫码登录 |
| `/api/detect-proxy` | GET | 自动检测系统代理 |
| `/api/storage-check` | POST | 存储检测（扫描路径有效性） |
| `/api/pick-directory` | POST | 原生目录选择器 |
| `/api/directories` | POST | 列出子目录 |

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `URL...` | UP主空间/播放列表地址 | 必填 |
| `-o, --output` | 保存目录 | `./downloads` |
| `-r, --resolution` | 目标分辨率 | 按优先级自动 |
| `-n, --concurrency` | 并发下载数 | `5` |
| `--dry-run` | 仅分析 | `False` |
| `--force` | 重置已下载 | `False` |
| `--name-template` | 命名模板 | `{title}【{creator}-{section}】` |
| `--headed` | 显示浏览器窗口 | `False` |
| `--no-cache` | 强制重新登录 | `False` |
| `--cookie NAME=VALUE` | B站Cookie | - |
| `web` | 启动仪表盘 | - |
| `--port` | Web端口 | `8080` |

## 文件命名

默认模板: `{title}【{creator}-{section}】_{bvid}.mp4`

```
有合集: 这是什么神仙颜值啊？？？【颜值回忆录-流行】_BV1xx.mp4
无合集: 独立视频【某UP主】_BV1yy.mp4
YouTube: Video Title【Channel】_dQw4w9WgXcQ.mp4
```

可用变量: `{title}`, `{creator}`, `{section}`, `{bvid}`

## 项目结构

```
platform_video_downloader/
├── config.py          # 配置常量 + 设置持久化
├── main.py            # 程序入口
├── browser.py         # Playwright浏览器 + Cookie缓存
├── platforms/         # 多平台抽象层（V3）
│   ├── base.py        # BasePlatform 抽象类 + PlatformRegistry 注册中心
│   ├── bilibili.py    # Bilibili 平台封装
│   └── youtube.py    # YouTube 平台（yt-dlp 集成）
├── bilibili/          # B站交互层
│   ├── api.py         # 流URL获取 + 视频详情 + API补全 + Cookie验证 + 充电专属检测
│   ├── parser.py      # 响应解析
│   ├── scraper.py     # Playwright数据采集
│   └── wbi.py         # WBI签名鉴权
├── core/              # 下载引擎
│   ├── manager.py     # 任务调度 + 取消事件 + 平台过滤
│   ├── worker.py      # 单视频下载（双流+合并+续传+限速+WS广播+416处理）
│   └── retry.py       # 重试策略（含 87008 永久错误）
├── storage/           # 存储层
│   ├── database.py    # SQLite异步CRUD（五表 + 分页 + 任务状态同步）
│   └── files.py       # 文件命名与路径解析
├── cli/               # 命令行
│   └── main.py        # argparse + Cookie + QR登录
└── web/               # Web仪表盘
    ├── app.py          # FastAPI应用 + WebSocket
    ├── routes.py       # REST API（35+端点 + Cookie管理 + 代理检测）
    ├── task_service.py # 后台任务服务（多平台采集+下载调度）
    ├── ws_manager.py   # WebSocket连接管理
    └── templates/
        └── index.html   # 仪表盘（任务+下载+播放+Cookie管理+设置）
cookies/               # Cookie 缓存目录
    ├── bilibili_cookies.json  # B站 Cookie（扫码登录缓存）
    └── youtube_cookies.txt    # YouTube Cookie（Netscape 格式）
start.bat              # Windows一键启动
restart.bat            # Windows一键重启（关闭旧终端）
restart.py             # 重启脚本（端口检测+进程管理+浏览器标签页关闭）
```

## 技术架构

两阶段分离：

1. **数据采集**（Playwright/yt-dlp）：
   - B站: Chromium → 注入Cookie → 导航空间页 → on_response读取API → 滚动加载 → API补全 → 关闭浏览器
   - YouTube: yt-dlp extract_flat → 播放列表视频元数据
2. **视频下载**（aiohttp/yt-dlp）：
   - B站: 获取流URL → 双流下载 → ffmpeg合并 → SQLite更新 → WebSocket广播
   - YouTube: yt-dlp download → 自动选择最优格式 → 记录实际分辨率

任务状态机：`init → scraping → pending → downloading → completed/paused/failed`

## 许可证

MIT
