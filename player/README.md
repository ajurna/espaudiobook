# player

Audiobook player for the Waveshare ESP32-S3-Touch-LCD-1.85C. Copied from
`WS-1.85C/01-Music & Internet Radio Player/Player-PIO` and cut down to one job: browse
BookOrbit books through the sidecar, play a chapter, cache chapters on the card.

## What changed vs the original

- `WebRadio.*` / `RadioStations.h` gone — the mode button now switches **book list ↔ chapter list**.
- `AudioPlayer.*` rewritten around book/chapter state instead of a flat MP3 list.
- New `Sidecar.*`: login, GET, PUT, download-to-card, and the stream URL.
- Progress is not stored on the device — every `SAVE_EVERY_SECONDS` it PUTs to the sidecar, which
  passes it to BookOrbit. Reopen a book and it resumes where you left off.

## Flow

1. First request logs in (`POST /auth/login`, token kept in RAM only).
2. Roller shows `GET /library`. Tap a book → `GET /books/{id}` + `GET /books/{id}/state` → the
   chapter holding `positionMs` is selected.
3. Play: the file on the card if it is cached, otherwise the stream
   (`GET /track?book=&chapter=&from_ms=&token=` — the audio library cannot send a header, so the
   token rides in the URL).
4. The cache task downloads the **next** chapter while you listen. Cache lives in `/cache/<bookId>/`
   with `cache/index.txt` (`<path> <bytes>`, insert order); over `CACHE_MAX_KB` the oldest entry is
   dropped, matching the sidecar's rule.
5. Resuming mid-chapter seeks by bytes (`BYTES_PER_MS`), re-anchored to an MP3 sync word like the
   sidecar's `find_start`.

## Config (`src/config.h`)

`SIDECAR_HOST`, `SIDECAR_PORT`, `BOOKORBIT_USER`, `BOOKORBIT_PASS`, `CACHE_ROOT`, `CACHE_INDEX`,
`CACHE_MAX_KB`, `SAVE_EVERY_SECONDS`, `BYTES_PER_MS`. Wi-Fi and the original display/volume defines
are unchanged.

## Build

```
pio run -e V1        # PCM5101 (this board); -e V2 is the ES8311 build
```

`lib/lvgl` and `lib/ESP32-audioI2S-master` are junctions to `WS-1.85C/00-❗libraries/` — PlatformIO
can't read a `file://` dep path containing `❗`, so the links are the workaround. Recreate them with
`New-Item -ItemType Junction` if you clone without them.

## On hardware, check these

- Token lifetime: the device logs in once and never refreshes.
- The cache task writes the card while the decoder reads it — SD bus contention is the likely first
  glitch (dropouts while prefetching).
- Chapter list is capped at `MAX_CHAPTERS` (300), books at `MAX_BOOKS` (40), roller shows 20.
