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
  * Only YouTube, Facebook, Instagram, TikTok, X/Twitter domains allowed.
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
    r"twitter\.com|x\.com|facebook\.com|fb\.watch|instagram\.com)$",
    re.IGNORECASE,
)

_cache = {}  # url -> (timestamp, result dict)


def platform_of(host):
    h = host.lower()
    if "youtube" in h or "youtu.be" in h:
        return "YouTube"
    if "tiktok" in h:
        return "TikTok"
    if h == "x.com" or "twitter" in h:
        return "X (Twitter)"
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

    # 2) Best video-only MP4 per resolution (highest bitrate per height).
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

    # 3) Best audio-only.
    best_audio = None
    for f in raw:
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none"):
            if best_audio is None or (f.get("abr") or 0) > (best_audio.get("abr") or 0):
                best_audio = f
    if best_audio:
        ext = (best_audio.get("ext") or "audio").upper()
        add(f"অডিও — {ext} (শুধু অডিও)", best_audio["url"], "শুধু অডিওর আসল লিংক।")

    # 4) Any other combined (audio+video) format yt-dlp offers, e.g. TikTok.
    for f in raw:
        if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none"):
            h = f.get("height")
            res = f"{h}p" if h else ""
            add(f"MP4 — {res}".strip(), f["url"], "আসল ডাউনলোড লিংক।")
            if len(formats) >= 6:
                break

    # 5) Fallback: yt-dlp's own best requested format.
    for f in info.get("requested_formats") or []:
        if f.get("url"):
            add("MP4 — ভিডিও", f["url"], "আসল ডাউনলোড লিংক।")
            break
    return formats[:6]


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
        result = {"ok": False, "error": "এই লিংকটি সমর্থিত নয়। YouTube, Facebook, Instagram, TikTok বা X-এর লিংক দিন।"}
        _cache[url] = (now, result)
        return result

    try:
        proc = subprocess.run(
            [YTDLP, "--no-download", "--no-playlist", "--no-warnings",
             "--socket-timeout", "15", "--dump-json", url],
            capture_output=True, text=True, timeout=90,
        )
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
            detail = "ভিডিওর তথ্য আনা যায়নি। লিংকটি পাবলিক কিনা দেখুন, কিছুক্ষণ পর আবার চেষ্টা করুন।"
        result = {"ok": False, "error": detail}
        _cache[url] = (now, result)
        return result

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
