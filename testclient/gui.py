"""Mock of the device UI: 320x320 with the circular window, so you can see what actually fits on it.

    uv run --project testclient python testclient/gui.py            # window
    uv run --project testclient python testclient/gui.py --check    # no window: geometry + one chapter

Live data from testclient/.env, token cached in testclient/.token (shared with test_client.py).
The GUI plays the fetched chapter with miniaudio and PUTs playback state every 15 s, exactly like the
firmware, so the server sees the same pattern the device produces.
"""
import math
import os
import sys
import tempfile
import time
import tkinter as tk

import miniaudio as ma
import test_client as c

SIZE = 320
R = SIZE // 2                    # usable radius: the physical window is a circle
REPORT_EVERY_MS = 15000          # same cadence as the firmware


def row_width(y, h=20):
    """Widest row that stays inside the circle when its centre is at y (firmware uses 300 px rows)."""
    dy = abs(y - R)
    return 2 * math.sqrt(R * R - dy * dy) if dy < R else 0


class Gui:
    def __init__(self, headless=False):
        self.root = tk.Tk() if not headless else None
        if self.root:
            self.root.title("espaudiobook mock")
            self.cv = tk.Canvas(self.root, width=SIZE, height=SIZE, bg="black", highlightthickness=0)
            self.cv.pack()
            # the corners are not visible on the real panel - draw the window so you can see the limit
            self.cv.create_oval(0, 0, SIZE, SIZE, fill="#111", outline="#444")
        self.books, self.chapters, self.book = [], [], None
        self.authors, self.author, self.view, self.state = [], None, "authors", None
        self.idx = 0                       # chapter being played
        self.pos_ms = 0
        self.playing = False
        self.device = None
        self.stream = None
        self.started = 0.0
        self.token = c.cached_token() or self.login()
        self.status = ""

    def login(self):
        st, _, body, _, _ = c.call("/auth/login", "POST",
                                   {"username": c.USER, "password": c.PASS,
                                    "clientKind": "native", "deviceLabel": "test-gui"})
        if st != 200:
            raise SystemExit(f"login failed ({st}): {body}")
        c.save_token(body["accessToken"], body["accessTokenExpiresAt"])
        return body["accessToken"]

    def text(self, y, s, size=14, color="#ddd"):
        if not self.root:
            return
        self.cv.create_text(SIZE // 2, y, text=s[:int(row_width(y)) // size], fill=color,
                            font=("TkDefaultFont", size))

    def button(self, y, label, action):
        w = min(64, row_width(y))          # a 64 px button near the circle edge gets clipped
        if w < 40:
            self.status = f"{label} does not fit at y={y} (only {w:.0f} px)"
            return
        x = SIZE // 2
        item = self.cv.create_rectangle(x - w / 2, y - 16, x + w / 2, y + 16, fill="#223", outline="#668",
                                        tags="ui")
        self.cv.create_text(item, y, text=label, fill="#cde", font=("TkDefaultFont", 12), tags="ui")
        if self.root:
            self.cv.tag_bind(item, "<Button-1>", action)

    def draw(self):
        if not self.root:
            return
        self.cv.delete("ui")
        self.status = ""
        if self.view == "authors":
            for i, a in enumerate(self.authors[:8]):
                y = 40 + i * 24
                if row_width(y) < 120:
                    break
                item = self.cv.create_text(SIZE // 2, y,
                                           text=f"{a['author']} ({a['books']})"[:int(row_width(y) / 12)],
                                           fill="#ddd", font=("TkDefaultFont", 12), tags="ui")
                if self.root:
                    self.cv.tag_bind(item, "<Button-1>", lambda e, n=a["author"]: self.pick(n))
            self.cv.create_text(SIZE // 2, 16, text="authors", fill="#888", font=("TkDefaultFont", 12), tags="ui")
        elif self.book is None:
            for i, b in enumerate(self.books[:8]):
                y = 40 + i * 24
                if row_width(y) < 120:
                    break
                item = self.cv.create_text(SIZE // 2, y, text=f"{i + 1}. {b['title'][:int(row_width(y) / 12)]}",
                                           fill="#ddd", font=("TkDefaultFont", 12), tags="ui")
                if self.root:
                    self.cv.tag_bind(item, "<Button-1>", lambda e, bk=b: self.open_book(bk))
            self.cv.create_text(SIZE // 2, 16, text="books", fill="#888", font=("TkDefaultFont", 12), tags="ui")
        else:
            self.text(40, self.book["title"], 14)
            self.text(90, self.chapters[self.idx]["title"] if self.chapters else "", 12, "#8cf")
            self.text(120, fmt(self.pos_ms), 12, "#8cf")
            pct = int(self.pos_ms * 100 / max(sum(ch["endMs"] - ch["startMs"] for ch in self.chapters), 1))
            self.cv.create_arc(SIZE // 2 - 70, 150, SIZE // 2 + 70, 290, start=90 - pct * 3.6, extent=pct * 3.6,
                               style="arc", outline="#68a", width=3, tags="ui")
            for label, y in (("prev", 230), ("play", 270), ("next", 300)):
                self.button(y, label, lambda e, l=label: self.act(l))
            self.button(190, "back", lambda e: self.act("back"))
            if self.status:
                self.cv.create_text(SIZE // 2, 316, text=self.status, fill="#a66", font=("TkDefaultFont", 10),
                                    tags="ui")

    def act(self, what):
        if what == "back":
            self.book = None
            self.view = "authors"
        elif what == "play":
            self.playing = not self.playing
            if self.playing:
                self.play_chapter()
            elif self.device:
                self.device.stop()
            self.report()
        elif what in ("prev", "next"):
            if not self.chapters:
                return
            self.idx += -1 if what == "prev" else 1
            self.idx = max(0, min(len(self.chapters) - 1, self.idx))
            self.pos_ms = self.chapters[self.idx]["startMs"]
            if self.playing:
                self.play_chapter()
        self.draw()
        if self.root:
            self.root.after(1000, self.tick)

    def play_chapter(self):
        ch = self.chapters[self.idx]
        st, length, body, first, total = c.call(f"/track/{self.book['id']}/{ch['assetId']}",
                                                stream=True, token=self.token,
                                                query={"chapter": self.idx})
        if st != 200 or not isinstance(body, bytes):
            self.status = f"track failed: {st} {body}"
            return
        path = os.path.join(tempfile.gettempdir(), "gui_track.mp3")
        open(path, "wb").write(body)
        if self.device is None:
            self.device = ma.PlaybackDevice()
        self.stream = ma.stream_file(path, frames_to_read=1024)
        self.device.stop()
        self.device.start(self.stream)
        self.started = time.perf_counter()
        self.status = f"first byte {first * 1000:.0f} ms, {len(body)} B (cached={length == len(body)})"

    def tick(self):
        if self.playing:
            self.pos_ms += 1000
            if int(time.perf_counter() - self.started) % 15 == 0:
                self.report()
            self.draw()
            self.root.after(1000, self.tick)

    def report(self):
        if not self.chapters:
            return
        c.call(f"/books/{self.book['id']}/state", "PUT",
               {"assetId": self.chapters[self.idx]["assetId"], "positionMs": self.pos_ms}, token=self.token)

    def fetch(self):
        st, _, state, _, _ = c.call("/state", token=self.token)
        self.state = state
        self.author = (state or {}).get("author") or None
        st, _, books, _, _ = c.call("/books", token=self.token)
        if st != 200:
            raise SystemExit(f"books failed: {st} {books}")
        self.books = [b for b in books if b["author"] == self.author] if self.author else books
        st, _, self.authors, _, _ = c.call("/authors", token=self.token)
        cur = (state or {}).get("book")
        self.book = next((b for b in self.books if str(b["id"]) == str(cur)), None)
        if self.book:
            st, _, man, _, _ = c.call(f"/books/{self.book['id']}", token=self.token)
            self.chapters = man.get("chapters", [])
            self.pos_ms = 0

    def pick(self, name):
        c.call("/state", "PUT", {"author": name}, token=self.token)
        self.author = name
        self.view = "books"

    def open_book(self, b):
        c.call("/state", "PUT", {"book": b["id"], "author": self.author}, token=self.token)
        self.book = b
        self.idx = 0
        self.view = "player"

    def check(self):
        print(f"circle radius {R} px (320x320 panel), firmware row is 300 px, buttons 64 px")
        for y in (40, 90, 120, 190, 230, 270, 300, 316):
            w = row_width(y)
            print(f"  y={y:<4} widest row {w:5.0f} px" + ("   <- a 300 px row is clipped" if w < 300 else ""))
        self.fetch()
        print(f"\nauthors: {len(self.authors)}, books cached for this author: {len(self.books)}")
        print(f"state: author {self.state and self.state['author']!r}, book {self.state and self.state['book']!r}")
        print(f"\nbook {self.book['id']} ({self.book['title']}), {len(self.chapters)} chapters")
        st, length, body, first, total = c.call(f"/track/{self.book['id']}/{self.chapters[0]['assetId']}",
                                                stream=True, token=self.token, query={"chapter": 0})
        print(f"chapter 0: {st}, {len(body)} B, first byte {first * 1000:.0f} ms, "
              f"cached={length == len(body)}")


def fmt(ms):
    s = int(ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


if __name__ == "__main__":
    g = Gui(headless="--check" in sys.argv)
    if g.root is None:
        g.check()
    else:
        g.fetch()
        g.draw()
        g.act("nothing")
        g.root.mainloop()
