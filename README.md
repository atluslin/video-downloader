# video-downloader

通用视频下载 skill：给一个视频网址，自动拿到真实视频地址并下载合并成 mp4。可用于 Claude Code 及其它支持 Agent Skills 的 AI 工具，也可以直接在命令行使用。

A universal video-downloader skill: yt-dlp for the 1800+ sites it supports; for everything else it sniffs the real m3u8/HLS stream with a headless browser and downloads + merges it with N_m3u8DL-RE.

## 工作方式

```
网址
├─ 直接是 .m3u8 地址 ──────────→ N_m3u8DL-RE
├─ 直链媒体文件（.mp4 等）─────→ yt-dlp
├─ yt-dlp 有专用解析器 ────────→ yt-dlp ──失败──→ 浏览器嗅探 m3u8 → N_m3u8DL-RE
└─ 其它网页 ──────────────────→ 浏览器嗅探 m3u8
                                 ├─ 抓到 → N_m3u8DL-RE
                                 └─ 没抓到 → yt-dlp 通用解析再试一次
```

- **yt-dlp 支持的网站**（YouTube、B 站、抖音、Twitter/X 等）：直接用 yt-dlp。
- **其它网站**：用 Playwright 无头浏览器打开网页，抓取播放器请求的 m3u8，同时记下当时的 Referer/Cookie。
  - 能应对地址由 JS/WASM 动态签名生成、片头广告、分片伪装成图片、AES-128 加密、音视频分离等情况。
  - 会自动挑出正片：排除广告、预告、单独的音轨和 I 帧列表，并优先选最高清晰度。
- **下载完成后**：用 ffprobe 核对时长；需要时可以把整个文件完整解码一遍，确认没有损坏。

## 安装

需要 Python 3.10+ 和 ffmpeg（含 ffprobe）。

```bash
# Claude Code（用户级 skill）
git clone https://github.com/atluslin/video-downloader ~/.claude/skills/video-downloader

# 安装其余依赖：yt-dlp、N_m3u8DL-RE、deno、Playwright Chromium（可重复运行）
python ~/.claude/skills/video-downloader/scripts/install_deps.py
```

其它支持 Agent Skills 的工具，把仓库克隆到该工具的 skills 目录即可。

## 用法

在 AI 工具里直接说“下载这个视频 <网址>”就行。也可以自己运行：

```bash
python scripts/vdl.py "https://example.com/play/123"                 # 单个视频
python scripts/vdl.py URL1 URL2 URL3 -o ./downloads                   # 批量
python scripts/vdl.py "https://www.youtube.com/playlist?list=..." --playlist
python scripts/vdl.py URL --sniff-only                                # 只看抓到的 m3u8，不下载
python scripts/vdl.py URL --headful --wait 120                        # 需要手动登录/点播放的网站
```

完整参数、透传给 yt-dlp / N_m3u8DL-RE 的用法和排错说明见 [SKILL.md](SKILL.md)。

## 说明

- 不支持 DRM 加密内容（Widevine、FairPlay 等）。
- 请只下载你有权下载的内容，并遵守相关网站的服务条款。

## 许可证

[MIT](LICENSE)。依赖的 [yt-dlp](https://github.com/yt-dlp/yt-dlp)、[N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE)、[deno](https://github.com/denoland/deno)、[Playwright](https://github.com/microsoft/playwright-python) 由 `install_deps.py` 在本地安装，不随本仓库分发，各自遵循其原有许可证。
