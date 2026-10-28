#!/usr/bin/env python3
"""SaveVid yt-dlp server — stdlib only, no Flask/Django needed.

What it does:
  * Serves the SaveVid frontend from ./public/index.html
  * GET /api/resolve?url=<video link> runs yt-dlp (metadata only, no file
    download on the server) and returns real title/author/thumbnail and
    real direct download URLs. Nothing is invented: if yt-dlp returns no
    usable format, formats is [] with an honest Bengali message.

Why this exists: Netlify Functions stop after ~10 seconds and Facebook
often gives its crawler page no direct video file at all, so Facebook
downloads fail there. yt-dlp uses Facebook's own video API routes and
returns real fbcdn links (verified 2026-10-28 for the user's link).

Safety / honesty rules built in:
  * Only YouTube, Facebook, Instagram, TikTok domains allowed.
  * Only formats yt-dlp actually returns are offered, with their real
    URLs. Video-only formats are labelled video-only; audio is separate.
  * Direct CDN URLs expire — the frontend must re-resolve if a link
    stops working, never cache them for days.
  * The server never downloads the video file itself, so it uses almost
    no bandwidth; the user's browser downloads straight from the CDN.
  * This tool is for videos the user owns or has permission to download.
    The frontend carries that disclaimer.

Run locally:
    pip install yt-dlp
    python3 server.py            # http://localhost:8000

Deploy (see DEPLOY-BN.md):
    Render:  New Web Service -> this folder -> Build: pip install -r requirements.txt
             Start: python3 server.py   (PORT is set by Render automatically)
    Railway: same start command, or `railway up` with the Dockerfile.
    VPS:     python3 server.py &  (use a process manager, and put it behind
             a reverse proxy if you want HTTPS / a custom domain).
"""

import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
YTDLP = os.environ.get("YTDLP", "yt-dlp")
CACHE_SECONDS = 600  # 10 minutes, like the earlier app

ALLOWED = re.compile(
    r"^(.*\.)?(youtube\.com|youtu\.be|tiktok\.com|vm\.tiktok\.com|"
    r"facebook\.com|fb\.watch|instagram\.com)$",
    re.IGNORECASE,
)

_cache = {}  # url -> (timestamp, result dict)


def platform_of(host):
    h = host.lower()
    if "youtube" in h or "youtu.be" in h:
        return "YouTube"
    if "tiktok" in h:
        return "TikTok"
    if "facebook" in h or "fb.watch" in h:
        return "Facebook"
    if "instagram" in h:
        return "Instagram"
    return None


def fmt_duration(seconds):
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    return f"{s // 60}:{s % 60:02d}"


def clean_title(title, author):
    """Facebook titles often start with '27K views · 3.5K reactions | ... | Page'.
    Strip the views prefix and the trailing page name (kept as author)."""
    if not title:
        return None, author
    parts = [p.strip() for p in title.split(" | ") if p.strip()]
    if len(parts) >= 2 and re.search(r"views|reactions", parts[0], re.I):
        title = " | ".join(parts[1:-1]) if len(parts) > 2 else parts[1]
        if not author:
            author = parts[-1]
    return (title[:300] if title else None), author


def build_formats(info, platform):
    """Pick a short, honest format list from yt-dlp's real formats."""
    formats = []
    seen = set()

    def add(label, url, note):
        if not url or url in seen:
            return
        seen.add(url)
        formats.append({"label": label, "url": url, "note": note})

    raw = [f for f in info.get("formats") or [] if f.get("url")]
    by_id = {f.get("format_id"): f for f in raw}

    # 1) Facebook legacy sd/hd (progressive MP4) first — these are the
    #    ones users can download and play directly.
    for fid, label in (("hd", "MP4 — HD"), ("sd", "MP4 — SD")):
        f = by_id.get(fid)
        if f:
            add(label, f["url"], "Facebook-এর আসল লিংক। এই লিংকে সময়সীমা থাকতে পারে — দেরি করলে লিংকটি আবার দিন।")

    # 2) Combined (audio+video) formats FIRST — these are the ones a user
    #    can tap once and get a playable video with sound (TikTok, Facebook
    #    progressive, and any platform that offers a combined MP4).
    #    Instagram's original MP4s carry no codec labels at all (vcodec and
    #    acodec both None) but are combined playable files — treat them so.
    for f in raw:
        if (f.get("vcodec") in (None, "none") and f.get("acodec") in (None, "none")
                and (f.get("ext") or "").lower() == "mp4"):
            add("MP4 — সাউন্ডসহ", f["url"], "সাউন্ডসহ আসল ডাউনলোড লিংক — চাপ দিলেই ডাউনলোড হবে।")
            break
    for f in raw:
        if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none"):
            h = f.get("height")
            res = f"{h}p" if h else ""
            add(f"MP4 — {res} (সাউন্ডসহ)".strip(), f["url"], "সাউন্ডসহ আসল ডাউনলোড লিংক — চাপ দিলেই ডাউনলোড হবে।")
            if len(formats) >= 3:
                break

    # 3) Best video-only MP4 per resolution (highest bitrate per height).
    best_video = {}
    for f in raw:
        if f.get("vcodec") in (None, "none") or f.get("acodec") not in (None, "none"):
            continue
        h = f.get("height")
        if not h:
            continue
        cur = best_video.get(h)
        if cur is None or (f.get("tbr") or 0) > (cur.get("tbr") or 0):
            best_video[h] = f
    for h in sorted(best_video, reverse=True)[:3]:
        f = best_video[h]
        add(f"MP4 — {h}p (ভিডিও-only, অডিও নেই)", f["url"],
            "এই ফাইলে অডিও থাকবে না — অডিও আলাদা করে নিন।")

    # 4) Best audio-only.
    best_audio = None
    for f in raw:
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none"):
            if best_audio is None or (f.get("abr") or 0) > (best_audio.get("abr") or 0):
                best_audio = f
    if best_audio:
        ext = (best_audio.get("ext") or "audio").upper()
        add(f"অডিও — {ext} (শুধু অডিও)", best_audio["url"], "শুধু অডিওর আসল লিংক।")

    # 5) Fallback: yt-dlp's own best requested format.
    for f in info.get("requested_formats") or []:
        if f.get("url"):
            add("MP4 — ভিডিও", f["url"], "আসল ডাউনলোড লিংক।")
            break
    return formats[:6]


INVIDIOUS_INSTANCES = [
    "https://yt.chocolatemoo53.com",
    "https://invidious.privacyredirect.com",
    "https://inv.nadeko.net",
]


def youtube_id_from_url(url):
    try:
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        if q.get("v"):
            return q["v"][0]
        m = re.search(r"/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{6,})", u.path)
        if m:
            return m.group(1)
        m = re.search(r"^/([A-Za-z0-9_-]{6,})$", u.path)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def resolve_youtube_via_invidious(url):
    """Fallback when YouTube bot-blocks yt-dlp from a datacenter IP.
    Invidious is a separate YouTube front-end; its API returns the same
    video's title/author/thumbnail and real googlevideo format URLs.
    Returns a result dict, or None if every instance fails."""
    import urllib.request
    vid = youtube_id_from_url(url)
    if not vid:
        return None
    # The one working instance is flaky (transient timeouts/429s), while
    # the other listed instances are mostly dead — so retry the working
    # instance up to 3 times before trying the rest.
    instances = [INVIDIOUS_INSTANCES[0]] * 2 + INVIDIOUS_INSTANCES[1:]
    for base in instances:
        try:
            req = urllib.request.Request(
                base + "/api/v1/videos/" + vid,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            if data.get("error") or not data.get("title"):
                continue
            fmts = []
            seen_res = set()
            videos = [f for f in data.get("adaptiveFormats") or []
                      if f.get("url") and (f.get("type") or "").startswith("video/")]
            def res_num(f):
                m = re.search(r"(\d+)", f.get("qualityLabel") or f.get("resolution") or "")
                return int(m.group(1)) if m else 0
            # Highest resolution first; prefer MP4 over WebM/AV1 at same resolution.
            videos.sort(key=lambda f: (res_num(f), 1 if (f.get("container") or "").lower() == "mp4" else 0), reverse=True)
            for f in videos:
                res = f.get("qualityLabel") or f.get("resolution") or ""
                if not res or res in seen_res:
                    continue
                seen_res.add(res)
                cont = (f.get("container") or "MP4").upper()
                fmts.append({"label": f"{cont} — {res} (ভিডিও-only, অডিও নেই)",
                             "url": f["url"], "note": "ভিডিও-only — অডিও আলাদা ডাউনলোড করুন।"})
                if len(fmts) >= 3:
                    break
            audios = [f for f in data.get("adaptiveFormats") or []
                      if f.get("url") and (f.get("type") or "").startswith("audio/")]
            audios.sort(key=lambda f: (1 if (f.get("container") or "").lower() == "m4a" else 0, int(f.get("bitrate") or 0)), reverse=True)
            if audios:
                a = audios[0]
                cont = (a.get("container") or "M4A").upper()
                fmts.append({"label": f"অডিও — {cont} (শুধু অডিও)",
                             "url": a["url"], "note": "শুধু অডিও।"})
            if not fmts:
                continue
            return {
                "ok": True, "platform": "YouTube", "title": data.get("title"),
                "author": data.get("author"),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "duration": fmt_duration(data.get("lengthSeconds")),
                "originalUrl": f"https://www.youtube.com/watch?v={vid}",
                "formats": fmts,
                "note": "YouTube সরাসরি ব্লক করায় Invidious রুটে তথ্য আনা হয়েছে — ফরম্যাট ভিডিও-only / অডিও আলাদা হতে পারে।",
            }
        except Exception:
            continue
    return None


TIKWM_LAST_ERROR = ""


def resolve_tiktok_via_tikwm(url):
    """Fallback when TikTok blocks yt-dlp from a datacenter IP
    ('Unexpected response from webpage request'). TikWM is a public
    TikTok API mirror; it returns the same video's title/author/cover
    and a no-watermark MP4 play URL plus the music URL."""
    global TIKWM_LAST_ERROR
    import urllib.request
    for host in ("https://www.tikwm.com", "https://tikwm.com"):
        try:
            api = host + "/api/?url=" + urllib.parse.quote(url, safe="")
            req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            d = data.get("data") or {}
            if data.get("code") != 0 or not d.get("play"):
                TIKWM_LAST_ERROR = f"{host}: code={data.get('code')}"
                continue
            break
        except Exception as e:
            TIKWM_LAST_ERROR = f"{host}: {str(e)[:120]}"
            continue
    else:
        return None
    author = (d.get("author") or {}).get("nickname") or (d.get("author") or {}).get("unique_id")
    fmts = [{"label": "MP4 — সাউন্ডসহ (নো-ওয়াটারমার্ক)", "url": d["play"],
             "note": "সাউন্ডসহ আসল ডাউনলোড লিংক — চাপ দিলেই ডাউনলোড হবে।"}]
    if d.get("wmplay"):
        fmts.append({"label": "MP4 — ওয়াটারমার্কসহ", "url": d["wmplay"],
                     "note": "TikTok ওয়াটারমার্কসহ আসল লিংক।"})
    if d.get("music"):
        fmts.append({"label": "অডিও — MP3 (শুধু অডিও)", "url": d["music"],
                     "note": "শুধু অডিওর আসল লিংক।"})
    return {"ok": True, "platform": "TikTok", "title": d.get("title"),
            "author": author, "thumbnail": d.get("cover"),
            "duration": fmt_duration(d.get("duration")),
            "originalUrl": url, "formats": fmts,
            "note": "TikTok সরাসরি ব্লক করায় TikWM রুটে তথ্য আনা হয়েছে।"}


def resolve(url):
    now = time.time()
    if url in _cache and now - _cache[url][0] < CACHE_SECONDS:
        return _cache[url][1]

    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        host = ""
    platform = platform_of(host) if ALLOWED.match(host) else None
    if not platform:
        result = {"ok": False, "error": "এই লিংকটি সমর্থিত নয়। YouTube, Facebook, Instagram বা TikTok-এর লিংক দিন।"}
        _cache[url] = (now, result)
        return result

    # YouTube often bot-checks datacenter IPs (Render) while a home IP works.
    # Try the normal (web) client first for full formats; if it fails, retry
    # with the android player client, which usually avoids the PO-token check
    # and at least returns a combined audio+video format.
    def run_ytdlp(extra):
        return subprocess.run(
            [YTDLP, "--no-download", "--no-playlist", "--no-warnings",
             "--socket-timeout", "10", "--dump-json"] + extra + [url],
            capture_output=True, text=True, timeout=45,
        )
    def run_ytdlp_all(extra):
        # --format all + first-JSON parse: YouTube's default format
        # selection fails on some clients ("Requested format is not
        # available") even though the client does return formats.
        return subprocess.run(
            [YTDLP, "--no-download", "--no-playlist", "--no-warnings",
             "--socket-timeout", "10", "--dump-json", "--format", "all"] + extra + [url],
            capture_output=True, text=True, timeout=45,
        )

    def first_json(text):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text.strip())
            return obj
        except Exception:
            return None

    # YouTube on Render free crashed with the old approach: yt-dlp
    # --format all dumps a huge multi-JSON blob per client, and 5
    # clients in a row blew the 512MB free instance (fetch failed
    # entirely, "সার্ভারে সমস্যা"). New order, lightest first:
    # 1) Invidious (plain HTTP, no subprocess, seconds, 1080p
    #    video-only + M4A) — this already worked for both test videos.
    # 2) One yt-dlp run with --format best only (single small JSON,
    #    android client, combined format) — never --format all.
    if platform == "YouTube":
        alt = resolve_youtube_via_invidious(url)
        if alt:
            _cache[url] = (now, alt)
            return alt

    try:
        proc = run_ytdlp([]) if platform != "YouTube" else None
        yt_info = None
        if platform == "YouTube":
            # A dummy failed proc so the error path below works even if
            # every client attempt raises before setting proc.
            class _P:
                returncode = 1
                stdout = ""
                stderr = "YouTube clients failed"
            proc = _P()
            for client in ("android", "ios"):
                try:
                    p = subprocess.run(
                        [YTDLP, "--no-download", "--no-playlist", "--no-warnings",
                         "--socket-timeout", "10", "--dump-json", "--format", "best",
                         "--extractor-args", f"youtube:player_client={client}", url],
                        capture_output=True, text=True, timeout=45,
                    )
                except Exception:
                    continue
                try:
                    info = json.loads(p.stdout) if p.stdout.strip() else None
                except Exception:
                    info = None
                if p.returncode == 0 and info and info.get("url"):
                    yt_info = info
                    proc = p
                    break
                proc = p
        if platform == "TikTok" and (proc.returncode != 0 or not proc.stdout.strip()):
            # TikTok's webpage extractor is the part datacenter IPs get
            # blocked on; its API host is a different route and often works.
            proc = run_ytdlp(["--extractor-args", "tiktok:api_hostname=api16-normal-c-useast1a.tiktokv.com"])
    except FileNotFoundError:
        return {"ok": False, "error": "সার্ভারে yt-dlp পাওয়া যায়নি। সার্ভার সেটআপে pip install -r requirements.txt চালাতে হবে।"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ভিডিওর তথ্য আনতে ৯০ সেকেন্ডের বেশি লাগছে। কিছুক্ষণ পর আবার চেষ্টা করুন।"}

    if proc.returncode != 0 or not proc.stdout.strip():
        err = (proc.stderr or "").strip().splitlines()
        msg = err[-1] if err else ""
        if "Private" in msg or "login" in msg.lower():
            detail = "এই ভিডিওটি লগইন ছাড়া দেখা যাচ্ছে না — প্রাইভেট বা ফ্রেন্ডস-অনলি হতে পারে। শুধু পাবলিক ভিডিওই আসবে।"
        else:
            # Show the real yt-dlp reason (truncated) so a failure is diagnosable
            # instead of the same generic message every time.
            detail = "ভিডিওর তথ্য আনা যায়নি। লিংকটি পাবলিক কিনা দেখুন, কিছুক্ষণ পর আবার চেষ্টা করুন।"
            if msg:
                detail += " (সার্ভার: " + msg[:200] + ")"
        # Route 3 for YouTube: Invidious fallback, before giving up.
        if platform == "YouTube":
            alt = resolve_youtube_via_invidious(url)
            if alt:
                _cache[url] = (now, alt)
                return alt
        # Route 3 for TikTok: TikWM fallback, before giving up.
        if platform == "TikTok":
            alt = resolve_tiktok_via_tikwm(url)
            if alt:
                _cache[url] = (now, alt)
                return alt
            if TIKWM_LAST_ERROR:
                detail += f" (TikWM: {TIKWM_LAST_ERROR[:120]})"
        result = {"ok": False, "error": detail}
        # Do NOT cache failures: a cached failure made every retry within
        # 10 minutes return the same error without even trying yt-dlp again.
        return result

    if platform == "YouTube" and yt_info is not None:
        info = yt_info
    else:
        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": "সার্ভারের উত্তর বোঝা যায়নি। আবার চেষ্টা করুন।"}

    title, author = clean_title(info.get("title"), info.get("uploader"))
    # yt-dlp's Facebook uploader is often the whole "views | caption | Page"
    # string, HTML-escaped. Keep only the trailing page name, decoded.
    if author and " | " in author:
        author = html.unescape(author.split(" | ")[-1].strip())
    elif author:
        author = html.unescape(author)
    if title:
        title = html.unescape(title)
    formats = build_formats(info, platform)
    result = {
        "ok": True,
        "platform": platform,
        "title": title or f"{platform} ভিডিও",
        "author": author,
        "thumbnail": info.get("thumbnail"),
        "duration": fmt_duration(info.get("duration")),
        "originalUrl": info.get("webpage_url") or url,
        "formats": formats,
    }
    if not formats:
        result["message"] = "পেজ থেকে নাম/ছবি পাওয়া গেছে, কিন্তু ডাউনলোড লিংক পাওয়া যায়নি। ভিডিওটি পাবলিক হলে আবার চেষ্টা করুন।"
    _cache[url] = (now, result)
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep logs quiet
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            try:
                v = subprocess.run([YTDLP, "--version"], capture_output=True, text=True, timeout=15)
                ver = v.stdout.strip() if v.returncode == 0 else None
            except Exception:
                ver = None
            self._send_json({"ok": True, "yt_dlp": ver})
            return
        if path == "/api/resolve":
            url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
            if not url:
                self._send_json({"ok": False, "error": "কোনো লিংক দেওয়া হয়নি।"}, 400)
                return
            self._send_json(resolve(url))
            return
        if path in ("/", "/index.html"):
            self._send_file(os.path.join("public", "index.html"), "text/html; charset=utf-8")
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    print(f"SaveVid yt-dlp server on port {PORT} (yt-dlp: {YTDLP})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
