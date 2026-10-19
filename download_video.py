#!/usr/bin/env python3
"""交互式视频下载：运行后输入视频网址，或粘贴含网址的分享文字，自动分析并下载。

实际工作交给 scripts/vdl.py：yt-dlp 支持的网站用 yt-dlp，其它网站用浏览器嗅探 m3u8
后用 N_m3u8DL-RE 下载。

用法: python download_video.py [保存目录]      (默认: 当前目录下的 downloads)
"""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# This repo, or the skill installed where Claude Code looks for it (when this file is copied elsewhere)
SKILL = next((p for p in (HERE, HERE / "skills" / "video-downloader",
                          Path.home() / ".claude" / "skills" / "video-downloader")
              if (p / "scripts" / "vdl.py").is_file()), None)
VDL = SKILL and SKILL / "scripts" / "vdl.py"
QUIT = {"q", "quit", "exit", "退出"}


def missing_deps():
    """Same lookups vdl.py uses (env var, PATH, the skill's bin/)."""
    spec = importlib.util.spec_from_file_location("vdl", VDL)
    vdl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vdl)
    missing = [m for m in ("yt_dlp", "playwright") if importlib.util.find_spec(m) is None]
    missing += [exe for env, exe in (("M3U8DL_RE", "N_m3u8DL-RE"), ("DENO", "deno"))
                if not vdl.find_tool(None, env, exe)]
    return missing


def extract_urls(text):
    """URLs from pasted share text ("【标题】 https://b23.tv/xx复制此链接…"), de-duplicated in order.

    A URL ends at whitespace or the first non-ASCII character (share texts glue Chinese right onto it);
    trailing ASCII punctuation is dropped. Without any http(s) URL, words that look like a domain
    ("www.site.com/play/1") are taken instead.
    """
    urls = [u.rstrip(".,;:!?'\")]}>") for u in re.findall(r"https?://[!-~]+", text)]
    if not urls:
        urls = [w for w in text.split() if re.match(r"[\w-]+(\.[\w-]+)+(/|$)", w) and not w.startswith("-")]
    return list(dict.fromkeys(u for u in urls if u))


def main():
    # UTF-8 even when piped (Windows would otherwise use the GBK/ANSI code page and mangle pasted text)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if not SKILL:
        sys.exit("找不到 video-downloader 的 scripts/vdl.py（应与本脚本同目录，或在 ~/.claude/skills/video-downloader）")
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd() / "downloads"

    missing = missing_deps()
    if missing:
        print(f"缺少依赖 {', '.join(missing)}，先自动安装（只需一次）…", flush=True)
        subprocess.run([sys.executable, str(SKILL / "scripts" / "install_deps.py")])
    print(f"视频保存到: {out}")

    while True:
        try:
            text = input("\n请输入视频网址（可直接粘贴分享文字或多个网址；输入 q 退出）：\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in QUIT:
            break
        if not text:  # blank line, e.g. inside a multi-line paste: just ask again
            continue
        urls = extract_urls(text)
        if not urls:
            print("没有识别到网址，请重新输入")
            continue
        print(f"识别到 {len(urls)} 个网址: " + "  ".join(urls), flush=True)  # before the child's output
        try:
            # "--": URLs are never parsed as vdl.py options, even if one starts with "-"
            rc = subprocess.run([sys.executable, str(VDL), "-o", str(out), "--", *urls]).returncode
        except KeyboardInterrupt:
            print("\n已中断本次下载")
            continue
        print("\n[+] 全部下载完成" if rc == 0 else "\n[!] 有网址下载失败，原因见上方 [x] 提示")
    print("退出")


if __name__ == "__main__":
    main()
