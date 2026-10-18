#!/usr/bin/env python3
"""Install video-downloader dependencies.

- yt-dlp (pip, always upgraded: site extractors go stale fast)
- N_m3u8DL-RE and deno (YouTube's JS challenges for yt-dlp) into ../bin, unless already on PATH
- Playwright + Chromium
- checks ffmpeg

Usage: python install_deps.py [--force]
Network access goes through the system / environment proxy automatically (urllib.request.getproxies).
"""
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
EXE = ".exe" if os.name == "nt" else ""
OS = "win" if sys.platform == "win32" else "osx" if sys.platform == "darwin" else "linux"
ARM = platform.machine().lower() in ("arm64", "aarch64")


def re_asset(name):  # e.g. N_m3u8DL-RE_v0.6.0-beta_win-x64_20260629.zip
    return f"_{OS}-{'arm64' if ARM else 'x64'}_" in name


def deno_asset(name):  # e.g. deno-x86_64-pc-windows-msvc.zip
    triple = {"win": "pc-windows-msvc", "osx": "apple-darwin", "linux": "unknown-linux-gnu"}[OS]
    return name == f"deno-{'aarch64' if ARM else 'x86_64'}-{triple}.zip"


GITHUB_TOOLS = {"N_m3u8DL-RE": ("nilaoda/N_m3u8DL-RE", re_asset), "deno": ("denoland/deno", deno_asset)}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "video-downloader-skill"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


class StepFailed(Exception):
    pass


def install_github_tool(tool, force):
    repo, matches = GITHUB_TOOLS[tool]
    exe = tool + EXE
    target = BIN_DIR / exe
    existing = target if target.is_file() else shutil.which(tool)
    if existing and not force:
        print(f"[=] {tool} 已存在: {existing}")
        return
    manual = f"请手动下载放到 {BIN_DIR}: https://github.com/{repo}/releases"
    try:
        release = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest"))
        asset = next((a for a in release["assets"] if matches(a["name"])), None)
        if not asset:
            raise StepFailed(f"{tool} {release['tag_name']} 没有适用于本系统的版本，{manual}")
        print(f"[*] 下载 {asset['name']} …")
        data = fetch(asset["browser_download_url"])
    except OSError as e:  # URLError/HTTPError (e.g. GitHub rate limit, no network/proxy)
        raise StepFailed(f"访问 GitHub 失败: {e}\n    检查网络/代理 (HTTPS_PROXY)，或{manual}")
    payload = None
    if asset["name"].endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next((n for n in z.namelist() if Path(n).name == exe), None)
            payload = member and z.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            member = next((m for m in t.getmembers() if Path(m.name).name == exe and m.isfile()), None)
            payload = member and t.extractfile(member).read()
    if not payload:
        raise StepFailed(f"压缩包 {asset['name']} 里没找到 {exe}，{manual}")
    try:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(0o755)
    except OSError as e:  # e.g. the exe is running (Windows locks it) or no write permission
        raise StepFailed(f"写入 {target} 失败: {e}（若正在下载中请等其结束再 --force）")
    print(f"[+] {tool} {release['tag_name']} → {target}")


def pip_install(*specs):
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", *specs],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()[-3:]
        hint = ("\n    这是系统托管的 Python (PEP 668)：请在虚拟环境里运行本 skill "
                "(python -m venv .venv 后用 .venv 的 python)，或 pip 加 --user / --break-system-packages"
                if "externally-managed" in (r.stderr + r.stdout) else "")
        raise StepFailed(f"pip install {' '.join(specs)} 失败:\n    " + "\n    ".join(msg) + hint)


def install_ytdlp():
    if sys.version_info < (3, 10):  # pip would silently pick an old yt-dlp without --js-runtimes etc.
        raise StepFailed(f"yt-dlp 需要 Python 3.10+，当前是 {platform.python_version()}，请升级 Python")
    print("[*] pip install -U yt-dlp …")
    pip_install("yt-dlp[default]")
    out = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True,
                         env={**os.environ, "PYTHONSAFEPATH": "1"})  # don't import from the cwd
    if out.returncode != 0:
        raise StepFailed(f"yt-dlp 已安装但无法运行: {(out.stderr or out.stdout).strip()[-300:]}")
    print(f"[+] yt-dlp {out.stdout.strip()}")


def install_playwright():
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[*] pip install playwright …")
        pip_install("playwright")
    print("[*] playwright install chromium …")
    if subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"]).returncode != 0:
        raise StepFailed("playwright install chromium 失败 (Linux 可能还需: python -m playwright install-deps)")
    print("[+] Playwright Chromium 就绪")


def check_ffmpeg():
    # same lookup as vdl.py: $FFMPEG, PATH, bin/ (ffprobe is expected next to ffmpeg, or on PATH)
    for ff in (os.environ.get("FFMPEG"), shutil.which("ffmpeg"), BIN_DIR / ("ffmpeg" + EXE)):
        if ff and Path(ff).is_file():
            fp = Path(ff).with_name("ffprobe" + Path(ff).suffix)
            if fp.is_file() or shutil.which("ffprobe"):
                print(f"[+] ffmpeg: {ff}")
                return True
    hint = {"win": "winget install Gyan.FFmpeg", "osx": "brew install ffmpeg"}.get(
        OS, "sudo apt install ffmpeg  (或对应发行版的包管理器)")
    print(f"[!] 未在 PATH 中找到 ffmpeg/ffprobe，请安装: {hint}\n"
          f"    或把 ffmpeg/ffprobe 放进 {BIN_DIR}，或运行 vdl.py 时用 --ffmpeg 指定")
    return False


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # sys.exit() messages go to stderr
        stream.reconfigure(encoding="utf-8", errors="replace")
    force = "--force" in sys.argv
    # Each step is independent: one failing (e.g. no deno build for this platform) must not skip the rest.
    steps = [("yt-dlp", install_ytdlp),
             *((t, lambda t=t: install_github_tool(t, force)) for t in GITHUB_TOOLS),
             ("Playwright", install_playwright)]
    failed = 0
    for label, step in steps:
        try:
            step()
        except StepFailed as e:
            print(f"[x] {e}")
            failed += 1
        except Exception as e:  # unexpected (e.g. odd GitHub API JSON): report, keep going
            print(f"[x] {label}: {type(e).__name__}: {e}")
            failed += 1
    if not check_ffmpeg():
        failed += 1
    print("\n[+] 依赖全部就绪" if not failed else f"\n[!] {failed} 项未就绪，见上方 [x]/[!]")
    sys.exit(1 if failed else 0)
