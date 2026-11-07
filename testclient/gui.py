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
ROWS, ROW_STEP, ROW_TOP = 6, 24, 40   # rows live above the buttons, so rows and buttons cannot overlap


def author_of(b):
    return (b.get("authors") or [b.get("author") or "(unknown)"])[0]


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
        self.all_books = []
        self.authors, self.author, self.view, self.state = [], None, "authors", None
        self.cursor, self.stack = 0, []
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

    def button(self, label, action, dx=0, dy=0):
        if not self.root:
            return
        w = row_width(R + dy)
        if w < 60:
            self.status = f"{label} does not fit at y={dy} (only {w:.0f} px)"
            return
        x, y = SIZE // 2 + dx, R + dy
        item = self.cv.create_rectangle(x - 30, y - 15, x + 30, y + 15, fill="#223", outline="#668", tags="ui")
        self.cv.create_text(item, y, text=label, fill="#cde", font=("TkDefaultFont", 12), tags="ui")
        self.cv.tag_bind(item, "<Button-1>", action)

    def page(self, items, open_item):
        base = (self.cursor // ROWS) * ROWS
        for i in range(ROWS):
            n = base + i
            if n >= len(items):
                break
            w = row_width(ROW_TOP + i * ROW_STEP)
            if w < 120:
                break
            label = items[n].get("author") or items[n].get("title") or str(items[n])
            item = self.cv.create_text(SIZE // 2, ROW_TOP + i * ROW_STEP, text=label[:int(w / 12)],
                                       fill="#8cf" if n == self.cursor else "#ddd", font=("TkDefaultFont", 12),
                                       tags="ui")
            if self.root:
                self.cv.tag_bind(item, "<Button-1>", lambda e, k=n: (setattr(self, "cursor", k), open_item(items[k])))

    def draw(self):
        if not self.root:
            return
        self.cv.delete("ui")
        self.status = ""
        if self.view == "authors":
            self.page(self.authors, self.pick)
            self.cv.create_text(SIZE // 2, 20, text="authors", fill="#888", font=("TkDefaultFont", 12), tags="ui")
        elif self.view == "books":
            self.page(self.books, self.open_book)
            self.cv.create_text(SIZE // 2, 20, text="books", fill="#888", font=("TkDefaultFont", 12), tags="ui")
        else:
            self.text(40, self.book["title"], 14)
            self.text(90, self.chapters[self.idx]["title"] if self.chapters else "", 12, "#8cf")
            self.text(120, fmt(self.pos_ms), 12, "#8cf")
            pct = int(self.pos_ms * 100 / max(sum(ch["endMs"] - ch["startMs"] for ch in self.chapters), 1))
            self.cv.create_arc(SIZE // 2 - 70, 150, SIZE // 2 + 70, 290, start=90 - pct * 3.6, extent=pct * 3.6,
                               style="arc", outline="#68a", width=3, tags="ui")
            if self.status:
                self.cv.create_text(SIZE // 2, 316, text=self.status, fill="#a66", font=("TkDefaultFont", 10),
                                    tags="ui")
        for label, dx, dy in (("up", -100, 40), ("down", 100, 40), ("go", 0, 110), ("back", 0, 140)):
            self.button(label, lambda e, l=label: self.act(l), dx, dy)

    def act(self, what):
        if what == "back":
            self.view = self.stack.pop() if self.stack else "authors"
            if self.view == "authors":
                self.book = None
        elif what in ("up", "down"):
            if self.view == "player":
                self.act("prev" if what == "up" else "next")
                return
            items = self.authors if self.view == "authors" else self.books
            if items:
                self.cursor = max(0, min(len(items) - 1, self.cursor + (-1 if what == "up" else 1)))
        elif what == "go":
            if self.view == "player":
                self.act("play")
                return
            items = self.authors if self.view == "authors" else self.books
            if items:
                (self.pick if self.view == "authors" else self.open_book)(items[self.cursor])
                return
        elif what == "play":
            self.playing = not self.playing
            if self.playing:
                if self.root:
                    self.play_chapter()
                else:
                    self.status = "play (skipped, headless)"
            elif self.device:
                self.device.stop()
            self.report()
        elif what in ("prev", "next"):
            if not self.chapters:
                return
            self.idx += -1 if what == "up" or what == "prev" else 1
            self.idx = max(0, min(len(self.chapters) - 1, self.idx))
            self.cursor = self.idx
            self.pos_ms = self.chapters[self.idx]["startMs"]
            if self.playing and self.root:
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
        self.state = c.call("/state", token=self.token)[2]
        self.author = (self.state or {}).get("author") or None
        cached = c.call("/books", token=self.token)[2]
        lib = c.call("/library", token=self.token)[2] or c.call("/books", token=self.token)[2]
        cached = cached if isinstance(cached, list) else []
        lib = lib if isinstance(lib, list) else []
        rows = cached or lib            # the cache is the list; an empty cache still shows the catalog
        counts = {}
        for b in rows:
            a = author_of(b)
            counts[a] = counts.get(a, 0) + 1
        self.authors = [{"author": a, "books": counts[a]} for a in sorted(counts)]
        self.books = [b for b in rows if not self.author or author_of(b) == self.author]
        self.all_books = rows
        cur = (self.state or {}).get("book")
        self.book = next((b for b in rows if str(b["id"]) == str(cur)), None)
        if self.book:
            st, _, man, _, _ = c.call(f"/books/{self.book['id']}", token=self.token)
            self.chapters = man.get("chapters", []) if isinstance(man, dict) else []
            self.pos_ms = 0

    def pick(self, a):
        name = a["author"]
        c.call("/state", "PUT", {"author": name}, token=self.token)
        self.author = name
        self.books = [b for b in self.all_books if author_of(b) == name]
        self.goto("books")

    def open_book(self, b):
        c.call("/state", "PUT", {"book": b["id"], "author": self.author}, token=self.token)
        self.book = b
        self.idx = 0
        self.cursor = 0
        man = c.call(f"/books/{b['id']}", token=self.token)[2]
        self.chapters = man.get("chapters", []) if isinstance(man, dict) else []
        self.pos_ms = self.chapters[self.idx]["startMs"] if self.chapters else 0
        self.goto("player")

    def goto(self, view):
        self.stack.append(self.view)
        self.view = view
        self.cursor = 0

    def nav(self):
        """Headless walk through the views using only the on-screen control names."""
        self.fetch()
        for what in ("down", "go", "down", "go", "next", "go", "back", "back"):
            self.act(what)
            items = self.authors if self.view == "authors" else (self.books if self.view == "books" else self.chapters)
            cur = items[self.cursor] if items and self.cursor < len(items) else None
            name = (cur.get("author") or cur.get("title")) if cur else "-"
            print(f"  {what:<5} {self.view:<8} {name} {self.status}")
            self.status = ""

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
    g = Gui(headless="--check" in sys.argv or "--nav" in sys.argv)
    if "--nav" in sys.argv:
        g.nav()
    elif g.root is None:
        g.check()
    else:
        g.fetch()
        g.draw()
        g.act("nothing")
        g.root.mainloop()
