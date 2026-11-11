"""Tests for the overlay (fleetnotes/overlay/fleetnotes.html, the news-ticker strip) in a real browser engine.

Serves a copy of the overlay and fixture JSON files from a temp dir on
127.0.0.1 and loads it in headless Chrome (own throwaway profile, every other
host unresolvable so web fonts fail fast and nothing leaves the machine),
driven over the DevTools protocol on a pipe. Before the overlay's script runs,
the page gets a test clock (TEST_CLOCK, never part of the overlay): Date.now(),
performance.now(), document.timeline and the timers all follow it, it starts
at NOW, the instant every fixture is dated from, and it only moves when a test
moves it, one timer at a time, each one once the page has finished the
fetches and font loads it started. So the board after N ms of test time is the
same on any machine, under any load and however long the suite has run.

Then the tests read the board: the fixed fleet header and the ticker's
segments, each with the moment it starts to enter on the right (data-start,
animation clock in ms) and its width (data-w), so they can check the ticker
has no gaps.

Prefers CHROME_BIN, then puppeteer's chrome-headless-shell or Chrome for
Testing, then an installed Chrome or Chromium; skipped when none is found.
FLEETNOTES_CHROME_ARGS adds flags (split like a shell would), e.g.
``--no-sandbox`` on a Linux CI runner.

Run from the repository root:
    python -m unittest discover -s tests -t . -v
"""

import fcntl
import functools
import glob
import html
import json
import os
import re
import select
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleetnotes.fleet import WHO_MAX, build_feed
from fleetnotes.notes import FLEET_FILENAME, LABEL_MAX, MANUAL_FILENAME, NOTE_MAX, iso_utc, parse_rules, text_width
from fleetnotes.server import OVERLAY_FILENAME, overlay_path

OVERLAY = overlay_path()
BOARD_W = 948          # the ticker's window
HEAD_MAX = 260         # widest fixed header
CHROME_ARGS = shlex.split(os.environ.get("FLEETNOTES_CHROME_ARGS", ""))
NOW = int(time.time())  # the instant the fixtures are dated from; the page's clock starts here
CHROME_WAIT_S = 60      # safety net for one DevTools reply; the tests themselves never wait on real time


def _newest(pattern: str) -> list[str]:
    version = lambda path: [int(n) for n in re.findall(r"\d+", path)]
    return sorted(glob.glob(os.path.expanduser(pattern)), key=version, reverse=True)


def find_chrome() -> str | None:
    """CHROME_BIN, else a test build (no app of its own that the desktop could
    hand the user's clicked links to), else the installed Chrome or Chromium."""
    candidates = [
        os.environ.get("CHROME_BIN"),
        *_newest("~/.cache/puppeteer/chrome-headless-shell/*/chrome-headless-shell-*/chrome-headless-shell"),
        *_newest("~/.cache/puppeteer/chrome/*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/"
                 "Google Chrome for Testing"),
        *_newest("~/.cache/puppeteer/chrome/*/chrome-*/chrome"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
        shutil.which("chromium"), shutil.which("chromium-browser"),
    ]
    return next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


CHROME = find_chrome()

# The page's clock, installed by the DevTools protocol before any of the page's scripts. Test time
# starts at 0 (Date.now() = EPOCH_MS) and moves only through step(), one timer at a time in order;
# before and after each timer it waits until the fetches, body reads and font loads the page started
# are over, so what the page does next depends on this clock alone. A timer's error is reported as a
# browser would and the other timers still run.
TEST_CLOCK = r"""
(() => {
  const EPOCH = EPOCH_MS, SETTLE_LIMIT_MS = 20000;
  const RealDate = Date, realSetTimeout = setTimeout;
  const timers = new Map();
  let now = 0, order = 0, pending = 0;

  function schedule(fn, delay, args, repeat) {
    if (typeof fn !== 'function') throw new TypeError('test clock: a timer needs a function');
    const id = ++order, wait = Math.max(0, Number(delay) || 0);
    timers.set(id, { id, fn, args, due: now + wait, order: id, every: repeat ? Math.max(1, wait) : 0 });
    return id;
  }
  class TestDate extends RealDate {
    constructor(...args) { if (args.length) super(...args); else super(EPOCH + now); }
    static now() { return EPOCH + now; }
  }
  function track(promise) {
    pending++;
    const done = () => { pending--; };
    promise.then(done, done);
    return promise;
  }

  // Resolves once nothing has been in flight for two task turns in a row (the page's await
  // chains run in between); fails if the page is still waiting after SETTLE_LIMIT_MS of real time.
  function settle() {
    const deadline = RealDate.now() + SETTLE_LIMIT_MS;
    return new Promise((resolve, reject) => {
      let quiet = 0;
      (function check() {
        quiet = pending ? 0 : quiet + 1;
        if (quiet >= 2) return resolve(now);
        if (RealDate.now() > deadline) return reject(new Error('test clock: the page is still loading'));
        realSetTimeout(check, pending ? 5 : 0);
      })();
    });
  }

  // Runs the next timer due by "limit" (ms of test time); with none left, moves the clock to "limit".
  // Returns whether a timer ran.
  async function step(limit) {
    await settle();
    let next = null;
    for (const t of timers.values()) {
      if (t.due <= limit && (!next || t.due < next.due || (t.due === next.due && t.order < next.order))) next = t;
    }
    if (!next) { now = Math.max(now, limit); return false; }
    now = next.due;
    if (next.every) { next.due += next.every; next.order = ++order; } else timers.delete(next.id);
    try { next.fn(...next.args); } catch (e) { realSetTimeout(() => { throw e; }); }
    await settle();
    return true;
  }

  const realFetch = fetch.bind(window);
  window.fetch = (...args) => track(realFetch(...args));
  for (const name of ['json', 'text', 'arrayBuffer', 'blob']) {
    const read = Response.prototype[name];
    Response.prototype[name] = function (...args) { return track(read.apply(this, args)); };
  }
  const loadFont = FontFaceSet.prototype.load;
  FontFaceSet.prototype.load = function (...args) { return track(loadFont.apply(this, args)); };
  window.setTimeout = (fn, delay, ...args) => schedule(fn, delay, args, false);
  window.setInterval = (fn, delay, ...args) => schedule(fn, delay, args, true);
  window.clearTimeout = window.clearInterval = id => { timers.delete(id); };
  window.Date = TestDate;
  Object.defineProperty(performance, 'now', { value: () => now, configurable: true });
  Object.defineProperty(document.timeline, 'currentTime', { get: () => now, configurable: true });
  Object.defineProperty(window, '__testClock', { value: Object.freeze({ settle, step }) });
})();
"""
# What the overlay may read time from: everything TEST_CLOCK drives.
CLOCKED = {"Date.now", "Date.parse", "performance.now", "timeline.currentTime", "setTimeout", "setInterval",
           "clearTimeout", "clearInterval"}
TIME_SOURCES = re.compile(r"\bnew\s+Date\b|\bDate\.\w+|\bperformance\.\w+|\btimeline\.\w+|"
                          r"\b(?:set|clear)(?:Timeout|Interval)\b|\brequest(?:AnimationFrame|IdleCallback)\b")


def _high_fd(fd: int) -> int:
    """A copy of ``fd`` numbered 10 or more and closed on exec: the child's dup2s cannot clobber it."""
    high = fcntl.fcntl(fd, fcntl.F_DUPFD, 10)
    os.set_inheritable(high, False)
    return high


class Chrome:
    """One headless Chrome with one page on ``url``, driven over the DevTools protocol on a pipe (the
    browser reads fd 3 and writes fd 4; each message is JSON ending in NUL). The page runs on TEST_CLOCK.
    A context manager: closing it ends the browser, its helpers and its throwaway profile."""

    def __init__(self, url: str, epoch_ms: int):
        self.profile = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.stderr = tempfile.TemporaryFile()
        self.buffer, self.events, self.last_id, self.session, self.pid = bytearray(), [], 0, None, None
        to_chrome, self.write_fd = os.pipe()
        self.read_fd, from_chrome = os.pipe()
        devnull = os.open(os.devnull, os.O_RDWR)
        fds = [_high_fd(fd) for fd in (devnull, self.stderr.fileno(), to_chrome, from_chrome)]
        for fd in (devnull, to_chrome, from_chrome):
            os.close(fd)
        args = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-background-networking", "--disable-component-update",
                "--mute-audio", f"--user-data-dir={self.profile.name}",
                "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1", *CHROME_ARGS,
                "--remote-debugging-pipe", "about:blank"]
        try:
            try:
                # Its own process group, so close() also ends the helpers it starts.
                self.pid = os.posix_spawn(CHROME, args, os.environ, setpgroup=0, file_actions=[
                    (os.POSIX_SPAWN_DUP2, fds[0], 0), (os.POSIX_SPAWN_DUP2, fds[0], 1),
                    (os.POSIX_SPAWN_DUP2, fds[1], 2), (os.POSIX_SPAWN_DUP2, fds[2], 3),
                    (os.POSIX_SPAWN_DUP2, fds[3], 4)])
            finally:
                for fd in fds:   # only the browser keeps its ends, so its exit reads as the pipe's end
                    os.close(fd)
            self._open(url, epoch_ms)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _open(self, url, epoch_ms):
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Page.enable")
        self.call("Page.setLifecycleEventsEnabled", {"enabled": True})
        clock = TEST_CLOCK.replace("EPOCH_MS", str(epoch_ms))
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": clock})
        nav = self.call("Page.navigate", {"url": url})
        if nav.get("errorText"):
            raise RuntimeError(f"Chrome could not load the overlay: {nav['errorText']}")
        self.wait_for("Page.lifecycleEvent",
                      lambda p: p.get("name") == "load" and p.get("loaderId") == nav.get("loaderId"))

    def _fail(self, why: str):
        self.stderr.seek(0)
        tail = self.stderr.read()[-2000:].decode(errors="replace")
        raise RuntimeError(f"Chrome: {why}" + (f"\n{tail}" if tail else ""))

    def _send(self, method, params=None) -> int:
        self.last_id += 1
        message = {"id": self.last_id, "method": method, "params": params or {}}
        if self.session:
            message["sessionId"] = self.session
        data = json.dumps(message).encode() + b"\0"
        try:
            while data:
                data = data[os.write(self.write_fd, data):]
        except OSError as e:
            self._fail(f"the DevTools pipe is closed ({e})")
        return self.last_id

    def _receive(self, deadline) -> dict:
        while b"\0" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                self._fail(f"no answer in {CHROME_WAIT_S} s")
            if select.select([self.read_fd], [], [], left)[0]:
                chunk = os.read(self.read_fd, 1 << 16)
                if not chunk:
                    self._fail("the browser closed the DevTools pipe")
                self.buffer += chunk
        end = self.buffer.index(b"\0")
        message = json.loads(self.buffer[:end])
        del self.buffer[:end + 1]
        return message

    def call(self, method: str, params: dict | None = None) -> dict:
        sent = self._send(method, params)
        deadline = time.monotonic() + CHROME_WAIT_S
        while True:
            message = self._receive(deadline)
            if message.get("id") == sent:
                if "error" in message:
                    self._fail(f"{method}: {message['error'].get('message')}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)

    def wait_for(self, method: str, matches) -> dict:
        deadline = time.monotonic() + CHROME_WAIT_S
        while True:
            for event in self.events:
                if event["method"] == method and matches(event.get("params", {})):
                    self.events.remove(event)
                    return event["params"]
            self.events.append(self._receive(deadline))

    def evaluate(self, expression: str):
        """The value of ``expression`` in the page, once its promise (if any) settles."""
        result = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                "returnByValue": True})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            self._fail(details.get("exception", {}).get("description") or details.get("text", "error"))
        return result["result"].get("value")

    def html(self) -> str:
        return self.evaluate("document.documentElement.outerHTML")

    def run(self, limit_ms: int, until=None):
        """Moves the page's clock to ``limit_ms`` of test time, one timer at a time. With ``until``, a
        test of the page's HTML, it stops at the first moment the test holds instead."""
        self.evaluate("__testClock.settle()")
        while not (until and until(self.html())) and self.evaluate(f"__testClock.step({int(limit_ms)})"):
            pass

    def close(self):
        if self.pid is not None:
            self.session = None
            try:
                self._send("Browser.close")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:   # the browser closes its end of the pipe as it exits
                    if select.select([self.read_fd], [], [], deadline - time.monotonic())[0] \
                            and not os.read(self.read_fd, 1 << 16):
                        break
            except (OSError, RuntimeError):
                pass
            try:
                os.killpg(self.pid, signal.SIGKILL)   # any helper left; not yet reaped, the group is still ours
            except (ProcessLookupError, PermissionError):
                pass
            os.waitpid(self.pid, 0)
            self.pid = None
        for fd in ("read_fd", "write_fd"):
            if getattr(self, fd, None) is not None:
                os.close(getattr(self, fd))
                setattr(self, fd, None)
        self.stderr.close()
        self.profile.cleanup()


class _QuietHandler(SimpleHTTPRequestHandler):
    # file name -> bodies served one per request (the last one repeats): data
    # that changes between the overlay's polls.
    sequences: dict = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        name = self.path.split("?", 1)[0].lstrip("/")
        bodies = self.sequences.get(name)
        if not bodies:
            return super().do_GET()
        body = bodies.pop(0) if len(bodies) > 1 else bodies[0]
        if body is None:                       # the file is gone
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def ago(seconds: float) -> str:
    return iso_utc(NOW - seconds)


def fleet(notes, summary=None, generated_ago=5):
    return {"schema": "fleetnotes-feed.v1", "generated": ago(generated_ago),
            "summary": summary or {"running": 2, "validating": 1, "needs_captain": 1, "problem": 0,
                                   "paused": 0, "done_recent": 0},
            "notes": notes}


def note(key, state="running", label=None, kind="ship", started=1080, changed=300, project="Lighthouse",
         who=None, **extra):
    return {"key": key, "project": project, "who": who or project, "label": label,
            "label_from": "title" if label else None, "kind": kind, "state": state,
            "started": ago(started), "changed": ago(changed), **extra}


def pinned(text, key="m1"):
    return {"schema": "fleetnotes-notes.v1", "off": False,
            "notes": [{"key": key, "text": text, "pinned": True, "expires": None}]}


# Five long sentences, as long as the feed allows ("who" up to 34, "what" up to 60).
LONG = [
    note("a1", "needs_captain", "Starter plan up to 3 projects and a notice to the admin…",
         project="Driftwood", who="Secondmate of Driftwood", changed=120),
    note("a2", "validating", "build 23 of the test version with the per-app counter…",
         project="Tidepool", who="Studio Mac: Tidepool", step="ci"),
    note("a3", "running", "the chat bot answers questions with the local example model"),
    note("a4", "running", "finish the missing QA for 1.5.0 without the captain's iPhone",
         project="Harbor", who="Studio Mac: Harbor"),
    note("a5", "paused", "FULL integration with Slack, Discord and Teams + WWW notices",
         project="DriftwoodKitPls1", who="Studio Mac Plus: DriftwoodKitPls1"),
]
LONG_SENTENCES = [f"{n['who']} · {n['label']}" for n in LONG]
# The widest "who" (a remote machine, 16 + ": " + 16) with an all-caps label.
WIDEST = note("a6", "running", "FULL MIGRATION OF THE WIDGET AND LIVE ACTIVITY TO THE ISLE",
              project="HarborWatchAppXY", who="Studio Mac Spare: HarborWatchAppXY")

BOARD = re.compile(r'<div class="board([^"]*)" id="board"([^>]*)>(.*?)</div>\s*<script>', re.S)
SEGMENT = re.compile(r'<div class="seg"([^>]*)>(.*?)<div class="divider"></div></div>', re.S)


@unittest.skipIf(CHROME is None, "Chrome not found (set CHROME_BIN)")
class OverlayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "public")
        os.makedirs(cls.root)
        shutil.copy(OVERLAY, cls.root)
        handler = functools.partial(_QuietHandler, directory=cls.root)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/{OVERLAY_FILENAME}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def tearDown(self):
        _QuietHandler.sequences.clear()

    def render(self, manual=None, feed=None, budget_ms=3000, query="", until=None) -> str:
        """Inner HTML of the board after ``budget_ms`` of test time (first poll at 0). With ``until``, a
        test of the board's inner HTML, it stops at the first moment the test holds instead, and
        ``budget_ms`` is only the limit."""
        for name, data in ((MANUAL_FILENAME, manual), (FLEET_FILENAME, feed)):
            path = os.path.join(self.root, name)
            if data is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    if isinstance(data, str):
                        f.write(data)  # raw text: a broken file
                    else:
                        json.dump(data, f, ensure_ascii=False)

        def board_holds(page):
            match = BOARD.search(page)
            return bool(match and until(match.group(3)))

        with Chrome(self.url + query, NOW * 1000) as chrome:
            chrome.run(budget_ms, until and board_holds)
            out = chrome.html()
        match = BOARD.search(out)
        self.assertIsNotNone(match, "board not found in the page")
        self.assertIn("ready", match.group(1))  # the first poll finished, whatever the data
        self.assertNotIn("�", match.group(3))
        self.filled = "filled" in match.group(1)
        attrs = dict(re.findall(r'(data-[a-z]+)="([^"]*)"', match.group(2)))
        self.speed = float(attrs["data-speed"])
        self.now = float(attrs["data-now"]) if "data-now" in attrs else None
        return match.group(3)

    def render_sequence(self, manual_bodies, feed_bodies, budget_ms, query=""):
        """Like render(), but each poll gets the next body (None: no file); the last one repeats."""
        encode = lambda data: None if data is None else json.dumps(data).encode()
        _QuietHandler.sequences[MANUAL_FILENAME] = [encode(d) for d in manual_bodies]
        _QuietHandler.sequences[FLEET_FILENAME] = [encode(d) for d in feed_bodies]
        return self.render(None, None, budget_ms=budget_ms, query=query)

    @staticmethod
    def text(board: str) -> str:
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", board)).split())

    @staticmethod
    def notes(board: str) -> list[str]:
        return re.findall(r'<div class="note[^"]*"[^>]*>', board)

    def head(self, board: str) -> str:
        return self.text(board.split('<div class="seg"', 1)[0])

    def segments(self, board: str) -> list[dict]:
        out = []
        for attrs, body in SEGMENT.findall(board):
            data = dict(re.findall(r'(data-[a-z]+)="([^"]*)"', attrs))
            out.append({"start": float(data["data-start"]), "w": int(data["data-w"]),
                        "text": self.text(body), "cls": re.search(r'<div class="(note[^"]*)"', body).group(1),
                        "overflow": "data-overflow" in body})
        return out

    def assertTicker(self, board, left=0) -> list[dict]:
        """No gaps: each segment starts entering exactly when the previous one is in whole, and
        at the last refill the first one covers the left edge and the last one reaches the right."""
        segs = self.segments(board)
        self.assertTrue(segs)
        step = 1000 / self.speed
        for a, b in zip(segs, segs[1:]):
            self.assertAlmostEqual(b["start"], a["start"] + a["w"] * step, delta=0.01)
        x = lambda s: BOARD_W - (self.now - s["start"]) / step
        self.assertLessEqual(x(segs[0]), left + 1)
        self.assertGreaterEqual(x(segs[-1]) + segs[-1]["w"], BOARD_W)
        for s in segs:
            self.assertFalse(s["overflow"], s["text"])
        return segs

    def assertBlank(self, board):
        self.assertEqual(self.notes(board), [])
        self.assertNotIn("head on", board)
        self.assertFalse(self.filled)

    def test_draws_nothing_without_data(self):
        self.assertBlank(self.render())

    def test_draws_nothing_with_empty_or_foreign_files(self):
        empty_summary = dict.fromkeys(("running", "validating", "needs_captain", "problem", "paused",
                                       "done_recent"), 0)
        self.assertBlank(self.render({"schema": "fleetnotes-notes.v1", "off": False, "notes": []},
                                     fleet([], empty_summary)))
        self.assertBlank(self.render({"schema": "other", "notes": [{"key": "a", "text": "hello"}]},
                                     dict(fleet([note("a1")]), schema="other")))

    def test_off_hides_everything(self):
        manual = {"schema": "fleetnotes-notes.v1", "off": True,
                  "notes": [{"key": "m1", "text": "Today: notes", "pinned": True, "expires": None}]}
        self.assertBlank(self.render(manual, fleet([note("a1")])))

    def test_stale_feed_is_hidden(self):
        self.assertBlank(self.render(None, fleet([note("a1")], generated_ago=400)))

    def test_one_short_note_repeats_so_the_strip_is_never_empty(self):
        board = self.render(None, fleet([note("a1")]))
        self.assertEqual(self.head(board), "FIRSTMATE 4 crewmates 1 waiting")
        segs = self.assertTicker(board, left=HEAD_MAX)
        self.assertGreaterEqual(len(segs), 3)              # one note is narrower than the window
        for s in segs:
            self.assertEqual(s["text"], "Lighthouse · a crewmate at work building · 18 min")
        self.assertNotIn("1/1", self.text(board))           # no page numbers in a ticker

    def test_without_the_fleet_the_ticker_starts_at_the_left_edge(self):
        board = self.render(pinned("Today: continuous ticker"))
        self.assertEqual(self.head(board), "")
        self.assertNotIn("head on", board)
        segs = self.assertTicker(board, left=0)
        self.assertIn("pinned", segs[0]["cls"])
        self.assertEqual(segs[0]["text"], "Today: continuous ticker captain's note")

    def test_every_long_sentence_is_whole_on_one_line(self):
        self.assertEqual(max(len(n["label"]) for n in LONG), LABEL_MAX)
        self.assertEqual(max(len(n["who"]) for n in LONG), WHO_MAX - 1)
        self.assertEqual((len(WIDEST["who"]), len(WIDEST["label"])), (WHO_MAX, LABEL_MAX - 2))
        for item in [*LONG, WIDEST]:
            with self.subTest(key=item["key"]):
                segs = self.assertTicker(self.render(None, fleet([item])), left=HEAD_MAX)
                for s in segs:
                    self.assertTrue(s["text"].startswith(f"{item['who']} · {item['label']} "), s["text"])

    def test_the_notes_pass_in_order_and_loop_without_gaps(self):
        # 240 px/s: about one lap of the five long notes every 25 s.
        for budget in (3000, 20_000, 41_000):
            with self.subTest(budget=budget):
                board = self.render(None, fleet(LONG), budget_ms=budget, query="?speed=240")
                self.assertEqual(self.speed, 240)
                segs = self.assertTicker(board, left=HEAD_MAX)
                order = [next(i for i, t in enumerate(LONG_SENTENCES) if s["text"].startswith(t + " "))
                         for s in segs]
                if budget == 3000:
                    self.assertEqual(order[0], 0)              # the first in the window is the first note
                    self.assertIn("waiting on the captain · 2 min", segs[0]["text"])
                for a, b in zip(order, order[1:]):
                    self.assertEqual(b, (a + 1) % len(LONG))
        # Two short notes: the lap is shorter than the window, so the wrap is always on screen.
        board = self.render(pinned("Today: ticker"), fleet([note("a1", label="live notes")]), budget_ms=30_000)
        segs = self.assertTicker(board, left=HEAD_MAX)
        texts = [s["text"].split(" ", 1)[0] for s in segs]
        self.assertGreaterEqual(len(texts), 4)
        self.assertEqual(set(texts), {"Today:", "Lighthouse"})
        for a, b in zip(texts, texts[1:]):
            self.assertNotEqual(a, b)

    def test_captains_notes_come_first(self):
        manual = {"schema": "fleetnotes-notes.v1", "off": False, "notes": [
            {"key": "m1", "text": "Pinned of the day", "pinned": True, "expires": None},
            {"key": "m2", "text": "Old temporary", "pinned": False, "expires": ago(-600)},
            {"key": "m3", "text": "New temporary", "pinned": False, "expires": ago(-900)},
            {"key": "m4", "text": "Already expired", "pinned": False, "expires": ago(60)},
        ]}
        feed = fleet([note("a1", label="alpha"), note("a2", label="bravo")])
        board = self.render(manual, feed)
        segs = self.assertTicker(board, left=HEAD_MAX)
        self.assertEqual([s["text"] for s in segs[:4]],
                         ["Pinned of the day captain's note", "New temporary captain's note",
                          "Lighthouse · alpha building · 18 min", "Lighthouse · bravo building · 18 min"])
        self.assertIn("pinned", segs[0]["cls"])
        self.assertNotIn("pinned", segs[1]["cls"])
        for gone in ("Old temporary", "Already expired"):
            self.assertNotIn(gone, self.text(board))

    def test_text_is_never_html(self):
        manual = pinned("<img src=x onerror=alert(1)>")
        feed = fleet([note("a1", label="<b>bold</b>")])
        for budget in (3000, 20_000):
            board = self.render(manual, feed, budget_ms=budget)
            self.assertNotIn("<img", board)
            self.assertIn("&lt;img", board)
            self.assertNotIn("<b>", board)
            self.assertIn("&lt;b&gt;bold", board)

    def test_ages_advance_without_new_data(self):
        feed = fleet([note("a1", started=1080)])
        self.assertIn("building · 18 min", self.text(self.render(None, feed)))
        later = self.render(None, feed, budget_ms=66_000)  # same files, 66 s later
        self.assertIn("building · 19 min", self.text(later))
        self.assertTicker(later, left=HEAD_MAX)

    def test_long_header_fits(self):
        many = {"running": 10, "validating": 0, "needs_captain": 2, "problem": 2, "paused": 0, "done_recent": 0}
        board = self.render(None, fleet([note("a1")], many))
        self.assertEqual(self.head(board), "FIRSTMATE 14 crewmates 2 waiting 2 stuck")

    def test_a_header_too_wide_falls_back_to_a_shorter_one(self):
        huge = {"running": 10 ** 18, "validating": 0, "needs_captain": 10 ** 17, "problem": 10 ** 17,
                "paused": 0, "done_recent": 0}
        head = self.head(self.render(None, fleet([note("a1"), note("a2")], huge)))
        self.assertNotIn("crewmates", head)                 # the longest one is too wide
        self.assertIn("1200000000000000000 crew", head)

    def test_new_notes_and_new_ages_enter_with_the_next_note_without_a_jump(self):
        first = fleet([note("a1", "needs_captain", "first note"), note("a2", label="second note")])
        second = fleet([note("a1", "needs_captain", "first note"),
                        note("a2", label="second note", started=1800), note("a3", label="brand new note")])
        # The second poll (5 s) changes a2's age and adds a3 while the first a2 is crossing the strip.
        board = self.render_sequence([None], [first, second], budget_ms=12_000)
        segs = self.assertTicker(board, left=HEAD_MAX)     # no gap where the change entered
        old = [s for s in segs if "second note building · 18 min" in s["text"]]
        new = [s for s in segs if "second note building · 30 min" in s["text"]]
        added = [s for s in segs if "brand new note" in s["text"]]
        self.assertTrue(old and new and added)
        self.assertLess(max(s["start"] for s in old), 5000)     # already entering when the data changed
        for s in new + added:
            self.assertGreater(s["start"], 5000)                # prepared after it, off the strip

    def test_a_retracted_note_leaves_at_the_next_poll(self):
        kept = "Lighthouse · first note"
        a1 = note("a1", "needs_captain", "first note")
        feed = fleet([a1, note("a2", label="retracted note")])
        empty = {"schema": "fleetnotes-notes.v1", "off": False, "notes": []}
        cases = {
            "fleet deny": ([None], [feed, fleet([a1])], "retracted note"),
            "fleet unlabel": ([None], [feed, fleet([a1, note("a2")])], "retracted note"),
            "a new label": ([None], [feed, fleet([a1, note("a2", label="another sentence")])], "retracted note"),
            "an unknown state": ([None], [feed, fleet([a1, note("a2", "lost", "retracted note")])],
                                 "retracted note"),
            "rm, clear or e": ([pinned("Retracted pinned"), empty], [fleet([a1])], "Retracted pinned"),
            "a new pinned note": ([pinned("Retracted pinned"), pinned("New pinned", key="m2")], [fleet([a1])],
                                  "Retracted pinned"),
        }
        for name, (manual, feeds, gone) in cases.items():
            with self.subTest(name):
                # The second poll (5 s) retracts the note while it is crossing the strip.
                board = self.render_sequence(manual, feeds, budget_ms=7000)
                self.assertTrue(self.filled)
                self.assertNotIn(gone, self.text(board))
                crossing = [s for s in self.segments(board) if s["start"] < 5000]
                self.assertTrue(crossing)                       # the other notes did not jump
                for s in crossing:
                    self.assertTrue(s["text"].startswith(kept + " "), s["text"])

    def test_a_temporary_note_that_expires_finishes_crossing(self):
        # Same file every poll: the overlay's own clock drops the note about 7 s in (a poll at 5 or 10 s).
        manual = {"schema": "fleetnotes-notes.v1", "off": False, "notes": [
            {"key": "m1", "text": "Pinned of the day", "pinned": True, "expires": None},
            {"key": "m2", "text": "Temporary that expires", "pinned": False, "expires": ago(-7)}]}
        segs = self.assertTicker(self.render(manual, budget_ms=14_000))   # no gap where it expired
        self.assertTrue(any("Temporary that expires" in s["text"] for s in segs))   # still crossing
        for s in segs:
            if s["start"] > 10_000:
                self.assertNotIn("Temporary that expires", s["text"])          # never prepared again

    def test_the_last_note_expiring_finishes_crossing_then_the_strip_goes_blank(self):
        manual = {"schema": "fleetnotes-notes.v1", "off": False, "notes": [
            {"key": "m1", "text": "Temporary that expires", "pinned": False, "expires": ago(-7)}]}
        board = self.render(manual, budget_ms=14_000)
        self.assertTrue(self.filled)
        segs = self.segments(board)
        self.assertTrue(segs)
        for s in segs:
            self.assertEqual(s["text"], "Temporary that expires captain's note")
            self.assertLess(s["start"], 10_000)                 # none prepared after it expired
        self.assertBlank(self.render(manual, budget_ms=40_000))

    def test_a_stale_feed_takes_its_notes_off_at_once(self):
        manual = pinned("Today: ticker")
        fresh = fleet([note("a1", label="live notes")])
        stale = fleet([note("a1", label="live notes")], generated_ago=400)
        for later in (stale, None, "broken"):
            with self.subTest(later=later if not isinstance(later, dict) else "stale"):
                if later == "broken":
                    _QuietHandler.sequences[FLEET_FILENAME] = [json.dumps(fresh).encode(), b"{not json"]
                    _QuietHandler.sequences[MANUAL_FILENAME] = [json.dumps(manual).encode()]
                    board = self.render(None, None, budget_ms=7000)
                else:
                    board = self.render_sequence([manual], [fresh, later], budget_ms=7000)
                self.assertTrue(self.filled)
                self.assertNotIn("live notes", self.text(board))
                self.assertNotIn("head on", board)
                segs = self.segments(board)
                self.assertTrue(segs)
                for s in segs:
                    self.assertIn("kind-manual", s["cls"])

    def test_off_mid_run_clears_everything(self):
        on = pinned("Today: ticker")
        off = dict(on, off=True)
        board = self.render_sequence([on, off], [fleet([note("a1")])], budget_ms=7000)
        self.assertBlank(board)

    def test_speed_comes_from_the_url(self):
        feed = fleet([note("a1", label="live notes")])
        for query, speed in (("", 60), ("?speed=120", 120), ("?speed=5", 20), ("?speed=1000", 240),
                             ("?speed=abc", 60)):
            with self.subTest(query=query):
                board = self.render(None, feed, query=query)
                self.assertEqual(self.speed, speed)
                self.assertTicker(board, left=HEAD_MAX)

    def test_odd_values_never_draw_or_break(self):
        feed = fleet([
            note("a1", state="constructor", label="alpha"),
            note("a2", kind="toString", step="valueOf", changed=60, started=600, label="bravo"),
            dict(note("a3", label="charlie"), changed={"toString": 0}, started=["x"]),
        ])
        text = self.text(self.render(None, feed))
        self.assertNotIn("function", text)
        self.assertNotIn("native code", text)
        self.assertIn("Lighthouse · bravo working · 10 min", text)   # unknown kind: the neutral phrase
        self.assertIn("Lighthouse · charlie building", text)
        self.assertNotIn("alpha", text)                              # "constructor" is not a state

    def test_the_other_states_and_their_phrases(self):
        feed = fleet([
            note("a1", "problem", "stuck job"),
            note("a2", "done", "finished job", changed=30),
            note("a3", "running", "scouting job", kind="scout", started=120),
            note("a4", "validating", "checked job", step="test", position=2, total=3),
        ], {"running": 1, "validating": 1, "needs_captain": 0, "problem": 1, "paused": 0, "done_recent": 1})
        # Each note is prepared 4 s before it enters, so when the fourth one is ready depends on how
        # wide the font makes the first three: wait for the four of them, never past 20 s.
        board = self.render(None, feed, budget_ms=20_000, until=lambda b: len(self.segments(b)) >= 4)
        self.assertEqual(self.head(board), "FIRSTMATE 3 crewmates 1 stuck")
        text = self.text(board)
        self.assertIn("Lighthouse · stuck job stuck · firstmate is on it", text)
        self.assertIn("Lighthouse · finished job done! · just now", text)
        self.assertIn("Lighthouse · scouting job researching · 2 min", text)
        self.assertIn("Lighthouse · checked job validating 2/3 · tests · 18 min", text)

    def test_a_feed_without_who_falls_back_to_the_project(self):
        old = {k: v for k, v in note("a1", label="live notes").items() if k not in ("who", "label_from")}
        self.assertIn("Lighthouse · live notes", self.text(self.render(None, fleet([old]))))

    def test_unreadable_manual_file_hides_everything(self):
        self.assertBlank(self.render("{not json", fleet([note("a1")])))

    def test_a_note_longer_than_any_limit_is_shown_whole(self):
        board = self.render(pinned("\U0001F389" * 100))
        segs = self.assertTicker(board)
        self.assertEqual(segs[0]["text"].count("\U0001F389"), 100)
        self.assertGreater(segs[0]["w"], BOARD_W)
        self.assertNotIn("…", self.text(board))

    def test_the_longest_captains_note_is_whole_on_one_line(self):
        widest = {"letters": ("Today: sticky notes with whole sentences for each job, from the "
                              "secondmates and the Studio Mac, yes")[:NOTE_MAX],
                  "emoji": "Live until 18:00 \U0001F389\U0001F389 then Harbor on the Studio Mac ☕"}
        self.assertEqual(len(widest["letters"]), NOTE_MAX)
        for name, text in widest.items():
            with self.subTest(name):
                self.assertLessEqual(text_width(text), NOTE_MAX)
                segs = self.assertTicker(self.render(pinned(text)))
                self.assertEqual(segs[0]["text"], f"{text} captain's note")

    def test_renders_the_feed_built_from_a_snapshot(self):
        snapshot = {"schema": "fm-fleet-snapshot.v1", "generated": ago(5), "tasks": [{
            "id": "lh-kumquat-overlay", "kind": "ship", "project": "/opt/fm/projects/lighthouse",
            "spawn_gen": f"s{NOW - 1500}.1.2",
            "paths": {"status_log": {"last_event": {"age_seconds": 400}}},
            "current_state": {"state": "working", "source": "pane", "freshness": "fresh"},
            "hints": {"pending_decision": False, "blocked_event": False}}],
            "backlog": {"records": [{"id": "lh-kumquat-overlay",
                                     "title": "Lighthouse: notes with concrete sentences (lh-kumquat-overlay)"}]}}
        rules = parse_rules('project lighthouse "Lighthouse"\n', "/opt/fm/rules.conf")
        feed = build_feed(snapshot, rules, b"s" * 32, now=NOW).feed
        board = self.render(None, feed)
        self.assertEqual(self.head(board), "FIRSTMATE 1 crewmate")
        self.assertIn("Lighthouse · notes with concrete sentences building · 25 min", self.text(board))
        self.assertNotIn("kumquat", self.text(board))

    def test_the_page_runs_on_the_test_clock(self):
        with Chrome(self.url, NOW * 1000) as chrome:
            chrome.run(1234)
            seen = chrome.evaluate("[Date.now(), new Date().getTime(), performance.now(), "
                                   "document.timeline.currentTime]")
            self.assertEqual(seen, [NOW * 1000 + 1234, NOW * 1000 + 1234, 1234, 1234])


class OverlayClockTest(unittest.TestCase):
    def test_the_overlay_reads_time_only_from_what_the_test_clock_drives(self):
        with open(OVERLAY, encoding="utf-8") as f:
            script = f.read().split("<script>", 1)[1]
        used = set(TIME_SOURCES.findall(script))
        self.assertIn("Date.now", used)
        self.assertLessEqual(used, CLOCKED)


if __name__ == "__main__":
    unittest.main()
