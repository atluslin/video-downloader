#!/usr/bin/env python3
"""Download videos from web pages: yt-dlp for sites it supports, else sniff the m3u8 with a headless
browser and download/merge it with N_m3u8DL-RE + ffmpeg.

Usage:
  python vdl.py URL [URL ...] [options]
"""
import argparse
import glob
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
PASS_HEADERS = ("referer", "origin", "user-agent", "cookie")
# Big-play buttons of common players: ArtPlayer, DPlayer, video.js, Plyr, xgplayer, Aliplayer, ckplayer, jwplayer
PLAY_SELECTORS = [
    ".art-state", ".art-control-play", ".dplayer-mobile-play", ".dplayer-play-icon",
    ".vjs-big-play-button", ".plyr__control--overlaid", ".xgplayer-start",
    ".prism-big-play-btn", ".ck-play", ".jw-display-icon-display", "button[aria-label*=Play i]",
]
MAIN_MIN_SECONDS = 60  # media playlists shorter than this are likely ads / previews
MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".ts", ".flv", ".mov", ".m4v", ".avi",
              ".m4a", ".aac", ".mp3", ".opus", ".flac", ".wav", ".ogg")
SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".ttml")
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
EXE = ".exe" if os.name == "nt" else ""


def find_tool(explicit, env, exe):
    for c in (explicit, os.environ.get(env), shutil.which(exe), BIN_DIR / (exe + EXE)):
        if c and Path(c).is_file():
            return str(c)
    return None


def with_scheme(proxy):
    return proxy if "://" in proxy else "http://" + proxy


def detect_proxy():
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        if os.environ.get(k):
            return with_scheme(os.environ[k])
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            if not winreg.QueryValueEx(k, "ProxyEnable")[0]:
                return None
            server = winreg.QueryValueEx(k, "ProxyServer")[0]
    except OSError:
        return None
    return parse_proxy_server(server)


def parse_proxy_server(server):
    """Windows ProxyServer value: "host:port", or per-protocol "http=host:port;https=host:port;socks=host:port"."""
    if "=" not in server:
        return with_scheme(server)
    parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
    if parts.get("https") or parts.get("http"):
        return with_scheme(parts.get("https") or parts.get("http"))
    return "socks5://" + parts["socks"] if parts.get("socks") else None


def playwright_proxy(proxy):
    """Playwright wants credentials as separate fields and doesn't know socks5h."""
    if not proxy:
        return None
    u = urlparse(proxy.replace("socks5h://", "socks5://", 1))
    server = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    return {"server": server, **({"username": unquote(u.username), "password": unquote(u.password or "")} if u.username else {})}


def split_args(s):
    """Split a pass-through argument string. POSIX shlex would eat the backslashes of Windows paths."""
    if os.name != "nt":
        return shlex.split(s or "")
    # Windows: backslashes stay literal; "…" groups words, also mid-token (--opt="a b"). '…' groups only
    # at the start of a word, so an apostrophe in a path (C:\Users\O'Brien) stays literal.
    # (shlex's non-POSIX mode only honours quotes at the start of a token.)
    args, cur, quote, quoted = [], [], None, False
    for ch in s or "":
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
        elif ch == '"' or (ch == "'" and not cur and not quoted):
            quote, quoted = ch, True
        elif ch.isspace():
            if cur or quoted:
                args.append("".join(cur))
            cur, quoted = [], False
        else:
            cur.append(ch)
    if quote:  # like shlex: don't silently swallow the rest of the arguments into one
        raise ValueError(f"透传参数里的引号 {quote} 没有闭合: {s}")
    if cur or quoted:
        args.append("".join(cur))
    return args


def normalize_url(url):
    """Accept "www.site.com/..." without a scheme (Playwright refuses it); keep local file paths."""
    if re.match(r"[a-zA-Z][\w+.-]*://", url):  # not just "://" anywhere, e.g. inside ?u=https://...
        return url
    try:
        if Path(url).exists():
            return url
    except (OSError, ValueError):  # e.g. ENAMETOOLONG for a long query string on POSIX
        pass
    return "https://" + url


WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "")
    # leading "-" would be parsed as an option by the downloaders
    s = re.sub(r"\s+", " ", s).strip(" .").lstrip("-").strip()
    # Cap by UTF-8 bytes, not chars: 120 CJK chars = 360 bytes > the 255-byte filename limit on Linux/macOS.
    # 180 leaves room for suffixes the downloaders add (".MUX.mp4", "_2", ".log").
    s = s.encode("utf-8")[:180].decode("utf-8", "ignore").rstrip(" .")
    # "CON.mp4" / "nul.txt.mp4" can't be created on Windows: the part before the first dot counts
    return s + "_" if s.split(".")[0].strip().upper() in WINDOWS_RESERVED else s


def python_cmd():
    """Interpreter command for child Python processes: same isolation as ours (so the child sees the same
    site-packages, incl. user site), and never the cwd on sys.path (PYTHONSAFEPATH, Python 3.11+)."""
    return [sys.executable] + (["-I"] if sys.flags.isolated else [])


CHILD_ENV = {**os.environ, "PYTHONSAFEPATH": "1", "PYTHONIOENCODING": "utf-8"}


def site_of(url):
    return ".".join((urlparse(url).hostname or "").split(".")[-2:])


def user_passes(user_args, flags):
    """Whether pass-through args contain one of these options (as "--opt", "--opt=v" or "--opt:v")."""
    return any(t in flags or t.startswith(tuple(f + sep for f in flags for sep in "=:")) for t in user_args)


def outputs(out, name):
    return [p for p in (out / (name + ext) for ext in MEDIA_EXTS) if p.is_file()]


def playlist_outputs(out, name):
    """Files of an earlier --playlist -n NAME run: NAME_1.mp4, NAME_01.mp4, ..."""
    return [p for p in out.glob(glob.escape(name) + "_*")
            if p.suffix.lower() in MEDIA_EXTS and re.fullmatch(re.escape(name) + r"_\d+", p.stem, re.I)]


def unique_name(out, name, used, playlist=False):
    """Avoid clobbering earlier files in this batch or from previous runs (e.g. same page title)."""
    base, n = name, 1
    while name in used or outputs(out, name) or (playlist and playlist_outputs(out, name)):
        n += 1
        name = f"{base}_{n}"
    used.add(name)
    return name


# ---------------------------------------------------------------- yt-dlp

def ytdlp_extractor(url):
    """Name of the yt-dlp extractor that claims this URL ("Generic" = no dedicated support), or None."""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except ImportError:
        return None
    return next((ie.ie_key() for ie in gen_extractor_classes() if ie.suitable(url)), None)


def ytdlp_multi(a):
    """Several entries may be downloaded: --playlist, or the user's own -I/--playlist-items."""
    return a.playlist or any(
        t in ("-I", "--playlist-items", "--playlist-start", "--playlist-end", "--yes-playlist")
        or t.startswith(("--playlist-items=", "--playlist-start=", "--playlist-end=", "-I"))
        for t in split_args(a.ytdlp_args))


def ytdlp_download(url, name, out, ctx):
    """Returns [(path, duration)] of downloaded files, or [] on failure."""
    a = ctx["args"]
    multi = ytdlp_multi(a)
    out.mkdir(parents=True, exist_ok=True)
    fd, paths_file = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    # Several entries: "_<n>" each so -n NAME doesn't reuse one filename. Without -n, the video id
    # keeps two videos whose titles share the first bytes from colliding (yt-dlp would skip the second
    # as "already downloaded").
    index = "%(playlist_index&_{}|)s" if multi else ""
    cmd = [*python_cmd(), "-m", "yt_dlp", url, "-P", str(out),
           "-o", (name.replace("%", "%%") + index + ".%(ext)s") if name else "%(title).100B [%(id)s].%(ext)s",
           "--merge-output-format", "mp4", "--ffmpeg-location", ctx["ffmpeg"],
           "--proxy", ctx["proxy"] or "",
           "--print-to-file", "after_move:%(duration)s\t%(playlist_count|)s\t%(filepath)s", paths_file]
    if a.referer:
        cmd += ["--referer", a.referer]
    deno = find_tool(None, "DENO", "deno")
    if deno and deno != shutil.which("deno"):  # not the one yt-dlp finds itself (needed for YouTube)
        cmd += ["--js-runtimes", f"deno:{deno}"]
    if not multi:
        # --no-playlist only disambiguates video+list URLs (watch?v=..&list=..); a pure playlist/channel
        # URL (or a generic page with many embeds) would still download every entry.
        cmd += ["--no-playlist", "--playlist-items", "1"]
    if not sys.stdout.isatty():
        cmd.append("--no-progress")
    cmd += split_args(a.ytdlp_args)
    sys.stdout.flush()
    rc = subprocess.run(cmd, env=CHILD_ENV).returncode
    try:
        lines = Path(paths_file).read_text(encoding="utf-8", errors="replace").splitlines()
    finally:
        os.unlink(paths_file)
    results, total = {}, 0
    for line in lines:
        dur, count, path = (line.split("\t", 2) + ["", ""])[:3]
        total = max(total, int(count) if count.isdigit() else 0)
        if path and Path(path).is_file():  # dict: an entry skipped as "already downloaded" repeats a path
            results[Path(path)] = float(dur) if re.fullmatch(r"[\d.]+", dur) else 0
    results = list(results.items())
    if results and total > len(results) and not multi and not ctx.get("hinted"):
        print(f"[!] 这个网址是含 {total} 个视频的列表/合集/多视频帖子，只下了第 1 个；要全部下载加 --playlist")
    if rc != 0:
        print(f"[{'!' if results else 'x'}] yt-dlp 退出码 {rc}" + ("，部分条目失败，见上方 ERROR" if results else ""))
    return results


# ---------------------------------------------------------------- m3u8 sniffing

def parse_playlist(url, text):
    variant_urls, rendition_urls, expect_uri = [], [], False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            expect_uri = True
        elif line.startswith("#EXT-X-MEDIA:"):  # separate audio/subtitle tracks of a master
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                rendition_urls.append(urljoin(url, m.group(1)))
        elif expect_uri and line and not line.startswith("#"):
            variant_urls.append(urljoin(url, line))
            expect_uri = False
    master = bool(variant_urls)
    return {
        "master": master,
        "variants": len(variant_urls),
        "segments": text.count("#EXTINF"),
        "duration": round(sum(float(d) for d in re.findall(r"#EXTINF:\s*([\d.]+)", text)), 2),
        "encrypted": bool(re.search(r"#EXT-X-KEY:METHOD=(?!NONE)", text)),
        "live": not master and "#EXT-X-ENDLIST" not in text,
        "iframes": "#EXT-X-I-FRAMES-ONLY" in text,  # trick-play keyframes, never the video to download
        "variant_urls": variant_urls,
        "rendition_urls": rendition_urls,
    }


def choose(cands):
    """Pick the main video. Rank by effective duration, ties going to master playlists (quality choice).

    A master's duration is that of whichever variant the player loaded; if none was loaded it is unknown
    and ranked as MAIN_MIN_SECONDS, so it beats short ad playlists but not a long media playlist.
    Returns (best, known_duration_or_0).
    """
    # Audio/subtitle renditions of a captured master are never the video (an AAC track can even be a
    # little longer than the video and would win on duration).
    renditions = {u for c in cands if c["master"] for u in c.get("rendition_urls", ())}
    cands = [c for c in cands if not c.get("iframes") and not renditions & set(c.get("aliases", [c["url"]]))]
    durations = {u: c["duration"] for c in cands if not c["master"] for u in c.get("aliases", [c["url"]])}

    def eff(c):
        if not c["master"]:
            return c["duration"]
        known = [durations[v] for v in c["variant_urls"] if v in durations]
        return max(known) if known else None

    best = max(cands, default=None, key=lambda c: (
        MAIN_MIN_SECONDS if eff(c) is None else eff(c), c["master"], c["variants"]))
    return (best, eff(best) or 0) if best else (None, 0)


def looks_main(best, duration):
    # A master counts on its own only while its duration is unknown: ad creatives come as master+variant too.
    return duration >= MAIN_MIN_SECONDS or (best["master"] and not duration)


def try_play(page):
    for frame in page.frames:
        try:
            frame.evaluate("() => document.querySelectorAll('video').forEach(v => {"
                           " v.muted = true; v.play().catch(() => {}); })")
        except Exception:
            pass
        for sel in PLAY_SELECTORS:
            try:
                el = frame.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=2000)
                    break
            except Exception:
                pass


def sniff(url, proxy, headful, wait):
    """Open the page in Chromium and collect every m3u8 playlist it loads."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # not sys.exit: yt-dlp URLs later in the batch can still work
        raise RuntimeError("未安装 Playwright，无法嗅探：先运行 scripts/install_deps.py") from None

    responses, others, cands, state = [], [], [], {"last": 0.0, "read": 0}

    def on_response(r):
        u, ct = r.url, r.headers.get("content-type", "").lower()
        if ".m3u8" in u.lower() or "mpegurl" in ct:
            responses.append(r)
            state["last"] = time.time()
        elif ct.startswith("video/") or re.search(r"\.(mp4|flv|mpd)(\?|$)", u.lower()):
            others.append(u)

    def read_new():
        # Bodies are read here, promptly, rather than at the end: they become unreadable once the page
        # navigates away (e.g. after an ad click).
        while state["read"] < len(responses):
            r = responses[state["read"]]
            state["read"] += 1
            if any(c["url"] == r.url for c in cands):
                continue
            try:
                headers = r.request.all_headers()
                if r.request.resource_type in ("document", "media"):
                    # The page itself is the playlist (extension-less URL): Chromium plays it natively and
                    # r.text() can block for minutes until the page crashes. Refetch it with a timeout
                    # instead (same context: shares cookies and proxy).
                    text = bctx.request.get(r.url, timeout=15000, headers={
                        k: v for k, v in headers.items() if k in PASS_HEADERS and k != "cookie"}).text()
                else:
                    text = r.text()
                text = text.lstrip("\ufeff")
            except Exception:
                continue
            if text.startswith("#EXTM3U"):
                # URLs before redirects too: a master names its variants/renditions by the original URL,
                # while r.url is where a CDN redirected (re-signed) it.
                aliases, req = [r.url], r.request.redirected_from
                while req:
                    aliases.append(req.url)
                    req = req.redirected_from
                cands.append({"url": r.url, "aliases": aliases, **parse_playlist(r.url, text),
                              "headers": {k: v for k, v in headers.items() if k in PASS_HEADERS and v}})

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headful,
            proxy=playwright_proxy(proxy),
            # Without a proxy option Chromium would silently use the OS proxy; keep "no proxy" consistent
            # with yt-dlp and N_m3u8DL-RE.
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"]
                 + ([] if proxy else ["--no-proxy-server"]),
        )
        try:
            bctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 768})
            bctx.on("response", on_response)
            page = bctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                if "net::ERR_" in str(e):  # unreachable (DNS, refused, proxy...): nothing to sniff
                    raise RuntimeError(f"页面打不开: {str(e).splitlines()[0]}  (检查网址或 --proxy)") from None
                print("[!] 页面加载未完成 (超时)，继续嗅探")
            title, site = "", site_of(url)
            start, clicked = time.time(), False
            try:
                while time.time() - start < wait:
                    page.wait_for_timeout(500)
                    read_new()
                    if not title or site_of(page.url) == site:
                        try:  # don't take an ad site's title after a navigation
                            title = page.title() or title
                        except Exception:  # mid-navigation (JS redirect): just retry next tick
                            pass
                    best, duration = choose(cands)
                    # Stop once the main video (not just a pre-roll ad) is found and loading has settled.
                    if best and looks_main(best, duration) and time.time() - state["last"] > 3:
                        break
                    if not clicked and not cands and time.time() - start > min(8, wait / 3):
                        print("[*] 未自动播放，尝试点击播放按钮…")
                        try_play(page)
                        clicked = True
            except Exception as e:
                print(f"[!] 嗅探中断，使用已抓到的结果: {e}")
            read_new()
        finally:
            browser.close()
    return cands, sorted(set(others)), title


def summarize_log(log_path, rc=0):
    data = Path(log_path).read_bytes()
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:  # console code page output, e.g. GBK on Chinese Windows
        raw = data.decode(locale.getpreferredencoding(False), errors="replace")
    lines = re.split(r"[\r\n]+|(?=\d{2}:\d{2}:\d{2}\.\d{3} (?:INFO|WARN|ERROR))", raw)
    # drop progress-bar fragments ("Vid 1920x1080 | 6221 Kbps ----- 3/6 50% ...") glued onto log lines
    lines = [re.sub(r"(?:Vid|Aud|Sub) [^-]*-{5,}.*$", "", l.strip()) for l in lines if l]
    plain = [l for l in lines if l]
    lines = [l for l in lines if re.match(r"\d{2}:\d{2}:\d{2}\.\d{3} ", l)]
    if rc != 0 and not any(" ERROR" in l for l in lines):
        # e.g. command-line errors ("Option '--x' expects a single argument") carry no timestamp, and RE
        # follows them with its whole --help text, so look for the error itself first
        hits = [l for l in plain if re.search(r"(?i)unrecognized|expects|required|invalid|error|exception|错误|失败", l)]
        for l in (hits or plain)[-5:]:
            print("    " + l)
        return
    corrupt = sum("Packet corrupt" in l for l in lines)
    keep = [l for l in lines if "corrupt" not in l]
    errors = [l for l in keep if " ERROR" in l]
    for l in (errors or keep)[-12:]:
        print("    " + l)
    if corrupt:
        print(f"    (ffmpeg 合并时报告 {corrupt} 个 corrupt packet，通常是分片边界残余字节；可加 --decode-check 验证)")


def m3u8_download(m3u8, headers, name, out, ctx):
    """Returns the downloaded file path, or None on failure."""
    if not ctx["re"]:
        print("[x] 找不到 N_m3u8DL-RE：先运行 scripts/install_deps.py，或用 --re 指定 / 设置环境变量 M3U8DL_RE")
        return None
    out.mkdir(parents=True, exist_ok=True)
    user_args = split_args(ctx["args"].re_args)
    sub_only = user_passes(user_args, ("--sub-only",))
    proxy = ctx["proxy"]  # .NET's WebProxy rejects socks5h; its socks5 resolves names remotely anyway
    # Our defaults, each dropped when the user passes that option: RE rejects an option given twice
    # ("expects a single argument but 2 were provided") instead of letting the last one win.
    # --auto-select (best of every track type) would override the user's -sv/-sa/-ss/--sub-only; drop
    # filters (-dv/-da/-ds) keep it, since without any selector RE opens an interactive picker that fails
    # when output isn't a terminal.
    defaults = [
        (("--ffmpeg-binary-path",), ["--ffmpeg-binary-path", ctx["ffmpeg"]]),
        (("--auto-select", "-sv", "-sa", "-ss", "--select-video", "--select-audio", "--select-subtitle",
          "--sub-only"), ["--auto-select"]),
        (("-M", "--mux-after-done"), ["-M", "format=mp4"]),
        (("--thread-count",), ["--thread-count", "16"]),
        (("--download-retry-count",), ["--download-retry-count", "5"]),
        (("--custom-proxy", "--use-system-proxy"),
         ["--custom-proxy", proxy.replace("socks5h://", "socks5://", 1)] if proxy else ["--use-system-proxy", "False"]),
    ]
    cmd = [ctx["re"], m3u8, "--save-dir", str(out), "--save-name", name, "--tmp-dir", str(out / ".tmp"),
           "--disable-update-check", "--no-log"]
    for flags, args in defaults:
        if not user_passes(user_args, flags):
            cmd += args
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += user_args

    log_path = out / f"{name}.log"
    print(f"[*] N_m3u8DL-RE 下载中 → {out / name}.mp4")
    if sys.stdout.isatty():
        rc = subprocess.run(cmd).returncode
    else:
        with open(log_path, "wb") as log:
            rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        summarize_log(log_path, rc)
    try:
        (out / ".tmp").rmdir()  # only if N_m3u8DL-RE left it empty
    except OSError:
        pass
    if rc != 0:
        print(f"[x] N_m3u8DL-RE 退出码 {rc}" + ("" if sys.stdout.isatty() else f"，完整日志: {log_path}"))
        return None
    if log_path.exists():
        log_path.unlink()
    files = outputs(out, name)
    if sub_only:  # e.g. NAME.srt, NAME.zh-Hans.vtt
        files = [p for p in out.glob(glob.escape(name) + "*") if p.suffix.lower() in SUB_EXTS]
    if not files:
        print("[x] 未找到输出文件")
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def sniff_best(url, ctx):
    """Sniff the page; returns (best_candidate, expected_duration, title) or None."""
    a = ctx["args"]
    cands, others, title = sniff(url, ctx["proxy"], a.headful, a.wait)
    best, expected = choose(cands)
    if a.sniff_only:
        print(json.dumps({"title": title, "chosen": best and best["url"], "expected_duration": expected,
                          "m3u8": cands, "other_media": others}, ensure_ascii=False, indent=1))
        return best and (best, expected, title)
    for c in cands:
        tag = "★" if c is best else " "
        kind = f"master({c['variants']}档)" if c["master"] else f"{c['segments']}片/{c['duration']:.0f}s"
        flags = (" 加密" if c["encrypted"] else "") + (" 直播" if c["live"] else "")
        print(f"  {tag} {kind}{flags}  {c['url'][:150]}")
    if not best:
        print("[x] 没抓到 m3u8")
        if others:
            print("    捕获到的其它媒体请求:\n    " + "\n    ".join(others[:10]))
        return None
    if not looks_main(best, expected):
        print(f"[!] 选中的播放列表只有 {expected:.0f}s，可能是广告/预告；不对的话加大 --wait 或用 --sniff-only 检查")
    if best["live"]:
        print("[!] 这是直播流 (无 #EXT-X-ENDLIST)，N_m3u8DL-RE 会持续录制，需手动停止")
    return best, expected, title


# ---------------------------------------------------------------- orchestration

def report(f, expected, ctx):
    sibling = Path(ctx["ffmpeg"]).with_name("ffprobe" + Path(ctx["ffmpeg"]).suffix)
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    dur = None
    if ffprobe:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(f)], capture_output=True, text=True,
                             errors="replace").stdout.strip()
        dur = float(out) if re.fullmatch(r"[\d.]+", out) else None
    msg = f"[+] 完成: {f}  ({f.stat().st_size / 1024 ** 2:.1f} MB"
    if dur:
        msg += f", {time.strftime('%H:%M:%S', time.gmtime(dur))}"
        if expected and abs(dur - expected) / expected > 0.02:
            msg += f", ⚠ 与源时长 {expected:.0f}s 不符"
    print(msg + ")")
    if ctx["args"].decode_check:
        r = subprocess.run([ctx["ffmpeg"], "-v", "error", "-i", str(f), "-f", "null", "-"],
                           capture_output=True, text=True, errors="replace")
        errs = [l for l in r.stderr.splitlines() if l.strip()]
        print(f"[{'+' if not errs else '!'}] 完整解码检查: {len(errs)} 条错误"
              + "".join("\n    " + l for l in errs[:5]))


def process(url, name, ctx):
    """Download one URL. Returns True on success.

    Routing (--engine auto): direct .m3u8 → N_m3u8DL-RE; site with a dedicated yt-dlp extractor →
    yt-dlp, falling back to sniffing if it fails; otherwise sniff the m3u8 → N_m3u8DL-RE, falling back
    to yt-dlp's generic extractor (embeds, plain <video>, mp4) if no m3u8 is found.
    """
    a, out = ctx["args"], ctx["out"]
    ctx["hinted"] = False  # per URL

    def via_ytdlp():
        if not ctx["has_ytdlp"]:
            print("[x] 未安装 yt-dlp：先运行 scripts/install_deps.py")
            return False
        n = name and unique_name(out, safe_name(name) or "video", ctx["used"], ytdlp_multi(a))
        results = ytdlp_download(url, n, out, ctx)
        if not results and n:
            ctx["used"].discard(n)  # nothing written: let the m3u8 fallback reuse the name, not NAME_2
        for f, dur in results:
            report(f, dur, ctx)
        return bool(results)

    def via_m3u8(m3u8, headers, expected, title):
        n = unique_name(out, safe_name(name or title) or time.strftime("video_%Y%m%d_%H%M%S"), ctx["used"])
        f = m3u8_download(m3u8, headers, n, out, ctx)
        if f:  # a subtitle's length isn't comparable with the video's
            report(f, 0 if f.suffix.lower() in SUB_EXTS else expected, ctx)
        return bool(f)

    if ".m3u8" in urlparse(url).path.lower():
        headers = {"referer": a.referer} if a.referer else {}
        if a.sniff_only:  # nothing to sniff
            print(json.dumps({"url": url, "headers": headers}, ensure_ascii=False, indent=1))
            return True
        if a.engine != "ytdlp":
            return via_m3u8(url, headers, 0, Path(urlparse(url).path).stem)

    ie = ytdlp_extractor(url) if a.engine != "sniff" else None
    # A direct media file / DASH manifest: the browser would only "download" it and sniff nothing.
    direct_media = re.search(r"\.(mp4|m4v|webm|mkv|mov|flv|avi|ts|mp3|m4a|aac|ogg|ogv|opus|flac|wav|mpd)$",
                             urlparse(url).path, re.I)
    if not a.sniff_only and (a.engine == "ytdlp" or (ie and ie != "Generic") or (direct_media and ie)):
        print(f"[*] yt-dlp 解析器: {ie or '?'} → 用 yt-dlp 下载")
        # Up-front hint by extractor name; yt-dlp's playlist_count (checked after download) covers the rest,
        # but is unknown for lazily paged lists such as YouTube channel tabs.
        ctx["hinted"] = bool(ie and not ytdlp_multi(a) and re.search(
            r"tab|playlist|channel|user|album|series|collection|list", ie, re.I))
        if ctx["hinted"]:
            print("[!] 这像是播放列表/频道/合集，默认只下第 1 个；要全部下载加 --playlist")
        if via_ytdlp():
            return True
        if a.engine == "ytdlp" or direct_media:  # a media file has no page to sniff
            return False
        print("[!] yt-dlp 失败，改用浏览器嗅探 m3u8 (若是网站改版，可运行 install_deps.py 升级 yt-dlp)")
    elif ie and not a.sniff_only:
        print("[*] yt-dlp 无专用解析器 → 浏览器嗅探 m3u8")

    try:
        found = sniff_best(url, ctx)
    except Exception as e:  # browser missing/unlaunchable or page unreachable: still try yt-dlp generic
        print(f"[x] 嗅探失败: {e}")
        if a.sniff_only:
            return False
        found = None
    if a.sniff_only:
        return bool(found)
    if found:
        return via_m3u8(found[0]["url"], found[0]["headers"], found[1], found[2])
    if a.engine == "auto" and ctx["has_ytdlp"] and ie == "Generic":
        print("[*] 用 yt-dlp 通用解析再试一次 (内嵌视频 / 直链 mp4 等)")
        if via_ytdlp():
            return True
    print("    可试: --wait 60 / --headful 手动点播放 / --sniff-only 查看其它媒体请求")
    return False


def main():
    ap = argparse.ArgumentParser(
        description="下载网页中的视频：yt-dlp 支持的网站用 yt-dlp，其余嗅探 m3u8 用 N_m3u8DL-RE")
    ap.add_argument("urls", nargs="+", help="视频播放页网址，或直接的 .m3u8 地址")
    ap.add_argument("-o", "--out", default="downloads", help="保存目录 (默认 ./downloads)")
    ap.add_argument("-n", "--name", help="文件名 (默认取视频/网页标题；多个网址时自动加序号)")
    ap.add_argument("--engine", choices=("auto", "ytdlp", "sniff"), default="auto",
                    help="auto(默认): 按网站自动选择 | ytdlp: 只用 yt-dlp | sniff: 只用浏览器嗅探 m3u8")
    ap.add_argument("--proxy", default="auto", help="auto(默认, 读系统/环境代理) | none | http://host:port")
    ap.add_argument("--wait", type=int, default=30, help="嗅探最长等待秒数 (默认 30)")
    ap.add_argument("--headful", action="store_true", help="显示浏览器窗口 (需手动点播放/登录/过验证时用)")
    ap.add_argument("--referer", help="直接给 m3u8 地址时附带的 Referer")
    ap.add_argument("--playlist", action="store_true", help="yt-dlp: 网址是播放列表/合集时全部下载 (默认只下单个视频)")
    ap.add_argument("--sniff-only", action="store_true", help="只嗅探并输出候选 m3u8 (JSON)，不下载")
    ap.add_argument("--decode-check", action="store_true", help="下载后完整解码一遍检查损坏")
    ap.add_argument("--ytdlp-args", help='透传给 yt-dlp 的参数，需用等号: --ytdlp-args="-f bv+ba --cookies-from-browser chrome"')
    ap.add_argument("--re-args", help='透传给 N_m3u8DL-RE 的参数，需用等号: --re-args="--custom-range 0-10"')
    ap.add_argument("--re", help="N_m3u8DL-RE 可执行文件路径")
    ap.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    # Before parse_args: -h / usage errors print Chinese text (a cp1252 pipe would raise); sys.exit()
    # messages go to stderr.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    a = ap.parse_args()
    proxy = detect_proxy() if a.proxy == "auto" else (None if a.proxy == "none" else with_scheme(a.proxy))
    print(f"[*] 代理: {proxy or '无'}")
    ffmpeg = find_tool(a.ffmpeg, "FFMPEG", "ffmpeg")
    if not ffmpeg and not a.sniff_only:
        sys.exit("[x] 找不到 ffmpeg：安装后加入 PATH，或用 --ffmpeg 指定 / 设置环境变量 FFMPEG")
    ctx = {"args": a, "proxy": proxy, "ffmpeg": ffmpeg, "re": find_tool(a.re, "M3U8DL_RE", "N_m3u8DL-RE"),
           "has_ytdlp": ytdlp_extractor("https://example.com/") is not None,
           "out": Path(a.out).resolve(), "used": set()}

    failed = []
    a.urls = [u.strip() for u in a.urls if u.strip()]
    if not a.urls:
        sys.exit("[x] 没有有效的网址")
    try:  # fail fast on malformed pass-through args instead of once per URL
        split_args(a.ytdlp_args)
        if user_passes(split_args(a.re_args), ("--save-dir", "--save-name", "--save-pattern", "--tmp-dir")):
            sys.exit("[x] --re-args 里不能用 --save-dir/--save-name/--save-pattern/--tmp-dir"
                     "（脚本要据此找到输出文件），请用 -o / -n")
    except ValueError as e:
        sys.exit(f"[x] {e}")
    for i, url in enumerate(a.urls, 1):
        url = normalize_url(url)
        print(f"\n=== [{i}/{len(a.urls)}] {url}")
        name = a.name and (f"{a.name}_{i:02d}" if len(a.urls) > 1 else a.name)
        try:
            ok = process(url, name, ctx)
        except Exception as e:  # keep going with the rest of the batch
            print(f"[x] {type(e).__name__}: {e}")
            ok = False
        if not ok:
            failed.append(url)

    if len(a.urls) > 1:
        print(f"\n[*] 共 {len(a.urls)} 个，成功 {len(a.urls) - len(failed)}，失败 {len(failed)}")
        for u in failed:
            print("    失败: " + u)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
