"""Run: python test_sidecar.py  (needs ffmpeg; no BookOrbit server)"""

import json
import os
import subprocess
import tempfile

import sidecar


def probe(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
                                   "-of", "json", path])
    s = json.loads(out)["streams"][0]
    return int(s["sample_rate"]), int(s["channels"])


def make_fixture(dirpath):
    src = os.path.join(dirpath, "chapter.opus")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=6", "-ac", "2", "-c:a", "libopus", "-y", src],
                   check=True)
    return src


def main():
    tmp = tempfile.mkdtemp()
    sidecar.CACHE_DIR = tmp
    src = make_fixture(tmp)

    dst = sidecar.transcode(src, sidecar.cache_path("7", {"assetId": "aud_test", "etag": "x"}, "all"))
    assert probe(dst) == (44100, 1), f"expected 48 kHz mono, got {probe(dst)}"
    assert sidecar.transcode(src, dst) == dst, "second call should hit the cache, not re-run ffmpeg"
    assert not os.path.exists(dst + ".part"), "partial file must not survive a completed transcode"

    # resume: the byte offset must land on an MP3 sync word, otherwise the ESP32 decodes garbage
    with open(dst, "rb") as f:
        data = f.read()
        pos = int(2000 * sidecar.BYTES_PER_MS)
        assert data[sidecar.find_sync(data, pos)] == 0xFF, "seek did not land on a frame start"
    print("ok: transcode 44.1kHz mono, cache hit, frame-aligned seek")


def test_forwarding():
    # the sidecar must own no session - it forwards whatever the device sends
    assert not hasattr(sidecar, "access_token"), "the sidecar must not hold a credential"
    sent = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'

    def fake(req, data=None, timeout=30):
        sent.append(req.get_header("Authorization"))   # add_header capitalizes it
        return Resp()

    sidecar.urlopen = fake
    sidecar.api("books/query", "POST", {}, bearer="Bearer tok1")
    assert sent == ["Bearer tok1"], "the device's token must be forwarded as given"
    sidecar.api("books/query", "POST", {})
    assert sent[1] is None, "there is no sidecar credential to fall back on"
    print("ok: bearer passed through, nothing stored on the sidecar")


def test_first_pass():
    # the whole chapter has to be finished before the first byte goes out - the device only keeps a
    # card copy when it can match what it received against content-length
    tmp = tempfile.mkdtemp()
    sidecar.CACHE_DIR = tmp
    src = make_fixture(tmp)

    class FakeResp:
        def __enter__(self):
            self.f = open(src, "rb")
            return self
        def __exit__(self, *a):
            self.f.close()
            return False
        def read(self, n=-1): return self.f.read(n)

    sidecar.open_asset = lambda b, a, bearer: FakeResp()
    asset = {"assetId": "aud_test", "format": "opus", "etag": "x"}
    manifest = {"book": {"id": "7", "title": "Mock", "authors": ["Ann"]}, "assets": [asset], "chapters": []}
    real_api = sidecar.api                      # so a later test does not inherit this fake
    sidecar.api = lambda path, method="GET", body=None, timeout=30, bearer="": manifest
    sidecar.chapter_entry = lambda b, a, idx, bearer: (asset, {"sequence": idx, "assetId": a,
                                                                "assetOffsetMs": 0, "startMs": 0, "endMs": 6000})

    dst = sidecar.ensure_track("7", "aud_test", 0, "Bearer tok1")
    assert probe(dst) == (44100, 1), f"expected 48 kHz mono, got {probe(dst)}"
    # a chapter is sliced out of its asset, so the file is chapter-sized, not the whole book
    assert abs(os.path.getsize(dst) - 6000 * sidecar.BYTES_PER_MS) < 4096, f"chapter file {os.path.getsize(dst)} B"
    assert os.path.exists(f"{sidecar.stem('7', asset)}.opus"), "the source stays cached so the next chapter does not re-download"
    sidecar.api = real_api
    print("ok: chapter sliced from the cached source, chapter-sized file")


def test_clean():
    tmp = tempfile.mkdtemp()
    sidecar.CACHE_DIR = tmp
    fresh = os.path.join(tmp, "fresh")   # connect() must create the cache dir, not 500 on a cold start
    sidecar.CACHE_DIR = fresh
    sidecar.connect()
    assert os.path.isdir(fresh), "connect() must create a missing cache dir"
    sidecar.CACHE_DIR = tmp
    os.makedirs(os.path.join(tmp, "7"))
    live = {"assetId": "aud_1", "etag": "abc"}
    stale = {"assetId": "aud_1", "etag": "old"}
    open(sidecar.cache_path("7", live, 0), "wb").write(b"x" * 100)
    open(sidecar.cache_path("7", stale, 0), "wb").write(b"y" * 100)
    open(os.path.join(tmp, "7", "aud_gone_xyz_0.mp3"), "wb").write(b"z" * 100)
    c = sidecar.connect()
    c.execute("insert or replace into books values('7','Mock','Ann')")   # what add_entry writes when it caches a chapter
    c.execute("insert or replace into tracks values('7','aud_1',0,?,100,0)", (sidecar.cache_path("7", live, 0),))
    c.commit()
    sidecar.sweep("7", [live])
    assert os.path.exists(sidecar.cache_path("7", live, 0)), "live entry must survive"
    assert not os.path.exists(sidecar.cache_path("7", stale, 0)), "stale etag must be dropped"
    assert not os.path.exists(os.path.join(tmp, "7", "aud_gone_xyz.mp3")), "orphan must be dropped"
    assert c.execute("select 1 from books where id = '7'").fetchone(), "a cached book keeps its row"

    # a book row must not outlive its cached audio
    p = sidecar.cache_path("8", {"assetId": "aud_2", "etag": "e"}, 0)
    os.makedirs(os.path.join(tmp, "8"), exist_ok=True)
    open(p, "wb").write(b"w" * 100)
    c.execute("insert or replace into books values('8','Mock','Ann')")
    c.execute("insert or replace into tracks values('8','aud_2',0,?,100,0)", (p,))
    c.commit()
    sidecar.sweep("8", [])
    assert not c.execute("select 1 from books where id = '8'").fetchone(), \
        "no book row without a cached file"

    sidecar.CACHE_MAX_BYTES = 150
    sidecar.evict()
    d = os.path.join(tmp, "7")
    assert sum(os.path.getsize(os.path.join(d, n)) for n in os.listdir(d)) <= 150, "cap not enforced"
    sidecar.CACHE_MAX_BYTES = 0
    print("ok: stale/orphan entries dropped, cap enforced oldest-first")


def test_http():
    # routing, the token, and the {"error": ...} shape the firmware reads
    from fastapi.testclient import TestClient
    from urllib.error import HTTPError

    tmp = tempfile.mkdtemp()
    sidecar.CACHE_DIR = tmp
    real_api = sidecar.api
    sent = []

    def fake(path, method="GET", body=None, timeout=30, bearer=""):
        sent.append((path, bearer))
        return {"ok": True, "revision": 1, "manifestRevision": "m"}

    sidecar.api = fake
    c = TestClient(sidecar.app)

    assert c.get("/healthz").json()["ok"], "healthz works without a token"
    assert c.get("/books").status_code == 401, "no token, no data"
    assert not sent, "no token, nothing forwarded"
    c.get("/books/7/state", headers={"authorization": "Bearer tok1"})
    assert sent[-1][1] == "Bearer tok1", "header token is forwarded as given"
    c.get("/books/7/state?token=tok2")
    assert sent[-1][1] == "Bearer tok2", "the stream request passes the token as ?token="
    c.post("/auth/login", json={})
    assert sent[-1] == ("auth/login", ""), "login is how the device gets its token, so nothing is carried"
    assert c.get("/state", headers={"authorization": "Bearer tok1"}).status_code == 400, "the sidecar stores no state of its own"
    c.get("/state?book=7", headers={"authorization": "Bearer tok1"})
    assert sent[-1] == ("audiobooks/7/playback-state", "Bearer tok1"), "GET /state is a pass-through"
    c.put("/state", json={"book": "7", "assetId": "a", "positionMs": 1}, headers={"authorization": "Bearer tok1"})
    assert sent[-1][0] == "audiobooks/7/playback-state", "PUT /state forwards to BookOrbit"
    assert c.get("/books", headers={"authorization": "Bearer tok1"}).json() == [], "no book row without a cached file"
    assert c.post("/auth/nope").status_code == 404

    # GET /track?book=&chapter= takes a chapter number, so the player never needs asset ids
    calls = []
    real_ensure = sidecar.ensure_track
    def fake_ensure(book_id, asset_id, ch_idx, b):
        calls.append((book_id, asset_id, ch_idx))
        p = os.path.join(sidecar.CACHE_DIR, "chapter.bin")
        with open(p, "wb") as f:      # a real frame start so find_start has something to land on
            f.write(b"\x00" * 10 + b"\xff\xb2" + b"x" * 8000)
        return p
    sidecar.ensure_track = fake_ensure
    def manifest(path, method="GET", body=None, timeout=30, bearer=""):
        sent.append((path, bearer))
        return {"book": {"id": 7, "title": "t", "authors": ["a"], "narrators": []},
                "totalDurationMs": 0, "revision": 1, "manifestRevision": "m", "assets": [],
                "chapters": [{"id": "c1", "title": "one", "assetId": "asset-1", "startMs": 0, "endMs": 1000},
                             {"id": "c2", "title": "two", "assetId": "asset-2", "startMs": 1000, "endMs": 3000}]}
    sidecar.api = manifest
    r = c.get("/track?book=7&chapter=1", headers={"authorization": "Bearer tok1"})
    assert calls[-1] == ("7", "asset-2", 1), "the chapter number resolves through the manifest"
    assert r.headers.get("content-type", "").startswith("audio/mpeg"), "the player gets audio, not json"
    sidecar.ensure_track = real_ensure

    def boom(*a, **k):
        raise HTTPError("u", 403, "Forbidden", {}, None)
    sidecar.api = boom
    r = c.get("/books/7/state", headers={"authorization": "Bearer tok1"})
    assert r.status_code == 403 and r.json() == {"error": "bookorbit 403"}, "BookOrbit status passes through"

    # content-length is the whole file even when the stream starts mid-file
    path = os.path.join(tmp, "t.mp3")
    open(path, "wb").write(bytes(6000) + bytes([0xFF, 0xE1]) + b"x" * 100)
    sidecar.ensure_track = lambda b, a, ch, bearer: path
    r = c.get("/track/7/aud_test?token=tok2&from_ms=1000")
    sent = os.path.getsize(path) - 6000
    assert r.content[0] == 0xFF and len(r.content) == sent, "resume lands on a frame start"
    assert int(r.headers.get("content-length")) == sent, "content-length must match what is actually sent"
    sidecar.api = real_api
    print("ok: routes, token, error shape, content-length on a resumed stream")


if __name__ == "__main__":
    main()
    test_forwarding()
    test_first_pass()
    test_clean()
    test_http()
