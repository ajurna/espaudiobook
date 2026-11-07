# espaudiobook — ESP32-S3 audiobook player for BookOrbit

Two parts, one narrow seam between them:

| part | what it is |
|---|---|
| `sidecar/` | ~150 lines of Python. The **only** code that knows BookOrbit routes. Proxy + ffmpeg cache. |
| `firmware/` | Arduino sketch: LVGL 9 UI on the 1.85" round touchscreen, MP3 playback over Wi-Fi or from the TF card, progress back to BookOrbit. |

Board: Waveshare **ESP32-S3-Touch-LCD-1.85C V2** (ST77916 360×360, CST816 touch, ES8311 codec + NS4150B amp).

## 1. Sidecar

Needs `ffmpeg` on PATH, no other dependencies.

```sh
export BOOKORBIT_URL=http://127.0.0.1:3000
export BOOKORBIT_USER=you BOOKORBIT_PASSWORD=secret   # or BOOKORBIT_TOKEN for a quick test
export CACHE_DIR=cache PORT=8090
python3 sidecar/sidecar.py
python3 sidecar/test_sidecar.py      # transcode + cache-hit + frame-aligned seek
```

Against the deployed sidecar, `testclient/test_client.py` behaves like the ESP32 (Bearer header, `?token=`
for the stream, the 3-chapter prefetch burst, cold vs warm first byte). Put your connection details in
`testclient/.env` (SIDECAR_URL, BOOKORBIT_USER/BOOKORBIT_PASSWORD, or TEST_TOKEN) — it writes playback
state to whatever server you point it at, so pick a book you do not mind touching.

### Screen layout and controls

Same control set in the test client and the firmware: `up`, `go`, `down`, `back`. On a list they move the
highlighted row and open it; on the player up/down are prev/next chapter and go is play/pause. Rows are
drawn clipped to the circle at their own height and live above the buttons, so a row can never sit under
one — rows at y=40..160 (6 per page, chord 212-320 px), buttons 64 px at y=200 (chord 310), 270 (232),
300 (155). `python testclient/gui.py --nav` walks authors -> book -> chapter -> back without a window.

Deploy (image = python + ffmpeg only):

```sh
cp .env.example .env      # BOOKORBIT_URL, cache cap; no credentials - the device owns its session
docker compose up -d --build
```

Run it inside BookOrbit's compose project so `http://app:3000` resolves, sharing the same `.env`:

```sh
docker compose -f docker-compose.yml -f /path/to/bookorbit/docker-compose.yml up -d
```

Standalone, pointing at an already-running BookOrbit:

```sh
docker build -t espaudiobook-sidecar sidecar
mkdir -p data/sidecar
docker run --rm -p 8090:8090 -e BOOKORBIT_URL=http://127.0.0.1:3000 -e BOOKORBIT_TOKEN=... \
  -v "$PWD/data/sidecar:/cache" espaudiobook-sidecar
```

Push to DockerHub. The version lives only in `sidecar/pyproject.toml` (locked with `uv lock`), and
`/healthz` reports it so you can tell which image is running:

```sh
V=$(python -c "import tomllib;print(tomllib.load(open('sidecar/pyproject.toml','rb'))['project']['version'])")
docker build -t ajurna/espaudiobook-sidecar:$V sidecar
docker push ajurna/espaudiobook-sidecar:$V
docker tag ajurna/espaudiobook-sidecar:$V ajurna/espaudiobook-sidecar:latest
docker push ajurna/espaudiobook-sidecar:latest
```

The ESP32 points at `http://<host>:8090`. Cache lives in `./data/sidecar` (the container is read-only,
only `/cache` and `/tmp` are writable); delete it anytime, it is rebuildable from BookOrbit. If the
bind mount is owned by a host uid other than 1000, set `PUID`/`PGID` to it.

Cache is cleaned on its own. Opening a book drops entries no live asset points at — stale etag (the
file was re-processed by BookOrbit) or an asset/book deleted upstream — and `CACHE_MAX_BYTES`
evicts oldest-first after each transcode. On the card the same idea in manifest order: `CACHE_MAX_KB`
(400000, i.e. 400 MB) drops the earliest cached chapter before a new one is written, because SDFS
gives the device no free-space or file-time information.

### Authentication

The ESP32 owns the session and the sidecar only forwards it — `sidecar.py` stores nothing, so it can act
only as the session the device presents. The device logs in (`POST api/v1/auth/login`,
`clientKind: "native"`, which is what puts the refresh token in the JSON body instead of a cookie), keeps
the refresh token in NVS (namespace `auth`), refreshes it when the access token expires, and logs in again
on a 401 — a password change bumps `token_version` and revokes the session.

A BookOrbit access JWT lives 15 minutes (`JWT_EXPIRES_IN`), the refresh token 7 days
(`JWT_REFRESH_EXPIRES_IN`). The device sends it as `Authorization: Bearer` for JSON calls, and as `?token=`
for the audio stream, because ESP32-audioI2S builds its own request line and can only send Basic auth.

nginx (or your proxy) terminates TLS in front of the sidecar, which stays plain http — do this even on the
LAN, since the token rides in the URL for the stream. `?token=` means the token appears in nginx's access
log unless you turn logging off for `/track`. For that location use `proxy_buffering off;` and a long
`proxy_read_timeout` (a chapter streams for minutes). The device pins the cert it is given (`CA_CERT`);
note ESP32-audioI2S calls `clientsecure.setInsecure()` internally, so only the HTTPClient calls verify it.

Two things to know: the refresh token sits in NVS as plain text (anyone holding the device can use it —
revoke it in Settings → Sessions), and the sidecar cannot tell the device apart from any client that has
the token.

What the device sees (flat, stable — BookOrbit's own API is not a public integration surface,
issue #650 was declined, so all BookOrbit coupling stays in `sidecar.py`):

```
GET  /books                      [{id,title,authors}]          audiobooks only
GET  /books/{id}                 book + chapters [{assetId,title,startMs,endMs,durationMs}]
GET  /books/{id}/state           {assetId,positionMs,percentage} | null
PUT  /books/{id}/state           {"assetId":"...","positionMs":1234}   (sidecar adds revision/op fields)
GET  /track/{bookId}/{assetId}   audio/mpeg, optional ?from_ms=N
GET  /healthz
```

Every format BookOrbit serves (M4B/M4A/MP3/OPUS/OGG/FLAC) becomes **44.1 kHz mono 48 kbps CBR MP3**.
24 kHz is not possible: the ES8311 codec accepts only 8/16/32/44.1/48 kHz, and the firmware
initialises it at 44.1 kHz, so the sidecar outputs 44.1 kHz. CBR is what makes the
device's seek math exact (6 bytes/ms) and keeps one decoder path on the firmware side.
M4B/M4A cannot be piped (MP4 needs a seekable input), so those chapters are downloaded in full first.

## 2. Firmware

Libraries: Arduino ESP32 core 2.x/3.x, **LVGL 9.3**, **ESP32-audioI2S**, **ArduinoJson 7**.
Set `WIFI_SSID`, `WIFI_PASS`, `SIDECAR` at the top of `espaudiobook.ino`, then upload.

Pins (V2): I2S MCK=2 BCK=48 LRCK=38 DOUT=47, codec at I2C addr `0x18` (SDA 11 / SCL 10),
amp enable GPIO15 high, backlight GPIO5, SDMMC CLK=14 CMD=17 D0=16 (4-bit), touch INT GPIO4,
LCD/touch reset via the TCA9554 expander (EXIO1/EXIO2). For a V1 board (PCM5101) drop the codec
init and change the pin defines — everything board-specific is in `initBoard()`.

`firmware/*.{h,c,cpp}` are Waveshare's Apache-2.0 example drivers, copied unmodified; the app is
only `espaudiobook.ino`.

UI: book list → player (title, chapter, time, progress bar, prev/play/next/back).

## 3. How a track is picked

1. Complete file on the card → `connecttoFS`, then `setAudioPlayPosition` (exact for CBR).
2. Nothing on the card → the sidecar completes the whole chapter first, then serves it with
   Content-Length, so the device stores a verifiable copy on the card while it plays. First byte lands
   a few seconds later (ffmpeg decodes faster than realtime; a chapter is ~1-2 MB at 48 kbps).
3. The card keeps only what matches that length — a half-received stream is never mistaken for a
   chapter. Prefetch runs for the next 3 tracks when nothing is decoding (paused or idle), since a
   download in the main loop would starve the decoder.
4. Progress is PUT every 15 s while playing: `chapter.startMs + elapsed`.

## Known limits

- Resume is exact for a file on the card; the `?from_ms=` path seeks by byte offset (6 bytes/ms, CBR)
  and skips the tag header, so it can be a few ms off — `find_sync` re-anchors to the next frame start.
- Prefetching is not live during playback (single loop); upgrade path is a download task on core 0.
- No battery on this board — USB-C only, backlight always on.
- BookOrbit's API is unofficial; if a route moves, only `sidecar.py` has to change.
- License: GPL-3.0 for project code (`LICENSE`); `firmware/` drivers are vendored and keep their upstream
  licenses (`THIRD-PARTY-NOTICES.md`). Six of them carry no license notice at all, so they cannot be
  relicensed, and the firmware binary is GPL-3.0 covered because it links ESP32-audioI2S.
- Not run on hardware from here: the sketch is unverified on a real board.
- The image builds and runs (healthcheck green, uid 1000, read-only root, cache write verified through the
  bind mount). Endpoints were checked against a mock BookOrbit shaped like `packages/types/src/audiobook.ts`
  (a real server was not contacted). BookOrbit's `books/query` defaults to 50 books per page.
