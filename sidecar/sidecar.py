#!/usr/bin/env python3
"""BookOrbit proxy for the ESP32 player: one stable flat API + an ffmpeg transcode cache.

BookOrbit's API is not a stable public integration surface, so every BookOrbit-specific call
lives in this file. The firmware only ever talks to the endpoints below.

  GET  /books                     -> [{id, title, authors}]                    (audiobooks only)
  GET  /books/{id}                -> {id, title, authors, narrators, totalDurationMs, revision,
                                     chapters: [{id, title, assetId, startMs, endMs, durationMs}]}
  GET  /books/{id}/state          -> {assetId, positionMs, percentage} | null
  PUT  /books/{id}/state          <- {"assetId": "...", "positionMs": 1234}
  GET  /track/{bookId}/{assetId}  -> audio/mpeg, optional ?from_ms=N (frame-aligned)
  POST /auth/login, /auth/refresh -> passed straight through to BookOrbit
  GET  /healthz

Run: BOOKORBIT_URL=http://127.0.0.1:3000 python3 sidecar.py

The sidecar holds no credential. The device owns its session and sends it on every request, so the
sidecar can only act while the device is asking, and only as that session. The token rides in the
Authorization header, or as ?token= for the audio stream - the ESP32 audio library builds its own
request line and cannot add a header. Only /healthz works without a token.

TLS is not handled here - nginx (or your reverse proxy) terminates it in front of the sidecar, which
serves plain http on the compose network. Put the proxy in front anyway: the token rides in the URL
for the audio stream, so plain http on the LAN would leak it.

Why transcode: the ES8311 codec on the board only accepts 8/16/32/44.1/48 kHz, so 24 kHz output
is not an option. Every BookOrbit format (M4B/M4A/MP3/OPUS/OGG/FLAC) becomes 48 kHz mono 48 kbps
CBR MP3 - one decoder path on the device and exact seek math (6 bytes/ms).
"""

import json
import os
import shutil
import subprocess
import tomllib
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

BASE = os.environ.get("BOOKORBIT_URL", "http://127.0.0.1:3000").rstrip("/")
CACHE_DIR = os.environ.get("CACHE_DIR", "cache")
PORT = int(os.environ.get("PORT", "8090"))


def version():
    # pyproject.toml is the single source of the version (uv reads it for the lockfile)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pyproject.toml")
    return tomllib.load(open(p, "rb"))["project"]["version"] if os.path.exists(p) else ""

SAMPLE_RATE = 48000
BITRATE_K = 48
BYTES_PER_MS = BITRATE_K * 1000 // 8000      # 6 bytes/ms - only true for our CBR output
AUDIO_FORMATS = {"m4b", "m4a", "mp3", "opus", "ogg", "flac"}
CACHE_MAX_BYTES = int(os.environ.get("CACHE_MAX_BYTES", "0"))   # 0 = no cap, manual rm is the policy


def api(path, method="GET", body=None, timeout=30, bearer=""):
    req = Request(f"{BASE}/api/v1/{path}", method=method)
    if bearer:
        req.add_header("authorization", bearer)
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(req, data, timeout=timeout) as r:
        return json.load(r)


def book_list(bearer):
    # BookOrbit has no GET /books list route; POST /books/query accepts an empty query.
    page = api("books/query", "POST", {}, bearer=bearer)
    items = page.get("items", page) if isinstance(page, dict) else page
    return [{"id": b["id"], "title": b.get("title") or f"book {b['id']}", "authors": b.get("authors") or []}
            for b in items
            if any((f.get("format") or "").lower() in AUDIO_FORMATS for f in b.get("files", []))]


def book_detail(book_id, bearer):
    m = api(f"audiobooks/{book_id}/manifest", bearer=bearer)
    sweep(book_id, m["assets"])
    return {"id": m["book"]["id"], "title": m["book"]["title"], "authors": m["book"]["authors"],
            "narrators": m["book"]["narrators"], "totalDurationMs": m["totalDurationMs"],
            "revision": m["revision"],
            "chapters": [{"id": c["id"], "title": c["title"], "assetId": c["assetId"],
                          "startMs": c["startMs"], "endMs": c["endMs"],
                          # BookOrbit chapters have no durationMs field (packages/types/src/audiobook.ts:237)
                          "durationMs": c["endMs"] - c["startMs"]}
                         for c in m["chapters"]]}


def book_state(book_id, bearer):
    return api(f"audiobooks/{book_id}/playback-state", bearer=bearer)


def save_state(book_id, asset_id, position_ms, bearer):
    # The device only knows where it is; revision/operation fields are BookOrbit's, so fill them here.
    current = book_state(book_id, bearer) or {}
    return api(f"audiobooks/{book_id}/playback-state", "PUT", {
        "assetId": asset_id,
        "positionMs": position_ms,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "operationId": str(uuid.uuid4()),
        "baseRevision": current.get("revision", 0),
        "manifestRevision": current.get("manifestRevision") or book_detail(book_id, bearer)["revision"],
    })


def key(s):
    return "".join(c for c in (s or "") if c.isalnum() or c in "-_") or "x"


def cache_path(book_id, asset):
    # etag in the name so a source file re-processed by BookOrbit cannot serve stale audio
    return os.path.join(CACHE_DIR, str(book_id), f"{asset['assetId']}_{key(asset['etag'])}.mp3")


def sweep(book_id, assets):
    """Drop entries no live asset points at: stale etag, or a book/chapter deleted in BookOrbit."""
    d = os.path.join(CACHE_DIR, str(book_id))
    if not os.path.isdir(d):
        return
    live = {os.path.basename(cache_path(book_id, a)) for a in assets}
    for name in os.listdir(d):
        if name not in live:
            os.remove(os.path.join(d, name))
    if not os.listdir(d):
        os.rmdir(d)


def evict(keep=None):
    """Oldest cached file first, until the cache fits CACHE_MAX_BYTES."""
    if not CACHE_MAX_BYTES:
        return
    items, total = [], 0
    for root, _, names in os.walk(CACHE_DIR):
        for n in names:
            p = os.path.join(root, n)
            s = os.path.getsize(p)
            total += s
            items.append((os.path.getmtime(p), s, p))
    for _, s, p in sorted(items):
        if total <= CACHE_MAX_BYTES:
            break
        if p == keep:
            continue
        os.remove(p)
        total -= s


def find_sync(data, start=0):
    """A proportional byte offset can land mid-frame; MP3 needs a sync word to start decoding."""
    for i in range(start, min(start + 4096, len(data))):
        if data[i] == 0xFF and data[i + 1] & 0xE0 == 0xE0:
            return i
    return start


def ffmpeg_cmd(src, dst, from_ms=0):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if from_ms:
        cmd += ["-ss", f"{from_ms / 1000:.3f}"]
    return cmd + ["-i", src, "-vn", "-map", "0:a:0", "-af", "aformat=channel_layouts=mono",
                  "-c:a", "libmp3lame", "-ar", str(SAMPLE_RATE), "-b:a", f"{BITRATE_K}k",
                  "-f", "mp3", "-y", dst]


def transcode(src, dst, from_ms=0):
    """Any BookOrbit audio format -> 48 kHz mono CBR MP3. Reuses the cached file."""
    if os.path.exists(dst):
        return dst
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    part = dst + ".part"
    subprocess.run(ffmpeg_cmd(src, part, from_ms), check=True)
    os.replace(part, dst)
    return dst


def open_asset(book_id, asset_id, bearer):
    req = Request(asset_url(book_id, asset_id))
    if bearer:
        req.add_header("authorization", bearer)
    return urlopen(req, timeout=120)


def asset_url(book_id, asset_id):
    return f"{BASE}/api/v1/audiobooks/{book_id}/assets/{asset_id}/content"


def asset_entry(book_id, asset_id, bearer):
    for a in api(f"audiobooks/{book_id}/manifest", bearer=bearer)["assets"]:
        if a["assetId"] == asset_id:
            return a
    raise LookupError(f"asset {asset_id} is not in book {book_id}")


def ensure_track(book_id, asset_id, bearer):
    """The whole chapter as one cached CBR MP3. The device can only cache what it can verify against
    a content-length, so the file is complete before the first byte is sent."""
    a = asset_entry(book_id, asset_id, bearer)
    dst = cache_path(book_id, a)
    if os.path.exists(dst):
        return dst
    fmt = (a["format"] or "").lower()
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    # the source stays on disk during the run because MP4 needs a seekable input
    src = os.path.join(CACHE_DIR, str(book_id), f"{asset_id}.{fmt}")
    with open_asset(book_id, asset_id, bearer) as r, open(src, "wb") as f:
        shutil.copyfileobj(r, f)
    transcode(src, dst)
    os.remove(src)
    evict(dst)
    return dst


def send_file(path, from_ms, send):
    with open(path, "rb") as f:
        if from_ms:
            pos = int(from_ms * BYTES_PER_MS)
            # shortcut: byte offset assumes CBR and ignores the tag/info header; find_sync
            # re-anchors to the next frame from pos, so a resume can be a few ms late.
            w = f.read(pos + 4096)
            f.seek(find_sync(w, pos))
        while (chunk := f.read(8192)):
            send(chunk)


def main():
    class Handler(BaseHTTPRequestHandler):
        def json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def bearer(self):
            # the device owns the session, we only carry it. The audio library builds its own request
            # line and cannot add a header, so the stream request passes the token as ?token=
            if h := self.headers.get("authorization"):
                return h
            tok = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            return f"Bearer {tok}" if tok else ""

        def log_message(self, fmt, *args):
            # keep the token out of the access log
            super().log_message(fmt, *[(a.replace("token=", "token=REDACTED") if isinstance(a, str) else a)
                                       for a in args])

        def do_GET(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            try:
                if parts == ["healthz"]:
                    return self.json({"ok": True, "ffmpeg": bool(shutil.which("ffmpeg")), "version": version()})
                b = self.bearer()
                if not b:
                    return self.json({"error": "no token"}, 401)
                if parts == ["books"]:
                    return self.json(book_list(b))
                if len(parts) == 2 and parts[0] == "books":
                    return self.json(book_detail(parts[1], b))
                if len(parts) == 3 and parts[0] == "books" and parts[2] == "state":
                    return self.json(book_state(parts[1], b))
                if len(parts) == 3 and parts[0] == "track":
                    from_ms = int((parse_qs(u.query).get("from_ms") or ["0"])[0])
                    path = ensure_track(parts[1], parts[2], b)
                    self.send_response(200)
                    self.send_header("content-type", "audio/mpeg")
                    # the device only stores a track when this matches what it received
                    self.send_header("content-length", str(os.path.getsize(path)))
                    self.end_headers()
                    send_file(path, from_ms, self.wfile.write)
                    return
                self.json({"error": "not found"}, 404)
            except HTTPError as e:
                self.json({"error": f"bookorbit {e.code}"}, e.code)
            except Exception as e:                                   # noqa: BLE001 - the device needs the reason
                self.json({"error": str(e)}, 500)

        def do_POST(self):
            # the device logs in and refreshes on its own; we only forward the credential it sends
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            try:
                if parts == ["auth", "login"] or parts == ["auth", "refresh"]:
                    return self.json(api("auth/" + parts[1], "POST", body, bearer=self.bearer()))
                self.json({"error": "not found"}, 404)
            except HTTPError as e:
                self.json({"error": f"bookorbit {e.code}"}, e.code)

        def do_PUT(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            try:
                if len(parts) == 3 and parts[0] == "books" and parts[2] == "state":
                    body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                    return self.json(save_state(parts[1], body["assetId"], int(body["positionMs"]), self.bearer()))
                self.json({"error": "not found"}, 404)
            except HTTPError as e:
                self.json({"error": f"bookorbit {e.code}"}, e.code)
            except Exception as e:                                   # noqa: BLE001
                self.json({"error": str(e)}, 500)

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"sidecar on :{PORT} -> {BASE}, cache {CACHE_DIR}")   # plain http, TLS ends at the proxy
    httpd.serve_forever()


if __name__ == "__main__":
    main()
