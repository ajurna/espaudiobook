#!/usr/bin/env python3
"""BookOrbit proxy for the ESP32 player: one stable flat API + an ffmpeg transcode cache.

BookOrbit's API is not a stable public integration surface, so every BookOrbit-specific call
lives in this file. The firmware only ever talks to the endpoints below.

  GET  /books                     -> [{id, title, authors}]                    (audiobooks only)
  GET  /books/{id}                -> {id, title, authors, narrators, totalDurationMs, revision,
                                     chapters: [{id, title, assetId, startMs, endMs, durationMs}]}
  GET  /books/{id}/state          -> {assetId, positionMs, percentage} | null
  PUT  /books/{id}/state          <- {"assetId": "...", "positionMs": 1234}
  GET  /state?book={id}           -> same as /books/{id}/state, nothing is stored here
  PUT  /state                     <- {"book": "...", "assetId": "...", "positionMs": 1234}
  GET  /track/{bookId}/{assetId}  -> audio/mpeg, optional ?from_ms=N (frame-aligned)
  GET  /track?book={id}&chapter=N -> the same stream addressed by chapter number, which is what the
                                      player caches on the card
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
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request as UrlRequest, urlopen

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

BASE = os.environ.get("BOOKORBIT_URL", "http://127.0.0.1:3000").rstrip("/")
CACHE_DIR = os.environ.get("CACHE_DIR", "cache")
PORT = int(os.environ.get("PORT", "8090"))


def version():
    # pyproject.toml is the single source of the version (uv reads it for the lockfile)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pyproject.toml")
    return tomllib.load(open(p, "rb"))["project"]["version"] if os.path.exists(p) else ""

SAMPLE_RATE = 44100          # matches the firmware ES8311_SAMPLE_RATE
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
    req = UrlRequest(f"{BASE}/api/v1/{path}", method=method)
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


def by_author(name, bearer, size=100):
    # mirrors BookOrbit's own GET /authors/{id}/books; size is capped at 100 per page server-side
    aid = author_id(name, bearer)
    if aid is None:
        return []
    ids = {str(r[0]) for r in connect().execute("select distinct book_id from tracks")}
    out, page = [], 0
    while True:
        items = api(f"authors/{aid}/books?page={page}&size={size}", bearer=bearer).get("items", []) or []
        out += [{"id": b["id"], "title": b.get("title") or f"book {b['id']}",
                 "authors": b.get("authors") or [], "seriesName": b.get("seriesName"),
                 "cached": str(b["id"]) in ids}
                for b in items if any((f.get("format") or "").lower() in AUDIO_FORMATS for f in b.get("files", []))]
        if len(items) < size:
            return out
        page += 1


def book_detail(book_id, bearer):
    m = api(f"audiobooks/{book_id}/manifest", bearer=bearer)
    sweep(book_id, m["assets"])   # viewing a book is not a cached file, so no book row here
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
    os.makedirs(CACHE_DIR, exist_ok=True)   # a fresh CACHE_DIR 500s every route that reads the db
    c = sqlite3.connect(os.path.join(CACHE_DIR, "sidecar.db"))
    c.execute("create table if not exists books(id text primary key, title text, author text)")
    c.execute("create table if not exists tracks(book_id text, asset_id text, ch integer, path text, "
              "size integer, mtime real, primary key(book_id, asset_id, ch))")
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


def cached_books():
    # books rows only exist while they have a cached track, so the index is the cache
    return [{"id": r[0], "title": r[1], "authors": [r[2]]} for r in connect().execute(
        "select id, title, author from books order by author, title")]


def cached_authors(bearer):
    # every author that has books, not only the ones this sidecar cached
    cached = {r[0]: r[1] for r in connect().execute("select author, count(*) from books group by author")}
    out, page = [], 0
    while True:
        items = api(f"authors?size=100&page={page}&minBookCount=1", bearer=bearer).get("items", []) or []
        out += [{"author": a.get("name"), "books": a.get("bookCount") or 0, "cached": cached.get(a.get("name"), 0)}
                for a in items]
        if len(items) < 100:
            return out
        page += 1


def drop_book(c, book_id):
    # a book row only exists while it has cached audio - the index mirrors what is on disk
    if not c.execute("select 1 from tracks where book_id = ?", (str(book_id),)).fetchone():
        c.execute("delete from books where id = ?", (str(book_id),))


def prune(c):
    """Keep the newest book per author, oldest first, until CACHE_MAX_BYTES."""
    if not CACHE_MAX_BYTES:
        return
    rows = c.execute("select t.book_id, b.author, t.size, t.path from tracks t join books b on b.id = t.book_id "
                     "order by t.mtime").fetchall()
    newest = {}
    for r in sorted(c.execute("select t.book_id, b.author, t.mtime from tracks t join books b on b.id = t.book_id"),
                    key=lambda r: r[2], reverse=True):
        newest.setdefault(r[1], r[0])
    keep = set(newest.values())
    total = sum(r[2] for r in rows)
    for b, a, s, p in rows:
        if total <= CACHE_MAX_BYTES:
            break
        if b in keep:
            continue
        if os.path.exists(p):
            os.remove(p)
        c.execute("delete from tracks where path = ?", (p,))
        drop_book(c, b)
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
    drop_book(c, book_id)
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
    req = UrlRequest(asset_url(book_id, asset_id))
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


def find_start(path, from_ms):
    """Byte offset to serve from. A proportional offset can land mid-frame, so re-anchor to a sync word."""
    if not from_ms:
        return 0
    pos = int(from_ms * BYTES_PER_MS)
    # shortcut: assumes CBR and ignores the tag/info header, so a resume can be a few ms late.
    with open(path, "rb") as f:
        return min(find_sync(f.read(pos + 4096), pos), os.path.getsize(path))


def send_file(path, start=0):
    with open(path, "rb") as f:
        f.seek(start)
        while (chunk := f.read(8192)):
            yield chunk


# --- HTTP layer ---------------------------------------------------------------

app = FastAPI()


def fail(message, code):
    return JSONResponse({"error": message}, status_code=code)


@app.exception_handler(HTTPError)
def bookorbit_error(request, e):
    return fail(f"bookorbit {e.code}", e.code)


@app.exception_handler(StarletteHTTPException)
def http_error(request, e):
    return fail(e.detail, e.status_code)


@app.exception_handler(Exception)
def other_error(request, e):
    return fail(str(e), 500)     # noqa: BLE001 - the device needs the reason


async def read_body(request):
    return json.loads(await request.body() or b"{}")


def raw_token(request: Request) -> str:
    """The device owns the session, we only carry it. The audio library builds its own request line
    and cannot add a header, so the stream request passes the token as ?token=."""
    if h := request.headers.get("authorization"):
        return h
    tok = request.query_params.get("token")
    return f"Bearer {tok}" if tok else ""


def bearer(request: Request) -> str:
    b = raw_token(request)
    if not b:
        raise HTTPException(401, "no token")
    return b


@app.middleware("http")
async def access_log(request, call_next):
    response = await call_next(request)
    print(f"{request.method} {request.url.path} {response.status_code}")   # path only: no token logged
    return response


@app.get("/healthz")
def healthz():
    return {"ok": True, "ffmpeg": bool(shutil.which("ffmpeg")), "version": version()}


@app.get("/books")
def books(b: str = Depends(bearer)):
    return cached_books()


@app.get("/library")
def library_route(b: str = Depends(bearer)):
    return library(b)


@app.get("/authors")
def authors(b: str = Depends(bearer)):
    return cached_authors(b)


@app.get("/authors/{name}/books")
def author_books(name: str, b: str = Depends(bearer)):
    return by_author(name, b)


@app.get("/state")
def state_route(book: str = "", b: str = Depends(bearer)):
    # nothing is stored here - BookOrbit owns playback state, the sidecar only forwards
    if not book:
        return fail("state needs ?book=", 400)
    return book_state(book, b)


@app.put("/state")
async def put_state(request: Request, b: str = Depends(bearer)):
    body = await read_body(request)
    if not body.get("book"):
        return fail("state needs a book", 400)
    return save_state(body["book"], body["assetId"], int(body["positionMs"]), b)


@app.get("/books/{book_id}")
def get_book(book_id: str, b: str = Depends(bearer)):
    return book_detail(book_id, b)


@app.get("/books/{book_id}/state")
def get_book_state(book_id: str, b: str = Depends(bearer)):
    return book_state(book_id, b)


@app.put("/books/{book_id}/state")
async def put_book_state(book_id: str, request: Request, b: str = Depends(bearer)):
    body = await read_body(request)
    return save_state(book_id, body["assetId"], int(body["positionMs"]), b)


# the device logs in and refreshes on its own; we only forward the credential it sends
@app.post("/auth/{action}")
async def auth(action: str, request: Request, b: str = Depends(raw_token)):
    # login is how the device gets its token, so there is none to carry
    if action not in ("login", "refresh"):
        return fail("not found", 404)
    return api("auth/" + action, "POST", await read_body(request), bearer=b)


@app.get("/track/{book_id}/{asset_id}")
def track(book_id: str, asset_id: str, from_ms: int = 0, chapter: int | None = None, b: str = Depends(bearer)):
    path = ensure_track(book_id, asset_id, chapter, b)
    start = find_start(path, from_ms)
    # content-length is what we actually send: the device only stores a track when this matches the
    # bytes it received, and h11 raises "Too little data for declared Content-Length" if we promise more.
    return StreamingResponse(send_file(path, start), media_type="audio/mpeg",
                             headers={"content-length": str(os.path.getsize(path) - start)})


@app.get("/track")
def track_by_chapter(book: str, chapter: int, from_ms: int = 0, b: str = Depends(bearer)):
    # the player knows chapter numbers, not asset ids, so resolve here instead of making it fetch the manifest
    ch = book_detail(book, b)["chapters"][chapter]
    return track(book, ch["assetId"], from_ms, chapter, b)


def main():
    print(f"sidecar on :{PORT} -> {BASE}, cache {CACHE_DIR}")   # plain http, TLS ends at the proxy
    # shortcut: sync routes share uvicorn's default 40-worker pool (the old server took one thread per
    # request); pass workers=4 if a prefetch burst can ever run more than 40 transcodes at once.
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False)


if __name__ == "__main__":
    main()
