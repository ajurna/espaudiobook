#!/usr/bin/env python3
"""Talks to the deployed sidecar exactly like the ESP32 does: one Bearer token, the stream path with
?token= because the audio library cannot send headers, and the prefetch burst the device runs when it is
idle. Fill testclient/.env, then:

    python testclient/test_client.py

It writes playback state to the server, so use a book you do not mind touching. No dependencies, no
local server: everything goes to SIDECAR_URL.
"""
import json
import sys
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 120
PREFETCH = 3                                    # what the firmware prefetches when nothing is decoding


def load_env(path=os.path.join(HERE, ".env")):
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()
URL = os.environ.get("SIDECAR_URL", "").rstrip("/")
USER = os.environ.get("BOOKORBIT_USER", "")
PASS = os.environ.get("BOOKORBIT_PASSWORD", "")
TOKEN = os.environ.get("TEST_TOKEN", "")

RESULTS = []


DEBUG = bool(os.environ.get("DEBUG")) or "--debug" in sys.argv


def log(*a):
    if DEBUG:
        print("[client]", *a, flush=True)


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("ok   " if ok else "FAIL ") + name + (" - " + detail if detail else ""))


def call(path, method="GET", body=None, token=None, query=None, stream=False):
    tok = TOKEN if token is None else token
    params = dict(query or {})
    if stream and tok:
        params["token"] = tok                   # ESP32-audioI2S cannot add a header
    u = URL + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(u, method=method)
    if tok and not stream:
        req.add_header("authorization", "Bearer " + tok)
    data = json.dumps(body).encode() if body is not None else None
    # the stream path carries the token in the query, so keep it out of the log
    shown = "&".join(k + "=" + ("***" if k == "token" else str(v)) for k, v in params.items())
    log(f"{method} {u.split('?')[0]}" + (f"?{shown}" if shown else "") + (f"  {data.decode()}" if data else ""))
    t0 = time.perf_counter()
    try:
        r = urllib.request.urlopen(req, data, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:          # a 401 must be a result, not a crash; nginx can answer
        first = time.perf_counter() - t0          # an error that is not JSON
        raw = e.read()
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = raw[:200].decode("utf-8", "replace")
        log(f"  {e.code} {body}")
        return e.code, 0, body, first, first
    with r:
        length = int(r.headers.get("content-length") or 0)
        got = b""
        first = None                             # first body byte, not the headers - ffmpeg can answer
        while (chunk := r.read(8192)):           # the headers before its first output frame
            if first is None:
                first = time.perf_counter() - t0
            got += chunk
    if first is None:
        first = time.perf_counter() - t0
    total = time.perf_counter() - t0
    ctype = (r.headers.get("content-type") or "").split(";")[0]
    log(f"  {r.status} {len(got)} B (content-length {length}, first byte {first * 1000:.0f} ms) {ctype}")
    return r.status, length, (json.loads(got) if ctype == "application/json" else got), first, total


def row(label, path, query=None, token=None):
    status, length, body, first, total = call(path, query=query, stream=True, token=token)
    print(f"     {label:<17} first byte {first * 1000:7.1f} ms   total {total * 1000:7.1f} ms   "
          f"{len(body):6d} B   {status}" + (f"   {body}" if status != 200 else ""))
    return status, length, body, first, total


def start_ok(b):
    return isinstance(b, bytes) and (b[:3] == b"ID3" or b[:1] == b"\xff")


def cached_token():
    f = os.path.join(HERE, ".token")
    if not os.path.exists(f):
        return ""
    d = json.load(open(f))
    exp = datetime.fromisoformat(str(d.get("expires", "")).replace("Z", "+00:00"))
    tok = d.get("token", "") if exp > datetime.now(timezone.utc) else ""
    log("token " + ("from cache" if tok else f"expired {exp}, need a login"))
    return tok


def save_token(tok, expires):
    json.dump({"token": tok, "expires": expires}, open(os.path.join(HERE, ".token"), "w"))


def main():
    if not URL:
        raise SystemExit("fill SIDECAR_URL in testclient/.env")
    print(f"sidecar {URL}\n")

    check("no token -> 401", call("/books", token="")[0] == 401)

    tok = TOKEN or cached_token()
    if tok:
        print(f"token from cache (testclient/.token), no login needed")
    elif USER and PASS:
        status, _, body, first, _ = call("/auth/login", "POST",
                                         {"username": USER, "password": PASS, "clientKind": "native",
                                          "deviceLabel": "test-client"}, token="")
        check("login through the sidecar", status == 200 and body.get("accessToken"), f"{status} {str(body)[:120]}")
        tok = body.get("accessToken", "")
        if not tok:
            raise SystemExit("no token - the login throttle is 5/min, so run again or set TEST_TOKEN")
        save_token(tok, body.get("accessTokenExpiresAt"))
    else:
        raise SystemExit("set TEST_TOKEN or BOOKORBIT_USER/BOOKORBIT_PASSWORD in testclient/.env")

    status, _, health, first, _ = call("/healthz", token=tok)
    print(f"     healthz           {health} ({first * 1000:.0f} ms)")
    check("healthz", status == 200 and health.get("ffmpeg"))

    status, _, books, first, _ = call("/books", token=tok)
    check("book list has an audiobook", status == 200 and books, f"{len(books)} books, {first * 1000:.0f} ms")
    if not (isinstance(books, list) and books):
        raise SystemExit(f"no audiobooks to test - {books}")
    book = books[0]["id"]

    status, _, manifest, first, _ = call(f"/books/{book}", token=tok)
    check("manifest", status == 200 and manifest.get("chapters"), f"{len(manifest.get('chapters', []))} chapters")
    asset = manifest["chapters"][0]["assetId"]
    print(f"\nbook {book} ({manifest.get('title')}), chapter 1 asset {asset}")

    status, _, state, first, _ = call(f"/books/{book}/state", token=tok)
    print(f"     state GET          {state} ({first * 1000:.0f} ms)")
    status, _, saved, first, _ = call(f"/books/{book}/state", "PUT",
                                      {"assetId": asset, "positionMs": 1000}, token=tok)
    check("state PUT accepted", status == 200 and saved.get("assetId") == asset, f"{first * 1000:.0f} ms")

    print("\ncold track (the whole chapter is finished before the first byte):")
    cold_status, cold_len, cold_got, cold_first, cold_total = row("cold", f"/track/{book}/{asset}", {"chapter": 0}, token=tok)
    check("cold content-length matches what arrived", cold_len == len(cold_got), f"{cold_len} vs {len(cold_got)}")
    check("cold starts where the decoder can read", start_ok(cold_got), repr(cold_got)[:60])

    print("\nwarm track (cache hit):")
    warm_status, warm_len, warm_got, warm_first, warm_total = row("warm", f"/track/{book}/{asset}", {"chapter": 0}, token=tok)
    check("warm content-length matches", warm_len == len(warm_got), f"{warm_len} vs {len(warm_got)}")
    check("warm beats cold on first byte", warm_first < cold_first,
          f"{warm_first * 1000:.1f} ms vs {cold_first * 1000:.1f} ms")

    mid = int(manifest["chapters"][0]["durationMs"] / 2) or 3000
    r_status, r_len, r_got, r_first, r_total = row(f"resume {mid}ms", f"/track/{book}/{asset}",
                                                   {"chapter": 0, "from_ms": mid}, token=tok)
    check("resume lands on a sync word", isinstance(r_got, bytes) and r_got[:1] == b"\xff")
    check("resume length is the whole file so the device will not cache a partial stream",
          r_len == cold_len and len(r_got) < r_len, f"{r_len} vs {len(r_got)}")

    print(f"\nprefetch burst - {PREFETCH + 1} chapters at once (what the device does when idle or paused):")
    rows = []
    t0 = time.perf_counter()
    for i, ch in enumerate(manifest["chapters"][:PREFETCH + 1]):
        status, length, body, first, total = row(f"ch{i + 1}", f"/track/{book}/{ch['assetId']}", {"chapter": i}, token=tok)
        rows.append((ch["assetId"], first, total, len(body), status, length))
    wall = time.perf_counter() - t0
    best, worst = min(r[1] for r in rows), max(r[1] for r in rows)
    print(f"     first byte     {best * 1000:6.1f} ms best / {worst * 1000:6.1f} ms worst   wall {wall * 1000:6.1f} ms")
    check("every burst request answered", all(r[4] == 200 for r in rows), str([r[4] for r in rows]))
    check("every burst body matched its chapter length", all(r[3] == r[5] for r in rows),
          str([(r[3], r[5]) for r in rows]))

    print("\nsummary")
    firsts = [r[1] for r in rows] + [cold_first, warm_first, r_first]
    total_time = sum(r[2] for r in rows) + cold_total + warm_total + r_total
    bytes_served = sum(r[3] for r in rows) + len(cold_got) + len(warm_got) + len(r_got)
    print(f"  first byte: {min(firsts) * 1000:.1f} ms best, {max(firsts) * 1000:.1f} ms worst")
    print(f"  audio served: {bytes_served} B in {total_time:.1f} s -> {bytes_served / total_time / 1024:.0f} kB/s")
    slowest = max([(r[0], r[1], r[2], r[3], r[4]) for r in rows] +
                  [("cold", cold_first, cold_total, len(cold_got), cold_status),
                   ("warm", warm_first, warm_total, len(warm_got), warm_status),
                   ("resume", r_first, r_total, len(r_got), r_status)], key=lambda x: x[1])
    print(f"  slowest request: {slowest[0]} {slowest[1] * 1000:.1f} ms first byte")

    bad = [r for r in RESULTS if not r[1]]
    if bad:
        print("\nFAILED: " + ", ".join(r[0] for r in bad))
        raise SystemExit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
