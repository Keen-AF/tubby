#!/usr/bin/env python3
"""Tubby: a tiny self-hosted web wrapper around yt-dlp.

Files are amnesiac: every browser window gets its own session folder, which is
wiped a grace period after the window closes (or goes silent). Stdlib only.
"""

import hmac
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("TUBBY_HOST", "0.0.0.0")
PORT = int(os.environ.get("TUBBY_PORT", "8080"))
DATA_DIR = Path(os.environ.get("TUBBY_DIR", "./data")).resolve()
CONCURRENCY = max(1, int(os.environ.get("TUBBY_CONCURRENCY", "2")))
PASSWORD = os.environ.get("TUBBY_PASSWORD", "")
SECRET = (os.environ.get("TUBBY_SECRET") or secrets.token_hex(32)).encode()
GRACE = int(os.environ.get("TUBBY_GRACE", "300"))      # seconds kept after the window closes
TIMEOUT = int(os.environ.get("TUBBY_TIMEOUT", "300"))  # seconds kept after the last heartbeat
YTDLP = os.environ.get("TUBBY_YTDLP", "yt-dlp")
COOKIES = os.environ.get("TUBBY_COOKIES", "")          # optional Netscape cookies.txt
STATIC = Path(__file__).resolve().parent / "static"

SEARCH_PAGE = 12
SEARCH_TTL = 600                                       # seconds a search result is cached
LOOKUP_SLOTS = threading.BoundedSemaphore(3)           # yt-dlp searches / preview lookups at once
# Small preview: one combined file when YouTube still has one (itag 18), otherwise separate
# low-res H.264 video + AAC audio that the page plays side by side. H.264 first for Safari.
PREVIEW_FORMAT = ("18/bv[height<=360][vcodec^=avc1]+ba[ext=m4a]/bv[height<=480][vcodec^=avc1]+ba[ext=m4a]"
                  "/bv[height<=480]+ba/b[height<=480]")
PREVIEW_CHUNK = 10 * 1024 * 1024                       # YouTube throttles large range requests

COOKIE_NAME = "tubby_auth"
COOKIE_TTL = 30 * 24 * 3600
SID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

PRESETS = {
    "best": ["-f", "bv*+ba/b", "--merge-output-format", "mp4"],
    "1080": ["-f", "bv*[height<=1080]+ba/b[height<=1080]", "--merge-output-format", "mp4"],
    "720": ["-f", "bv*[height<=720]+ba/b[height<=720]", "--merge-output-format", "mp4"],
    "audio": ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"],
}

COMMON_ARGS = [
    "--no-playlist", "--newline", "--progress", "--no-simulate", "--no-color",
    "--trim-filenames", "180",
    "-o", "%(title)s [%(id)s].%(ext)s",
    "--print", "before_dl:@@TITLE %(title)s",
    "--print", "after_move:@@FILE %(filepath)s",
    "--progress-template",
    "download:@@PROG %(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(info.vcodec)s",
    "--progress-template", "postprocess:@@POST %(progress.status)s|%(progress.postprocessor)s",
]


# ---------------------------------------------------------------- state

lock = threading.Lock()
sessions = {}            # sid -> Session
job_queue = deque()
job_ready = threading.Condition(lock)


class Session:
    def __init__(self, sid):
        self.id = sid
        self.dir = DATA_DIR / sid
        self.last_seen = time.monotonic()
        self.closed_at = None
        self.jobs = {}   # insertion-ordered: job id -> Job


class Job:
    def __init__(self, session, url, preset):
        self.id = secrets.token_hex(8)
        self.session = session
        self.url = url
        self.preset = preset
        self.dir = session.dir / self.id
        self.status = "queued"   # queued, downloading, processing, done, error, cancelled
        self.title = None
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.stream = None       # "video" or "audio" while downloading
        self.file = None
        self.size = None
        self.error = None
        self.proc = None
        self.log = deque(maxlen=30)

    def public(self):
        return {
            "id": self.id, "url": self.url, "preset": self.preset, "status": self.status,
            "title": self.title, "percent": round(self.percent, 1), "speed": self.speed,
            "eta": self.eta, "stream": self.stream, "size": self.size, "error": self.error,
            "filename": self.file.name if self.file else None,
        }


def touch_session(sid):
    """Return the session for sid, creating it; any contact counts as a heartbeat."""
    with lock:
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = Session(sid)
        s.last_seen = time.monotonic()
        s.closed_at = None
        return s


def kill(job):
    proc = job.proc
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def destroy_session(s):
    with lock:
        sessions.pop(s.id, None)
        jobs = list(s.jobs.values())
        for j in jobs:
            if j.status in ("queued", "downloading", "processing"):
                j.status = "cancelled"
    for j in jobs:
        kill(j)
    shutil.rmtree(s.dir, ignore_errors=True)
    print(f"[tubby] forgot session {s.id[:8]}… ({len(jobs)} job(s))", flush=True)


def reaper():
    while True:
        time.sleep(max(1, min(30, GRACE // 4, TIMEOUT // 4)))
        now = time.monotonic()
        with lock:
            expired = [
                s for s in sessions.values()
                if (s.closed_at is not None and now - s.closed_at >= GRACE)
                or now - s.last_seen >= TIMEOUT
            ]
        for s in expired:
            destroy_session(s)


# ---------------------------------------------------------------- downloads

PLAYLIST_MSG = "Playlists and channels aren't supported yet. Paste a link to a single video."


def is_collection(url):
    """True for YouTube links that point at a playlist or channel rather than one video."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if not (host == "youtube.com" or host.endswith(".youtube.com")):
        return False  # youtu.be links always name a video; other sites are caught in run_job
    q = urllib.parse.parse_qs(u.query)
    if "list" in q and "v" not in q:
        return True
    return re.match(r"^/(playlist|channel/|c/|user/|@)", u.path) is not None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def worker():
    while True:
        with job_ready:
            while not job_queue:
                job_ready.wait()
            job = job_queue.popleft()
            if job.status != "queued":
                continue
            job.status = "downloading"
        run_job(job)


def run_job(job):
    job.dir.mkdir(parents=True, exist_ok=True)
    cmd = [YTDLP, *COMMON_ARGS, *PRESETS[job.preset], "-P", str(job.dir)]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    cmd += ["--", job.url]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
        )
    except OSError as e:
        job.status, job.error = "error", f"Could not start yt-dlp: {e}"
        return
    with lock:
        job.proc = proc
        cancelled = job.status == "cancelled"
    if cancelled:  # cancelled between dequeue and spawn
        kill(job)

    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("@@PROG "):
            done, total, est, speed, eta, vcodec = (line[7:].split("|") + [""] * 6)[:6]
            done, total = num(done), num(total) or num(est)
            with lock:
                if job.status == "cancelled":
                    continue
                job.status = "downloading"
                if done is not None and total:
                    job.percent = min(100.0, done / total * 100)
                job.speed, job.eta = num(speed), num(eta)
                job.stream = "audio" if vcodec == "none" else "video"
        elif line.startswith("@@POST "):
            with lock:
                if job.status != "cancelled":
                    job.status, job.speed, job.eta = "processing", None, None
        elif line.startswith("@@TITLE "):
            if job.title is not None:  # a second item: this link was a playlist after all
                with lock:
                    job.status, job.error = "error", PLAYLIST_MSG
                kill(job)
                break
            job.title = line[8:]
        elif line.startswith("@@FILE "):
            job.file = Path(line[7:])
        elif line.strip():
            job.log.append(line)
    code = proc.wait()

    with lock:
        if job.status in ("cancelled", "error"):
            pass
        elif code == 0 and job.file and job.file.is_file() and job.file.resolve().is_relative_to(job.dir):
            job.status, job.percent, job.size = "done", 100.0, job.file.stat().st_size
            job.speed = job.eta = job.stream = None
        else:
            errors = [l for l in job.log if l.startswith("ERROR")]
            job.status = "error"
            job.error = (errors[-1] if errors else (job.log[-1] if job.log else f"yt-dlp exited with {code}"))
            job.error = re.sub(r"^ERROR:\s*", "", job.error)
        job.proc = None
        failed = job.status != "done"
    if failed:
        shutil.rmtree(job.dir, ignore_errors=True)


# ---------------------------------------------------------------- search

search_cache = {}        # (query, page) -> (expires, results)


def search(query, page):
    """YouTube search via yt-dlp's ytsearch, flat (no per-video lookups), one page at a time."""
    key = (query.casefold(), page)
    now = time.monotonic()
    with lock:
        hit = search_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    first, last = (page - 1) * SEARCH_PAGE + 1, page * SEARCH_PAGE
    cmd = [YTDLP, "--flat-playlist", "-J", "--no-warnings", "--no-color",
           "--playlist-items", f"{first}-{last}"]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    cmd += ["--", f"ytsearch{last}:{query}"]
    if not LOOKUP_SLOTS.acquire(timeout=20):
        raise RuntimeError("Search is busy, try again in a moment.")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=45, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Search timed out.")
    except OSError as e:
        raise RuntimeError(f"Could not start yt-dlp: {e}")
    finally:
        LOOKUP_SLOTS.release()
    if out.returncode != 0:
        errors = [l for l in out.stderr.splitlines() if l.startswith("ERROR")]
        raise RuntimeError(re.sub(r"^ERROR:\s*", "", errors[-1]) if errors else "Search failed.")
    try:
        entries = json.loads(out.stdout).get("entries") or []
    except ValueError:
        raise RuntimeError("Search returned something unexpected.")

    results = []
    for e in entries:
        vid = str(e.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            continue  # channels / playlists mixed into results
        results.append({
            "id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title") or vid,
            "channel": e.get("channel") or e.get("uploader"),
            "duration": num(e.get("duration")),
            "views": num(e.get("view_count")),
            "live": e.get("live_status") == "is_live",
        })
    with lock:
        for k in [k for k, (exp, _) in search_cache.items() if exp <= now]:
            del search_cache[k]
        if len(search_cache) < 256:
            search_cache[key] = (now + SEARCH_TTL, results)
    return results


# ---------------------------------------------------------------- youtube proxy
# Thumbnails and previews are fetched by the server, so viewers' browsers never talk to YouTube.

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
thumb_cache = {}         # video id -> (expires, jpeg bytes)
preview_cache = {}       # video id -> (expires, (url, headers) or error message)


class NoPreview(Exception):
    pass


def remember(cache, key, value, ttl, cap):
    """Store in an in-memory cache, dropping expired entries and then the oldest past cap."""
    now = time.monotonic()
    with lock:
        for k in [k for k, (exp, _) in cache.items() if exp <= now]:
            del cache[k]
        while len(cache) >= cap:
            del cache[next(iter(cache))]
        cache[key] = (now + ttl, value)


def recall(cache, key):
    with lock:
        hit = cache.get(key)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def upstream(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def thumbnail(vid):
    data = recall(thumb_cache, vid)
    if data is None:
        try:
            with upstream(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", timeout=10) as r:
                data = r.read(512 * 1024)
        except (urllib.error.URLError, OSError):
            return None
        remember(thumb_cache, vid, data, 3600, 400)
    return data


def preview_error(stderr):
    """Turn yt-dlp's last ERROR line into (message for the page, worth caching?)."""
    errors = [l for l in stderr.splitlines() if l.startswith("ERROR")]
    err = re.sub(r"^ERROR:\s*(\[\w+\]\s*[\w-]+:\s*)?", "", errors[-1]) if errors else ""
    print(f"[tubby] preview lookup failed: {err or 'no output'}", flush=True)
    low = err.lower()
    if "format is not available" in low:
        return "There's no small version of this video to preview.", True
    if "live event" in low or "premiere" in low:
        return "This video hasn't started yet.", True
    if "sign in" in low or "not a bot" in low:
        return "YouTube wants Tubby to sign in before it can preview this (see TUBBY_COOKIES).", False
    if "private" in low or "unavailable" in low or "removed" in low:
        return "This video is unavailable.", True
    return "Preview failed. Try again in a moment.", False


def preview_source(vid, fresh=False):
    """{"video": (url, headers), "audio": (url, headers) or None} for a small preview, via yt-dlp."""
    hit = None if fresh else recall(preview_cache, vid)
    if isinstance(hit, str):
        raise NoPreview(hit)
    if hit:
        return hit
    cmd = [YTDLP, "-J", "-f", PREVIEW_FORMAT, "--no-warnings", "--no-color"]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    cmd += ["--", f"https://www.youtube.com/watch?v={vid}"]
    if not LOOKUP_SLOTS.acquire(timeout=20):
        raise NoPreview("Tubby is busy, try the preview again in a moment.")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=45, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        raise NoPreview("Preview timed out. Try again in a moment.")
    finally:
        LOOKUP_SLOTS.release()
    if out.returncode != 0:
        msg, lasting = preview_error(out.stderr)
        if lasting:
            remember(preview_cache, vid, msg, 300, 200)
        raise NoPreview(msg)
    try:
        info = json.loads(out.stdout)
    except ValueError:
        raise NoPreview("Preview failed. Try again in a moment.")

    streams, expires = {}, []
    for f in info.get("requested_formats") or [info]:
        url = f.get("url") or ""
        u = urllib.parse.urlsplit(url)
        if f.get("protocol") != "https" or not (u.hostname or "").endswith(".googlevideo.com"):
            msg = "Live streams can't be previewed here." if info.get("is_live") else "This video can't be previewed here."
            remember(preview_cache, vid, msg, 300, 200)
            raise NoPreview(msg)
        headers = {k: v for k, v in (f.get("http_headers") or {}).items() if k in ("User-Agent", "Accept-Language")}
        streams["audio" if f.get("vcodec") == "none" else "video"] = (url, headers)
        exp = urllib.parse.parse_qs(u.query).get("expire", [""])[0]
        if exp.isdigit():
            expires.append(int(exp))
    if "video" not in streams:
        raise NoPreview("This video can't be previewed here.")
    source = {"video": streams["video"], "audio": streams.get("audio")}
    ttl = min(expires) - time.time() - 120 if expires else 3600
    remember(preview_cache, vid, source, max(60, min(ttl, 5 * 3600)), 200)
    return source


# ---------------------------------------------------------------- auth

def make_token():
    exp = str(int(time.time()) + COOKIE_TTL)
    sig = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(tok):
    exp, _, sig = (tok or "").partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    good = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "tubby"
    protocol_version = "HTTP/1.1"

    # -- helpers
    def log_message(self, fmt, *args):
        if self.path.startswith("/api/jobs") and self.command == "GET":
            return  # polling noise
        if self.path.startswith(("/api/search", "/api/thumb/", "/api/preview/")):
            return  # amnesia: don't write what people searched for or watched to the log
        sys.stderr.write(f"[http] {self.address_string()} {fmt % args}\n")

    def send(self, status, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, status=200):
        self.send(status, json.dumps(obj), "application/json")

    def page(self, name):
        self.send(200, (STATIC / name).read_bytes(), "text/html; charset=utf-8", {
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                       "script-src 'self' 'unsafe-inline'; img-src 'self' data:",
            "Referrer-Policy": "no-referrer",
        })

    def body(self, limit=64 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, limit)) if n else b""

    def authed(self):
        if not PASSWORD:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME and valid_token(v):
                return True
        return False

    def session(self, query):
        sid = self.headers.get("X-Tubby-Session") or query.get("s", [""])[0]
        return sid if SID_RE.match(sid) else None

    def route(self):
        u = urllib.parse.urlsplit(self.path)
        return u.path, urllib.parse.parse_qs(u.query)

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, q = self.route()
        if path == "/healthz":
            return self.send(200, "ok")
        if path == "/login":
            return self.page("login.html") if PASSWORD else self.redirect("/")
        if not self.authed():
            return self.redirect("/login") if not path.startswith(("/api/", "/files/")) else self.send(401, "login required")
        if path == "/":
            return self.page("index.html")
        if path == "/api/jobs":
            sid = self.session(q)
            if not sid:
                return self.json({"error": "missing session"}, 400)
            s = touch_session(sid)
            with lock:
                jobs = [j.public() for j in s.jobs.values()]
            return self.json({"jobs": jobs, "grace": GRACE, "auth": bool(PASSWORD)})
        if path == "/api/search":
            query = q.get("q", [""])[0].strip()
            page = q.get("page", ["1"])[0]
            if not query or len(query) > 200:
                return self.json({"error": "Type something to search for."}, 400)
            if not page.isdigit() or not 1 <= int(page) <= 5:
                return self.json({"error": "No more results."}, 400)
            sid = self.session(q)
            if sid:
                touch_session(sid)
            try:
                results = search(query, int(page))
            except RuntimeError as e:
                return self.json({"error": str(e)}, 502)
            return self.json({"results": results, "more": len(results) == SEARCH_PAGE and int(page) < 5})
        m = re.fullmatch(r"/api/(thumb|preview)/([A-Za-z0-9_-]{11})(?:/(video|audio))?", path)
        if m and m.group(1) == "thumb" and not m.group(3):
            data = thumbnail(m.group(2))
            return self.send(200, data, "image/jpeg") if data else self.send(404, "not found")
        if m and m.group(1) == "preview" and not m.group(3):
            try:  # the page asks this first: it does the slow lookup and says how to play it
                source = preview_source(m.group(2))
            except NoPreview as e:
                return self.json({"error": str(e)}, 404)
            return self.json({"audio": source["audio"] is not None})
        if m and m.group(1) == "preview":
            return self.proxy_preview(m.group(2), m.group(3))
        if path.startswith("/files/"):
            return self.serve_file(path[len("/files/"):], q)
        self.send(404, "not found")

    def do_POST(self):
        path, q = self.route()
        if path == "/login":
            return self.login()
        if not self.authed():
            return self.send(401, "login required")
        sid = self.session(q)
        if not sid:
            return self.json({"error": "missing session"}, 400)
        if path == "/api/bye":
            with lock:
                s = sessions.get(sid)
                if s:
                    s.closed_at = time.monotonic()
            return self.send(204)
        if path == "/api/logout":
            return self.send(204, headers={"Set-Cookie": f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
        if path == "/api/jobs":
            try:
                data = json.loads(self.body() or b"{}")
            except ValueError:
                return self.json({"error": "bad json"}, 400)
            url = str(data.get("url", "")).strip()
            preset = str(data.get("preset", "best"))
            if not re.match(r"^https?://\S+$", url) or len(url) > 2048:
                return self.json({"error": "That doesn't look like a video URL."}, 400)
            if is_collection(url):
                return self.json({"error": PLAYLIST_MSG}, 400)
            if preset not in PRESETS:
                return self.json({"error": "Unknown quality preset."}, 400)
            s = touch_session(sid)
            job = Job(s, url, preset)
            with job_ready:
                s.jobs[job.id] = job
                job_queue.append(job)
                job_ready.notify()
            return self.json({"job": job.public()}, 201)
        self.send(404, "not found")

    def do_DELETE(self):
        path, q = self.route()
        if not self.authed():
            return self.send(401, "login required")
        sid = self.session(q)
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{16})", path)
        if not sid or not m:
            return self.send(404, "not found")
        s = touch_session(sid)
        with lock:
            job = s.jobs.get(m.group(1))
            if job and job.status in ("queued", "downloading", "processing"):
                job.status = "cancelled"   # stays listed so the UI can show it
            elif job:
                del s.jobs[job.id]
        if not job:
            return self.send(404, "not found")
        kill(job)
        shutil.rmtree(job.dir, ignore_errors=True)
        self.send(204)

    # -- endpoints
    def redirect(self, where, headers=None):
        self.send(303, "", headers={"Location": where, **(headers or {})})

    def login(self):
        if not PASSWORD:
            return self.redirect("/")
        form = urllib.parse.parse_qs(self.body().decode("utf-8", "replace"))
        given = form.get("password", [""])[0]
        if hmac.compare_digest(given.encode(), PASSWORD.encode()):
            cookie = f"{COOKIE_NAME}={make_token()}; Max-Age={COOKIE_TTL}; Path=/; HttpOnly; SameSite=Strict"
            return self.redirect("/", {"Set-Cookie": cookie})
        time.sleep(1)  # slow down guessing
        self.redirect("/login?bad=1")

    def proxy_preview(self, vid, kind):
        # Every response is a bounded 206 chunk; the <video> element asks for the next one itself.
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "").strip())
        start = int(m.group(1)) if m else 0
        last = start + PREVIEW_CHUNK - 1
        if m and m.group(2):
            last = min(last, int(m.group(2)))
        for attempt in (1, 2):
            try:
                stream = preview_source(vid, fresh=attempt == 2)[kind]
            except NoPreview as e:
                return self.send(404, str(e))
            if not stream:
                return self.send(404, "not found")
            url, headers = stream
            try:
                r = upstream(url, {**headers, "Range": f"bytes={start}-{last}"})
                break
            except urllib.error.HTTPError as e:
                if e.code == 416:
                    return self.send(416, "", headers={"Content-Range": e.headers.get("Content-Range", "bytes */0")})
                if attempt == 1 and e.code in (403, 404, 410):
                    continue  # stream url expired or rotated: look it up again
                return self.send(502, "preview unavailable")
            except (urllib.error.URLError, OSError):
                return self.send(502, "preview unavailable")

        with r:
            ctype = r.headers.get("Content-Type", "")
            length = r.headers.get("Content-Length")
            crange = r.headers.get("Content-Range")
            self.send_response(206 if crange else 200)
            self.send_header("Content-Type", ctype if ctype.startswith(("video/", "audio/")) else "video/mp4")
            if length:
                self.send_header("Content-Length", length)
            if crange:
                self.send_header("Content-Range", crange)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if not length:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                while chunk := r.read(64 * 1024):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except OSError:  # upstream stalled mid-chunk: the byte count is now wrong, so hang up
                self.close_connection = True

    def serve_file(self, job_id, q):
        sid = self.session(q)
        with lock:
            s = sessions.get(sid) if sid else None
            job = s.jobs.get(job_id) if s else None
            path = job.file if job and job.status == "done" else None
        if not path or not path.is_file():
            return self.send(404, "not found")
        if sid:
            touch_session(sid)

        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                return self.send(416, "", headers={"Content-Range": f"bytes */{size}"})
            status = 206

        name = path.name
        ascii_name = name.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        remaining = end - start + 1
        try:
            with path.open("rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------- main

def check():
    """Fail loudly if a bundled dependency is missing (used at image build time)."""
    ok = True
    for cmd in ([YTDLP, "--version"], ["ffmpeg", "-version"], ["ffprobe", "-version"], ["deno", "--version"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.splitlines()[0]
            print(f"ok   {cmd[0]}: {out}")
        except (OSError, subprocess.CalledProcessError, IndexError) as e:
            print(f"FAIL {cmd[0]}: {e}")
            ok = False
    return ok


def main():
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)

    # Amnesia starts at boot: nothing survives a restart.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for child in DATA_DIR.iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)

    for _ in range(CONCURRENCY):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"[tubby] listening on http://{HOST}:{PORT}  data={DATA_DIR}  "
          f"auth={'on' if PASSWORD else 'off'}  grace={GRACE}s  timeout={TIMEOUT}s", flush=True)
    try:
        httpd.serve_forever()
    finally:
        for s in list(sessions.values()):
            destroy_session(s)
        print("[tubby] bye", flush=True)


if __name__ == "__main__":
    main()
