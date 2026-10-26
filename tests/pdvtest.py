"""Shared helpers for the server tests: a reference decoder for the video
stream, a frame source for Stream without ffmpeg, test frames and videos."""

import os
import random
import shutil
import socket
import subprocess
import sys
import threading
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import convert  # noqa: E402
import server   # noqa: E402

server.say = lambda msg: None    # no event lines in the test output

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ---- the stream as the PET client reads it

class StreamError(AssertionError):
    """The stream breaks a rule that the PET client depends on."""


def decode(data, cells, screen=None):
    """Decode stream bytes like the PET client: [(marker, screen)] for each
    complete frame, ("search", screen) for the $FC marker, ("audio",
    screen) for the $FA marker, after which only sound follows, and
    ("stopped", screen) for the $F9 marker. A frame cut off at
    the end of data is left out. Unlike the client, which skips unknown
    bytes and drops broken frames, this raises StreamError."""
    frames = []
    i, n = 0, len(data)
    while i < n:
        m = data[i]
        if m == server.PAD:
            i += 1
            continue
        if m == server.SEARCH:
            frames.append(("search", screen))
            i += 1
            continue
        if m == server.AUDIO:
            frames.append(("audio", screen))   # the rest is sound
            break
        if m == server.STOPPED:
            frames.append(("stopped", screen))  # the server stops
            break
        try:
            if m == server.KEY:
                if i + 1 + cells > n:
                    break
                screen = bytes(data[i + 1:i + 1 + cells])
                i += 1 + cells
            elif m == server.RLE:
                screen, i = _rle(data, i + 1, cells)
            elif m == server.DELTA:
                if screen is None:
                    raise StreamError(f"delta frame before a key frame at offset {i}")
                screen, i = _delta(data, i + 1, screen)
            else:
                raise StreamError(f"unknown marker ${m:02X} at offset {i}")
        except IndexError:
            break                    # the frame goes on in the next block
        frames.append((m, screen))
    return frames


def _rle(data, i, cells):
    out = bytearray()
    while len(out) < cells:
        c = data[i]
        i += 1
        if c < 128:
            lit = data[i:i + c + 1]
            if len(lit) < c + 1:
                raise IndexError
            out += lit
            i += c + 1
        else:
            out += bytes([data[i]]) * (c - 126)
            i += 1
        if len(out) > cells:
            raise StreamError("run-length frame longer than the screen")
    return bytes(out), i


def _delta(data, i, screen):
    out = bytearray(screen)
    pos = 0
    while True:
        skip, count = data[i], data[i + 1]
        i += 2
        if skip == 0 and count == 0:
            return bytes(out), i
        pos += skip
        chunk = data[i:i + count]
        if len(chunk) < count:
            raise IndexError
        if pos + count > len(out):
            raise StreamError("delta frame writes past the end of the screen")
        out[pos:pos + count] = chunk
        i += count
        pos += count


# ---- test frames

def noise(rnd, cells):
    """Every cell random: compresses badly, like a scene cut in fast motion."""
    return bytes(rnd.randrange(256) for _ in range(cells))


def runs(rnd, cells, lengths=(1, 2, 3, 4, 127, 128, 129, 130, 258, 300)):
    """Runs of equal codes, with run lengths around the coding limits."""
    out = bytearray()
    while len(out) < cells:
        out += bytes([rnd.choice((32, 160, 102, 81, 0, 255))]) * rnd.choice(lengths)
    return bytes(out[:cells])


def changed(rnd, prev, count):
    """prev with `count` random cells changed."""
    out = bytearray(prev)
    for _ in range(count):
        out[rnd.randrange(len(out))] = rnd.randrange(256)
    return bytes(out)


# ---- frame sources

class FakeSource:
    """The parts of LiveSource that Stream uses, with frames that the test
    sets with show(). Each show() makes a new frame due."""

    def __init__(self, codes=None, cols=80, url="test.mp4"):
        self.cols = cols
        self.codes = codes if codes is not None else bytes([32]) * (cols * 25)
        self.count = 0
        self.url = url
        self.paused = False
        self.audio = False
        self.media = None            # what get_media() returns in audio mode
        self.latest = self.codes
        self.ready = threading.Event()   # a frame exists
        self.ready.set()
        self.menu = None
        self.results = []
        self.position = 0.0
        self.seek_to = None
        self.loop_start = self.loop_end = None
        self.zoom = "fit"
        self.overlay = ["OVERLAY TEST"]
        self.settings_applied = None

    def show(self, codes):
        self.codes = self.latest = codes
        self.count += 1

    def get_media(self, audio=False):
        return self.media

    still_picture = None             # what still() returns
    still_calls = None

    def still(self, at, cols):
        if self.still_calls is None:
            self.still_calls = []
        self.still_calls.append(at)
        return self.still_picture

    def current(self, t0):
        return self.count, self.codes

    def set_cols(self, cols):
        self.cols = cols

    def close_results(self):
        pass

    def overlay_lines(self, cols):
        return list(self.overlay)

    def settings(self):
        return {"brightness": 0.0, "contrast": 1.0, "gamma": 1.0, "dither": 0}

    def apply_settings(self, st):
        self.settings_applied = st


_matchers = None


def matchers():
    global _matchers
    if _matchers is None:
        _matchers = {cols: convert.Matcher(cols) for cols in (80, 40)}
    return _matchers


def live_source(source=None, **kw):
    """A LiveSource whose decoder thread does not start, so nothing runs
    ffmpeg or yt-dlp. Fresh matchers, because the keys change them."""
    ms = {cols: convert.Matcher(cols) for cols in (80, 40)}
    with mock.patch.object(server.threading, "Thread"):
        return server.LiveSource(source, ms, kw.pop("fps", 10.0), **kw)


# ---- videos and processes

def make_video(path, seconds=10, size="640x360", source="testsrc"):
    """A test video made by ffmpeg: "testsrc" moves, "color=white" is flat."""
    lavfi = (f"{source}=duration={seconds}:size={size}:rate=25" if source == "testsrc"
             else f"{source}:duration={seconds}:size={size}:rate=25")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", lavfi,
                    "-pix_fmt", "yuv420p", path], check=True)
    return path


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seeded(seed=1):
    return random.Random(seed)
