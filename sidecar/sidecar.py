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
import sqlite3
import subprocess
import threading
import time
import tomllib
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlparse
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
_LOCKS = {}
_GUARD = threading.Lock()


def lock_for(path):
    with _GUARD:
        return _LOCKS.setdefault(path, threading.Lock())
CACHE_MAX_BYTES = int(os.environ.get("CACHE_MAX_BYTES", "0"))   # 0 = no cap, manual rm is the policy


def api(path, method="GET", body=None, timeout=30, bearer=""):
    req = Request(f"{BASE}/api/v1/{path}", method=method)
    if bearer:
        req.add_header("authorization", bearer)
    data = json.dumps(body).encode() if body is not None else None
    if data:
        # BookOrbit's body parser only reads JSON when content-type says so; without this every POST
        # and PUT comes back 400.
        req.add_header("content-type", "application/json")
    with urlopen(req, data, timeout=timeout) as r:
        return json.load(r)


def library(bearer):
    # BookOrbit has no GET /books list route; POST /books/query accepts an empty query.
    page = api("books/query", "POST", {}, bearer=bearer)
    items = page.get("items", page) if isinstance(page, dict) else page
    return [{"id": b["id"], "title": b.get("title") or f"book {b['id']}", "authors": b.get("authors") or [],
            "seriesName": b.get("seriesName")}  # the client groups books by this
            for b in items
            if any((f.get("format") or "").lower() in AUDIO_FORMATS for f in b.get("files", []))]


def author_id(name, bearer):
    hits = api(f"authors?q={quote(str(name))}&size=5", bearer=bearer).get("items", [])
    return next((a["id"] for a in hits if (a.get("name") or "").lower() == str(name).lower()),
                hits[0]["id"] if hits else None)


def by_author(name, bearer):
    # mirrors BookOrbit's own GET /authors/{id}/books; the client only knows author names
    aid = author_id(name, bearer)
    if aid is None:
        return []
    ids = {str(r[0]) for r in connect().execute("select distinct book_id from tracks")}
    out = []
    for b in api(f"authors/{aid}/books?size=100", bearer=bearer).get("items", []):
        if not any((f.get("format") or "").lower() in AUDIO_FORMATS for f in b.get("files", [])):
            continue
        out.append({"id": b["id"], "title": b.get("title") or f"book {b['id']}",
                    "authors": b.get("authors") or [], "seriesName": b.get("seriesName"),
                    "cached": str(b["id"]) in ids})
    return out


def book_detail(book_id, bearer):
    m = api(f"audiobooks/{book_id}/manifest", bearer=bearer)
    sweep(book_id, m["assets"])
    add_book(book_id, m["book"].get("title"), (m["book"].get("authors") or [""])[0])
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
    }, bearer=bearer)


def connect():
    # path read here, not at import, so a test or a changed CACHE_DIR still lands in the right place
    c = sqlite3.connect(os.path.join(CACHE_DIR, "sidecar.db"))
    c.execute("create table if not exists books(id text primary key, title text, author text)")
    c.execute("create table if not exists tracks(book_id text, asset_id text, ch integer, path text, "
              "size integer, mtime real, primary key(book_id, asset_id, ch))")
    # one row: the app's current state, so it can reopen where it left off
    c.execute("create table if not exists state(id integer primary key check(id = 1), book_id text, author text)")
    return c


def add_book(book_id, title, author):
    c = connect()
    c.execute("insert or replace into books values(?,?,?)", (str(book_id), title, author))
    c.commit()


def add_entry(book_id, asset, ch, path, bearer=""):
    c = connect()
    b = str(book_id)
    if not c.execute("select 1 from books where id = ?", (b,)).fetchone():
        m = api(f"audiobooks/{book_id}/manifest", bearer=bearer)
        add_book(b, m["book"].get("title"), (m["book"].get("authors") or [""])[0])
    c.execute("insert or replace into tracks values(?,?,?,?,?,?)",
              (b, asset["assetId"], ch, path, os.path.getsize(path), time.time()))
    c.commit()
    prune(c)


def load_db():
    c = connect()
    rows = c.execute("select book_id, asset_id, ch, path, size from tracks order by mtime desc").fetchall()
    s = c.execute("select book_id, author from state where id = 1").fetchone()
    if s:
        rows.append(("state", s[1], "", "", 0))     # the app's current state, never pruned
    return rows


def cached_books():
    c = connect()
    return [{"id": r[0], "title": r[1], "authors": [r[2]]} for r in c.execute(
        "select distinct b.id, b.title, b.author from books b join tracks t on t.book_id = b.id "
        "order by b.author, b.title")]


def cached_authors():
    c = connect()
    return [{"author": r[0], "books": r[1]} for r in c.execute(
        "select b.author, count(distinct t.book_id) from books b join tracks t on t.book_id = b.id "
        "group by b.author order by b.author")]


def app_state():
    r = connect().execute("select book_id, author from state where id = 1").fetchone()
    return {"book": r[0], "author": r[1]} if r else None


def save_app_state(book_id, author):
    c = connect()
    cur = app_state() or {}
    c.execute("insert or replace into state values(1,?,?)",
              (str(book_id) if book_id else cur.get("book") or "",
               author if author is not None else cur.get("author") or ""))
    c.commit()
    return app_state()


def prune(c):
    """Keep the newest book per author plus the app's current book, oldest first, until CACHE_MAX_BYTES.
    The state row is never dropped - that is what the app reopens on."""
    if not CACHE_MAX_BYTES:
        return
    rows = c.execute("select t.book_id, b.author, t.size, t.path from tracks t join books b on b.id = t.book_id "
                     "order by t.mtime").fetchall()
    newest = {}
    for r in sorted(c.execute("select t.book_id, b.author, t.mtime from tracks t join books b on b.id = t.book_id"),
                    key=lambda r: r[2], reverse=True):
        newest.setdefault(r[1], r[0])
    keep = set(newest.values()) | ({r[0] for r in c.execute("select book_id from state where id = 1")})
    total = sum(r[2] for r in rows)
    for b, a, s, p in rows:
        if total <= CACHE_MAX_BYTES:
            break
        if b in keep:
            continue
        if os.path.exists(p):
            os.remove(p)
        c.execute("delete from tracks where path = ?", (p,))
        total -= s
    c.execute("delete from books where id not in (select distinct book_id from tracks)")
    c.commit()


def key(s):
    return "".join(c for c in (s or "") if c.isalnum() or c in "-_") or "x"


def stem(book_id, asset):
    # etag in the name so a source file re-processed by BookOrbit cannot serve stale audio
    return os.path.join(CACHE_DIR, str(book_id), f"{asset['assetId']}_{key(asset.get('etag'))}")


def cache_path(book_id, asset, suffix):
    return f"{stem(book_id, asset)}_{suffix}.mp3"


def sweep(book_id, assets):
    """Drop entries no live asset points at: stale etag, or a book/chapter deleted in BookOrbit.
    A cached source and its chapter files share the same asset_etag prefix."""
    d = os.path.join(CACHE_DIR, str(book_id))
    if not os.path.isdir(d):
        return
    live = {f"{a['assetId']}_{key(a.get('etag'))}" for a in assets}
    for name in os.listdir(d):
        if not any(name.startswith(p) for p in live):
            os.remove(os.path.join(d, name))
    if not os.listdir(d):
        os.rmdir(d)
    c = connect()
    for (p,) in c.execute("select path from tracks where book_id = ?", (str(book_id),)).fetchall():
        if not os.path.exists(p):
            c.execute("delete from tracks where path = ?", (p,))
    c.commit()


def evict(keep=None):
    """Oldest cached file first, until the cache fits CACHE_MAX_BYTES."""
    if not CACHE_MAX_BYTES:
        return
    items, total = [], 0
    for root, _, names in os.walk(CACHE_DIR):
        for n in names:
            if n.endswith(".db"):                       # the index itself is not cache to evict
                continue
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


def ffmpeg_cmd(src, dst, from_ms=0, span_ms=0):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if from_ms:
        cmd += ["-ss", f"{from_ms / 1000:.3f}"]
    if span_ms:
        cmd += ["-t", f"{span_ms / 1000:.3f}"]
    return cmd + ["-i", src, "-vn", "-map", "0:a:0", "-af", "aformat=channel_layouts=mono",
                  "-c:a", "libmp3lame", "-ar", str(SAMPLE_RATE), "-b:a", f"{BITRATE_K}k",
                  "-f", "mp3", "-y", dst]


def transcode(src, dst, from_ms=0, span_ms=0):
    """Any BookOrbit audio format -> 48 kHz mono CBR MP3. Reuses the cached file."""
    if os.path.exists(dst):
        return dst
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    part = dst + ".part"
    subprocess.run(ffmpeg_cmd(src, part, from_ms, span_ms), check=True)
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


def chapter_entry(book_id, asset_id, ch_idx, bearer):
    m = api(f"audiobooks/{book_id}/manifest", bearer=bearer)
    asset = next((a for a in m["assets"] if a["assetId"] == asset_id), None)
    if not asset:
        raise LookupError(f"asset {asset_id} is not in book {book_id}")
    ch = next((c for c in m["chapters"] if c["assetId"] == asset_id and ch_idx is not None
               and c["sequence"] == ch_idx), None)
    return asset, ch


def ensure_track(book_id, asset_id, ch_idx, bearer):
    """One chapter as a cached CBR MP3. For an M4B the asset is the whole book - the live run pulled
    35 MB for a 2-minute chapter - so the cache is per chapter, sliced from a source that is kept on
    disk and downloaded once. The device can only cache what it can verify against a content-length,
    so the file is complete before the first byte is sent."""
    asset, ch = chapter_entry(book_id, asset_id, ch_idx, bearer)
    ext = (asset["format"] or "bin").lower()
    src = f"{stem(book_id, asset)}.{ext}"
    dst = cache_path(book_id, asset, ch["sequence"] if ch else "all")
    with lock_for(src):
        if not os.path.exists(src):
            os.makedirs(os.path.dirname(src) or ".", exist_ok=True)
            # MP4 needs a seekable input, so the source is a file, not a pipe
            with open_asset(book_id, asset_id, bearer) as r, open(src, "wb") as f:
                shutil.copyfileobj(r, f)
    with lock_for(dst):
        span = (ch["endMs"] - ch["startMs"]) if ch else 0
        transcode(src, dst, (ch or {}).get("assetOffsetMs", 0), span)
        evict(dst)
        add_entry(book_id, asset, ch["sequence"] if ch else 0, dst, bearer)
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
                    return self.json(cached_books())
                if parts == ["library"]:
                    return self.json(library(b))
                if parts == ["authors"]:
                    return self.json(cached_authors())
                if len(parts) == 3 and parts[0] == "authors" and parts[2] == "books":
                    return self.json(by_author(unquote(parts[1]), b))
                if parts == ["state"]:
                    return self.json(app_state())
                if len(parts) == 2 and parts[0] == "books":
                    return self.json(book_detail(parts[1], b))
                if len(parts) == 3 and parts[0] == "books" and parts[2] == "state":
                    return self.json(book_state(parts[1], b))
                if len(parts) == 3 and parts[0] == "track":
                    q = parse_qs(u.query)
                    from_ms = int((q.get("from_ms") or ["0"])[0])
                    ch = q.get("chapter")
                    path = ensure_track(parts[1], parts[2], int(ch[0]) if ch else None, b)
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
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                if parts == ["state"]:
                    return self.json(save_app_state(body.get("book"), body.get("author")))
                if len(parts) == 3 and parts[0] == "books" and parts[2] == "state":
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
