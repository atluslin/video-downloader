---
name: video-downloader
description: 通用视频下载器：给一个视频网址就能下载成 mp4。yt-dlp 支持的网站（YouTube、B站、抖音、Twitter/X 等 1800+）直接用 yt-dlp；不支持的网站用无头浏览器嗅探真实的 m3u8 (HLS) 地址（含 Referer/Cookie，能应对 JS/WASM 动态签名地址、广告片头、伪装图片分片、AES-128 加密），再用 N_m3u8DL-RE + ffmpeg 多线程下载合并。当用户给出视频页网址或 .m3u8 地址，要求“下载视频”“提取视频真实链接”“下载 m3u8”“下载这一集/整部剧/播放列表”时使用。Universal video downloader: yt-dlp for supported sites, otherwise sniffs the real m3u8/HLS stream with a headless browser and downloads + merges it to mp4.
---

# video-downloader

主脚本是 `scripts/vdl.py`（与本文件同目录）。它会根据网址自动选择下载方式，一种失败时自动换另一种。

**首次使用先装依赖**（可重复运行，已装好的会跳过）：
```bash
python "<本skill目录>/scripts/install_deps.py"
```
它会做这些事：安装或升级 yt-dlp；把 N_m3u8DL-RE 和 deno 下载到 `bin/`（deno 是 yt-dlp 解析 YouTube 必需的 JS 运行时）；安装 Playwright 和 Chromium；检查 ffmpeg。
每一项都是独立安装的，某一项失败（比如当前平台没有对应的 deno 版本）不会影响其它项。没装好的项会以 `[x]` 开头逐条列出（缺 ffmpeg 时以 `[!]` 开头提示），最后再报告共有几项未就绪。
如果 vdl.py 提示找不到某个工具，就先运行它。某个网站用 yt-dlp 突然下载失败（通常是网站改版），也再运行一次，把 yt-dlp 升级到最新。
如果提示 “externally-managed”（Debian/Ubuntu/Homebrew 的系统 Python 不允许直接用 pip 装包），先建一个虚拟环境：`python -m venv .venv`，之后用 `.venv` 里的 python 运行这两个脚本。

## 自动选择下载方式（`--engine auto`）

```
网址
├─ 路径以 .m3u8 结尾 ──────────────────→ N_m3u8DL-RE
├─ 直链媒体文件（.mp4/.flv/.mpd 等）───→ yt-dlp（失败即结束）
├─ yt-dlp 有这个网站的专用解析器 ───────→ yt-dlp ──失败──→ 浏览器嗅探 m3u8 → N_m3u8DL-RE
│   (YouTube、BiliBili、Douyin、Twitter …)
└─ 只能用 yt-dlp 的通用解析器 (Generic) → 浏览器嗅探 m3u8
                                          ├─ 抓到 → N_m3u8DL-RE
                                          └─ 没抓到 → yt-dlp 通用解析再试一次（内嵌视频、直链 mp4 等）
```

判断网址属于哪种情况只要不到 1 秒，不需要联网。

**嗅探和挑选正片的细节：**
- Playwright Chromium 打开网页后，收集 URL 含 `.m3u8` 或 Content-Type 为 `mpegurl` 的响应，同时记录当时浏览器发送的 Referer、Origin、User-Agent 和 Cookie。iframe 内嵌的播放器也能抓到。
- 如果约 8 秒内没有自动播放，会尝试点击常见播放器的播放按钮（ArtPlayer、DPlayer、video.js、Plyr、xgplayer、Aliplayer、ckplayer、jwplayer）。
- 候选按“实际时长”排序；时长相同时优先 master 播放列表，由 N_m3u8DL-RE 自动选最高清晰度。
- 短于 60 秒的列表视为广告或预告，嗅探会继续等待正片出现，直到 `--wait` 超时。如果最后选中的仍不足 60 秒，或者是直播流，会给出警告。

**下载完成后的校验**：用 ffprobe 比对成品时长和源时长，偏差超过 2% 时给出警告。加 `--decode-check` 会把整个文件完整解码一遍。

## 用法

```bash
python "<本skill目录>/scripts/vdl.py" URL [URL ...] [选项]
```
请用运行 `install_deps.py` 的同一个 python。不要加 `-I`，否则 `pip --user` 装在用户目录的依赖会找不到。网址不带 `https://` 也可以。

| 选项 | 说明 |
|---|---|
| `-o DIR` | 保存目录，默认是当前目录下的 `./downloads` |
| `-n NAME` | 文件名。不指定时，yt-dlp 下载的视频命名为 `标题 [视频ID]`，嗅探下载的用网页标题。有多个网址时自动加 `_01`、`_02` 序号。指定的名字或网页标题与已有文件重名时，自动加 `_2`，不会覆盖（yt-dlp 默认命名带视频 ID，本身不会重名）。文件名中的非法字符和开头的 `-` 会被去掉；过长的名字会按 180 字节截断 |
| `--engine auto\|ytdlp\|sniff` | 默认 auto 自动选择；`ytdlp` 只用 yt-dlp；`sniff` 只用浏览器嗅探 |
| `--playlist` | 网址是播放列表、频道或合集时全部下载（只对 yt-dlp 有效）。同时指定了 `-n` 时，文件名按序号加 `_1`、`_2`；没指定时按各视频的标题命名。**默认只下第 1 个**，这样误传一个频道网址也不会下载上千个视频；识别出是列表类网址时会提示。只想下其中几个用 `--ytdlp-args="-I 1:5"`，效果和 `--playlist` 一样（指定了 `-n` 时也会按序号命名） |
| `--proxy auto\|none\|URL` | 默认 auto：先读环境变量，再读 Windows 系统代理。浏览器、yt-dlp 和 N_m3u8DL-RE 使用同一个代理 |
| `--wait N` | 嗅探最长等待秒数，默认 30 |
| `--headful` | 显示浏览器窗口，用于需要手动点播放、登录或过验证码的网站，通常要配合加大 `--wait` |
| `--sniff-only` | 只嗅探，输出候选 m3u8 的 JSON（含时长、分片数、是否加密、请求头），不下载 |
| `--decode-check` | 下载完后完整解码检查。103 分钟的 720p 视频约需 20 秒 |
| `--referer URL` | 附带的 Referer，用于直接给 `.m3u8` 或媒体文件地址、而服务器有防盗链的情况（两种下载器都会带上） |
| `--ytdlp-args="…"` | 透传给 yt-dlp 的参数，**必须写等号**，例如 `--ytdlp-args="-f bv*[height<=1080]+ba --cookies-from-browser chrome"` |
| `--re-args="…"` | 透传给 N_m3u8DL-RE 的参数，**必须写等号**，例如 `--re-args="--custom-range 0-10"` |
| `--re PATH` / `--ffmpeg PATH` | 指定工具路径，也可以用环境变量 `M3U8DL_RE`、`FFMPEG`、`DENO` |

常用的透传参数：
- yt-dlp
  - `--cookies-from-browser chrome`：需要登录或会员的视频
  - `-S vcodec:h264,acodec:m4a`：兼容性优先（适合老电视、KTV 设备）
  - `-x --audio-format mp3`：只要音频
- N_m3u8DL-RE
  - `--custom-range 0-10`：只下载前 11 个分片，适合试下载
  - `--ad-keyword REGEX`：按 URL 正则去掉插播的广告分片
  - `-sv res="1280*" -sa best`：指定画质。只要传了 `-sv`、`-sa` 等选择参数，脚本就不会再自动选最高画质，所以音视频分离的源要同时写 `-sa best`，否则不会选音轨

## 使用规范

- **m3u8 地址通常有时效**。很多网站每次请求都会生成新的签名地址，所以每次下载都要重新传播放页网址，不要复用以前抓到的 m3u8。
- **陌生网站先试再下**：先跑 `--sniff-only` 或 `--re-args="--custom-range 0-5"`，确认抓到的是正片、画面正常，再完整下载。
- **批量下载剧集**：如果 yt-dlp 支持该网站的合集或播放列表，直接加 `--playlist`。否则先用 curl 获取播放页 HTML（需要代理时加 `-x`），从选集列表里提取各集链接，再一次性把所有链接传给脚本，每集都会重新嗅探。
- **下载时间长时**：整部片子通常几百 MB 到几 GB，用后台方式运行，完成后汇报文件路径、大小和时长。
- 输出里出现 “corrupt packet” 时，通常是分片边界的残余字节。用 `--decode-check` 检查，结果为 0 错误就不影响播放。

## 排错

| 现象 | 处理 |
|---|---|
| yt-dlp 失败 | 脚本会自动改用浏览器嗅探。如果是网站改版，运行 `install_deps.py` 升级 yt-dlp；需要登录的加 `--ytdlp-args="--cookies-from-browser chrome"` |
| YouTube 报 “No supported JavaScript runtime” | 缺 deno，运行 `install_deps.py` |
| 没抓到 m3u8 | 先加大 `--wait 60`；不行就用 `--headful --wait 120`，让用户在窗口里手动点播放；再看 `--sniff-only` 输出的 `other_media`，网站可能用的是 mp4、flv 或 DASH (mpd) |
| “页面打不开: net::ERR_…” | 网址写错、网站挂了或代理不对，脚本会立刻失败，不会空等。检查网址，或改用 `--proxy` |
| 抓到的是广告 m3u8 | 用 `--sniff-only` 查看所有候选，挑出正片地址后，直接把 `.m3u8` 地址加 `--referer` 传给脚本 |
| 片中有插播广告 | 根据 m3u8 里广告分片的 URL 特征写正则，用 `--re-args="--ad-keyword ..."` 去掉 |
| 下载报 403 | 请求头不够，或者地址已过期，重新嗅探。如果需要登录，用 `--headful` 登录后再抓 |
| 连接被重置或超时 | 检查代理：用 `--proxy http://127.0.0.1:端口` 指定，或者用 `--proxy none` 直连 |
| 嗅探超过 `--wait` 仍不结束 | 极少数反爬网站检测到自动化浏览器后，会用死循环卡住页面，脚本里对页面的调用没法中断。这时按 Ctrl+C 结束，改用 `--headful` 手动操作 |
| 画面花屏或时长不对 | 用 `--decode-check` 定位问题，再检查是否混入了广告分片 |

不支持 Widevine、FairPlay 之类的 DRM 加密内容。

## 依赖

支持 Windows、macOS 和 Linux（x64/arm64）。除 ffmpeg 外，都由 `scripts/install_deps.py` 自动安装：

- Python 3.10+（新版 yt-dlp 的要求），以及 yt-dlp、Playwright + Chromium（通过 pip 安装）
- [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE/releases) 和 [deno](https://github.com/denoland/deno/releases)：查找顺序为命令行参数、环境变量、PATH、本 skill 的 `bin/` 目录
- ffmpeg 和 ffprobe：需要自行安装，查找顺序为 `--ffmpeg` 参数、环境变量 `FFMPEG`、PATH、本 skill 的 `bin/` 目录

请只下载你有权下载的内容。
