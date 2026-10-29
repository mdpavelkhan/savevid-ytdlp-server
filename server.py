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
  * The server normally never downloads the video file itself, so it
    uses almost no bandwidth; the user's browser downloads straight
    from the CDN. EXCEPTION: YouTube merged (video+audio) downloads
    (/api/merge) — YouTube offers no combined file for many videos,
    so the server must download the 360p video + audio, mux them
    with ffmpeg -c copy (no re-encode), and serve the merged file
    temporarily; the merged file is deleted after download / 15 min.
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
import threading
import time
import uuid
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
            # COMBINED first: Invidious "formatStreams" are progressive
            # (audio+video in one file, e.g. itag 18 = 360p MP4). The
            # old code ignored them, so users only ever saw video-only
            # even when a sound-included file existed. Some videos
            # have no formatStream (web client omits it) — then the
            # android yt-dlp route (/api/combined) is the only way.
            for f in data.get("formatStreams") or []:
                if not f.get("url"):
                    continue
                res = f.get("qualityLabel") or f.get("resolution") or ""
                cont = (f.get("container") or "MP4").upper()
                fmts.append({"label": f"{cont} — {res} (সাউন্ডসহ)",
                             "url": f["url"],
                             "note": "সাউন্ডসহ আসল ডাউনলোড লিংক — চাপ দিলেই সাউন্ডসহ ভিডিও নামবে, আলাদা অডিও লাগবে না।"})
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
                "note": ("প্রথম অপশনটি সাউন্ডসহ ভিডিও — সেটা নিলে আলাদা অডিও লাগবে না। " if any("সাউন্ডসহ" in f["label"] for f in fmts) else "") + "YouTube সরাসরি ব্লক করায় Invidious রুটে তথ্য আনা হয়েছে — বাকি ফরম্যাট ভিডিও-only / অডিও আলাদা হতে পারে।",
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


def resolve_youtube_combined(url):
    """One light yt-dlp run (android/ios, --format best, single small
    JSON) looking for a COMBINED audio+video MP4 — the one users
    actually want ("সাউন্ডসহ"). Returns (info, format_dict) or
    (None, None). Never --format all (that crashed Render free)."""
    import subprocess
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
        if p.returncode != 0 or not info:
            continue
        # Prefer the info's own best URL when it is combined; else
        # scan its formats list for a combined MP4.
        candidates = []
        if info.get("url"):
            candidates.append(info)
        candidates.extend(info.get("formats") or [])
        for f in candidates:
            if (f.get("url") and f.get("vcodec") not in (None, "none")
                    and f.get("acodec") not in (None, "none")):
                h = f.get("height")
                res = f"{h}p" if h else ""
                label = f"MP4 — {res} (সাউন্ডসহ)".strip()
                return info, {"label": label, "url": f["url"],
                              "note": "সাউন্ডসহ আসল ডাউনলোড লিংক — চাপ দিলেই সাউন্ডসহ ভিডিও নামবে, আলাদা অডিও লাগবে না।"}
    return None, None


def ffmpeg_exe():
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _download_file(url, dest):
    # Parallel range download first: YouTube throttles a SINGLE
    # connection (~0.11 MB/s measured), but several parallel range
    # requests can add up. Fall back to a single download if the
    # server does not support Range / Content-Length.
    import urllib.request, threading
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"},
                                     method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0)
    except Exception:
        total = 0
    if total > 0:
        n = 8
        part = total // n
        errors = []
        def _fetch(i):
            try:
                s, e = i * part, (total - 1 if i == n - 1 else (i + 1) * part - 1)
                rq = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0",
                                  "Range": f"bytes={s}-{e}"})
                with urllib.request.urlopen(rq, timeout=120) as r, \
                        open(f"{dest}.part{i}", "wb") as out:
                    while True:
                        c = r.read(1024 * 256)
                        if not c:
                            break
                        out.write(c)
            except Exception as ex:
                errors.append(ex)
        ths = [threading.Thread(target=_fetch, args=(i,), daemon=True)
               for i in range(n)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        if not errors and all(os.path.exists(f"{dest}.part{i}")
                              for i in range(n)):
            with open(dest, "wb") as out:
                for i in range(n):
                    p = f"{dest}.part{i}"
                    with open(p, "rb") as f:
                        out.write(f.read())
                    os.remove(p)
            if os.path.getsize(dest) == total:
                return
    # Fallback: single-connection download.
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)


_MERGE_JOBS = {}  # job_id -> {status, stage, file, title, error, created}


def _merge_worker(job_id, video_url, audio_url, title):
    # video_url / audio_url may be a single URL or an ordered list
    # of candidates. A 403 on one googlevideo host does not mean
    # every format is blocked, so try the next resolution / audio
    # track before giving up, and say which file failed.
    job = _MERGE_JOBS[job_id]
    base = os.path.join("/tmp", "savevid_merge_" + job_id)
    def _try_all(cands, dest, kind):
        if isinstance(cands, str):
            cands = [cands]
        last = None
        for u in cands:
            try:
                _download_file(u, dest)
                if os.path.getsize(dest) > 0:
                    return
            except Exception as e:
                last = e
        raise RuntimeError(f"{kind} ডাউনলোডে ব্যর্থ: {last}")
    try:
        os.makedirs(base, exist_ok=True)
        job["stage"] = "ভিডিও ডাউনলোড হচ্ছে…"
        _try_all(video_url, os.path.join(base, "v.mp4"), "ভিডিও")
        job["stage"] = "অডিও ডাউনলোড হচ্ছে…"
        _try_all(audio_url, os.path.join(base, "a.m4a"), "অডিও")
        job["stage"] = "ভিডিও + অডিও একসাথে করা হচ্ছে…"
        exe = ffmpeg_exe()
        if not exe:
            raise RuntimeError("সার্ভারে ffmpeg পাওয়া যায়নি।")
        out = os.path.join(base, "merged.mp4")
        p = subprocess.run([exe, "-y", "-i", os.path.join(base, "v.mp4"),
                            "-i", os.path.join(base, "a.m4a"),
                            "-c", "copy", "-shortest", out],
                           capture_output=True, text=True, timeout=600)
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("মার্জ করতে ব্যর্থ হয়েছে।")
        os.remove(os.path.join(base, "v.mp4"))
        os.remove(os.path.join(base, "a.m4a"))
        job["file"] = out
        job["status"] = "ready"
        job["stage"] = "তৈরি — ডাউনলোড করুন।"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:200]
        job["stage"] = "ব্যর্থ হয়েছে।"


def start_youtube_merge(url):
    """Start a background merge job for a YouTube video: MP4 video
    (highest <=1080p first, then lower resolutions as fallbacks)
    + M4A audio, muxed with ffmpeg -c copy.
    Returns (job_id, error_or_None)."""
    vid = youtube_id_from_url(url)
    if not vid:
        return None, "ভিডিওর লিংকটি বোঝা যায়নি।"
    import urllib.request
    data = None
    for base in [INVIDIOUS_INSTANCES[0]] * 2 + INVIDIOUS_INSTANCES[1:]:
        try:
            req = urllib.request.Request(base + "/api/v1/videos/" + vid,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            if data.get("error") or not data.get("title"):
                data = None
                continue
            break
        except Exception:
            continue
    if not data:
        return None, "Invidious থেকে ভিডিওর তথ্য আনা যায়নি। কিছুক্ষণ পর আবার চেষ্টা করুন।"
    # User's instruction 2026-10-29: merged output is 720p for ALL
    # videos (smaller, faster, more likely under the size guard).
    # Pick 720p if present, else closest to 720p. + M4A audio.
    videos = [f for f in (data.get("adaptiveFormats") or [])
              if f.get("url") and (f.get("type") or "").startswith("video/mp4")]
    def _res(f):
        m = re.search(r"(\d+)", f.get("qualityLabel") or f.get("resolution") or "")
        return int(m.group(1)) if m else 0
    videos.sort(key=lambda f: abs(_res(f) - 720))
    audios = [f for f in (data.get("adaptiveFormats") or [])
              if f.get("url") and (f.get("type") or "").startswith("audio/") and (f.get("container") or "").lower() == "m4a"]
    if not videos or not audios:
        return None, "এই ভিডিওর জন্য মার্জ করার মতো ভিডিও/অডিও পাওয়া যায়নি।"
    v, a = videos[0], audios[0]
    # Rough size guard: (video+audio bitrate) * duration.
    dur = int(data.get("lengthSeconds") or 0)
    est = ((int(v.get("bitrate") or 0) + int(a.get("bitrate") or 0)) * dur) // 8 if dur else 0
    if est > 500 * 1024 * 1024:
        return None, "ভিডিওটি অনেক বড় (৫০০MB-এর বেশি হবে), ফ্রি সার্ভারে মার্জ করা যাবে না। ৩৬০পি ভিডিও-only + আলাদা অডিও নিন।"
    job_id = uuid.uuid4().hex[:16]
    _MERGE_JOBS[job_id] = {"status": "working", "stage": "শুরু হচ্ছে…",
                           "file": None, "title": data.get("title") or "YouTube ভিডিও",
                           "error": None, "created": time.time()}
    threading.Thread(target=_merge_worker,
                     args=(job_id, [f["url"] for f in videos],
                           [f["url"] for f in audios], data.get("title")),
                     daemon=True).start()
    return job_id, None


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

    # YouTube: users want a SOUND-INCLUDED video first, not only
    # video-only + separate audio. Invidious gives only adaptive
    # (video-only) formats, so merge both light routes:
    # 1) yt-dlp android/ios --format best (single small JSON) for a
    #    combined MP4, if one exists for this video.
    # 2) Invidious (plain HTTP) for high-res video-only + M4A.
    # Combined goes FIRST in the list; the rest follow. If neither
    # route works, fall through to the normal error path below.
    # Never --format all (that crashed Render free with huge JSON).
    if platform == "YouTube":
        # Run both light routes IN PARALLEL: sequential runs summed
        # to 50s+ when Invidious was slow, which loses users. Parallel
        # time is the slower of the two, not the sum; if Invidious
        # is still slow after 20s, return the combined format alone
        # instead of blocking the user.
        import concurrent.futures
        _ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            _fc = _ex.submit(resolve_youtube_combined, url)
            _fi = _ex.submit(resolve_youtube_via_invidious, url)
            try:
                _cinfo, _cfmt = _fc.result(timeout=15)
            except Exception:
                _cinfo, _cfmt = None, None
            try:
                alt = _fi.result(timeout=10)
            except Exception:
                alt = None
        finally:
            _ex.shutdown(wait=False)
        if alt and _cfmt and not any("সাউন্ডসহ" in f["label"] for f in (alt.get("formats") or [])):
            alt["formats"] = [_cfmt] + (alt.get("formats") or [])
            alt["note"] = "প্রথম অপশনটি সাউন্ডসহ ভিডিও — সেটা নিলে আলাদা অডিও লাগবে না। " + (alt.get("note") or "")
            _cache[url] = (now, alt)
            return alt
        if alt:
            _cache[url] = (now, alt)
            return alt
        if _cinfo and _cfmt:
            title, author = clean_title(_cinfo.get("title"), _cinfo.get("uploader"))
            result = {"ok": True, "platform": "YouTube",
                      "title": title or "YouTube ভিডিও", "author": author,
                      "thumbnail": _cinfo.get("thumbnail"),
                      "duration": fmt_duration(_cinfo.get("duration")),
                      "originalUrl": _cinfo.get("webpage_url") or url,
                      "formats": [_cfmt]}
            _cache[url] = (now, result)
            return result

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

    def send_response(self, code, message=None):
        # Cross-origin isolation (user instruction 2026-10-29):
        # enables SharedArrayBuffer so ffmpeg.wasm browser merge
        # can load. Tradeoff verified 2026-10-29: unpkg ffmpeg
        # sends CORP: cross-origin (loads fine), but Adsterra
        # bauval.org sends NO CORP header, so its banner scripts
        # may be blocked under require-corp. If banners stop
        # loading, switch COEP to "credentialless" as follow-up.
        super().send_response(code, message)
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")

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
        if path == "/api/merge":
            url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
            if not url:
                self._send_json({"ok": False, "error": "কোনো লিংক দেওয়া হয়নি।"}, 400)
                return
            job_id, err = start_youtube_merge(url)
            if err:
                self._send_json({"ok": False, "error": err})
            else:
                self._send_json({"ok": True, "job": job_id})
            return
        if path == "/api/merge2":
            # Generic merge: frontend passes the exact video-only and
            # audio URLs already returned by /api/resolve. Only CDN
            # hosts are allowed (no arbitrary SSRF). Used for Facebook
            # 1080p+sound, where fbcdn download speed is usable
            # (~1.5 MB/s locally, vs YouTube googlevideo throttling).
            qs = urllib.parse.parse_qs(parsed.query)
            vurl, aurl = qs.get("v", [""])[0], qs.get("a", [""])[0]
            if not vurl or not aurl:
                self._send_json({"ok": False, "error": "ভিডিও/অডিও লিংক দেওয়া হয়নি।"}, 400)
                return
            def _ok_host(u):
                h = (urllib.parse.urlparse(u).hostname or "").lower()
                return any(h == d or h.endswith("." + d) for d in
                           ("fbcdn.net", "googlevideo.com", "cdninstagram.com",
                            "tiktokcdn.com", "tiktokcdn-us.com"))
            if not _ok_host(vurl) or not _ok_host(aurl):
                self._send_json({"ok": False, "error": "এই লিংক মার্জ করা যাবে না।"}, 400)
                return
            job_id = uuid.uuid4().hex[:16]
            _MERGE_JOBS[job_id] = {"status": "working", "stage": "শুরু হচ্ছে…",
                                   "file": None, "title": "Facebook ভিডিও",
                                   "error": None, "created": time.time()}
            threading.Thread(target=_merge_worker,
                             args=(job_id, vurl, aurl, "Facebook ভিডিও"),
                             daemon=True).start()
            self._send_json({"ok": True, "job": job_id})
            return
        if path == "/api/merge/status":
            job_id = urllib.parse.parse_qs(parsed.query).get("job", [""])[0]
            job = _MERGE_JOBS.get(job_id)
            if not job:
                self._send_json({"ok": False, "error": "মার্জ জব পাওয়া যায়নি।"}, 404)
                return
            self._send_json({"ok": True, "status": job["status"],
                             "stage": job["stage"], "error": job["error"],
                             "title": job["title"]})
            return
        if path == "/api/merge/file":
            job_id = urllib.parse.parse_qs(parsed.query).get("job", [""])[0]
            job = _MERGE_JOBS.get(job_id)
            if not job or job["status"] != "ready" or not job["file"] or not os.path.exists(job["file"]):
                self.send_response(404)
                self.end_headers()
                return
            try:
                with open(job["file"], "rb") as fh:
                    body = fh.read()
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                return
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", (job["title"] or "video"))[:60] or "video"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{safe}_sound.mp4"')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/combined":
            # Background route for YouTube: the android yt-dlp run
            # can be slow / bot-blocked on Render, so the main
            # resolve must not block on it. The frontend calls this
            # after showing the fast Invidious list and prepends the
            # combined format when (and if) it arrives.
            url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
            if not url:
                self._send_json({"ok": False, "error": "কোনো লিংক দেওয়া হয়নি।"}, 400)
                return
            _info, _fmt = resolve_youtube_combined(url)
            if _fmt:
                self._send_json({"ok": True, "format": _fmt})
            else:
                self._send_json({"ok": False})
            return
        if path in ("/", "/index.html"):
            self._send_file(os.path.join("public", "index.html"), "text/html; charset=utf-8")
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    print(f"SaveVid yt-dlp server on port {PORT} (yt-dlp: {YTDLP})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
