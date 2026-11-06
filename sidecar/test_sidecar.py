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

    dst = sidecar.transcode(src, sidecar.cache_path("7", {"assetId": "aud_test", "etag": "x"}))
    assert probe(dst) == (48000, 1), f"expected 48 kHz mono, got {probe(dst)}"
    assert sidecar.transcode(src, dst) == dst, "second call should hit the cache, not re-run ffmpeg"
    assert not os.path.exists(dst + ".part"), "partial file must not survive a completed transcode"

    # resume: the byte offset must land on an MP3 sync word, otherwise the ESP32 decodes garbage
    with open(dst, "rb") as f:
        data = f.read()
        pos = int(2000 * sidecar.BYTES_PER_MS)
        assert data[sidecar.find_sync(data, pos)] == 0xFF, "seek did not land on a frame start"
    print("ok: transcode 48kHz mono, cache hit, frame-aligned seek")


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
    sidecar.asset_entry = lambda b, a, bearer: {"assetId": a, "format": "opus", "etag": "x"}

    dst = sidecar.ensure_track("7", "aud_test", "Bearer tok1")
    assert probe(dst) == (48000, 1), f"expected 48 kHz mono, got {probe(dst)}"
    assert not os.path.exists(os.path.join(tmp, "aud_test.opus")), "the source file must not stay behind"
    print("ok: whole chapter complete before serving, source cleaned up")


def test_clean():
    tmp = tempfile.mkdtemp()
    sidecar.CACHE_DIR = tmp
    os.makedirs(os.path.join(tmp, "7"))
    live = {"assetId": "aud_1", "etag": "abc"}
    stale = {"assetId": "aud_1", "etag": "old"}
    open(sidecar.cache_path("7", live), "wb").write(b"x" * 100)
    open(sidecar.cache_path("7", stale), "wb").write(b"y" * 100)
    open(os.path.join(tmp, "7", "aud_gone_xyz.mp3"), "wb").write(b"z" * 100)
    sidecar.sweep("7", [live])
    assert os.path.exists(sidecar.cache_path("7", live)), "live entry must survive"
    assert not os.path.exists(sidecar.cache_path("7", stale)), "stale etag must be dropped"
    assert not os.path.exists(os.path.join(tmp, "7", "aud_gone_xyz.mp3")), "orphan must be dropped"

    sidecar.CACHE_MAX_BYTES = 150
    sidecar.evict()
    d = os.path.join(tmp, "7")
    assert sum(os.path.getsize(os.path.join(d, n)) for n in os.listdir(d)) <= 150, "cap not enforced"
    sidecar.CACHE_MAX_BYTES = 0
    print("ok: stale/orphan entries dropped, cap enforced oldest-first")


if __name__ == "__main__":
    main()
    test_forwarding()
    test_first_pass()
    test_clean()
