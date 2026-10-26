#!/usr/bin/env python3
"""PETdisk Video (PDV) server, a fake petdisk.php: serves PET files and an
endless VIDEO80.SEQ and VIDEO40.SEQ frame streams to a PETdisk MAX network drive.

The PETdisk MAX firmware speaks HTTP/1.0, one TCP connection per request, and
reads the response until the server closes the socket. Its receive buffer is
832 bytes, so every response (headers plus body) must stay below that. The
firmware sends these requests (the path is whatever PETDISK.CFG says):

  ?d=1&p=N              directory page N, names ending in \\n, empty line ends
  ?file=NAME&l=1        file size as decimal text plus \\r\\n
  ?file=NAME&s=A&e=B    raw bytes [A, B) of the file

Run:  server.py VIDEO_OR_URL      converts while it plays, loops files,
                                  URLs go through yt-dlp
"""
import argparse
import collections
import concurrent.futures
import math
import shutil
import os
import re
import pickle
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SERVE_DIR = os.path.join(HERE, "serve")
# The PET client asks for VIDEO80 (80 columns) or VIDEO40 (40 columns)
VIDEO_NAMES = {"VIDEO80.SEQ": 80, "VIDEO40.SEQ": 40}
VIDEO_SIZE = 4_000_000_000       # "endless": the firmware never sees the end
MAX_RESPONSE = 832
MAX_RANGE = 4096        # largest byte range served in one request
LIST_EXT = (".prg", ".seq", ".d64")
# The server restarts itself when one of these files changes.
WATCH = [os.path.abspath(__file__), os.path.join(HERE, "convert.py")]


KEY = 0xFF      # key frame: all screen codes follow (2000, or 1000 for 40 columns)
DELTA = 0xFE    # delta frame: (skip, count, count codes)... then (0, 0)
PAD = 0xFD      # filler between frames, ignored by the client
SEARCH = 0xFC   # nothing to play: the client goes to the search screen
RLE = 0xFB      # run-length key frame: see encode_rle
AUDIO = 0xFA    # audio mode: packets of 1-bit sound follow, see AUDIO_PACKET
LEAVE = 0xFF    # in audio mode, at a packet start: back to video frames
STOPPED = 0xF9  # the server stops: the PET ends PDV with a message
# Audio mode (A key): bits per second that the PET plays. Its VIA shifts a
# bit every 42 cycles (23.8 kHz), plus a short pause after each byte until
# the next byte goes in. Tune this by ear if the pitch is off.
AUDIO_RATE = 22000
AUDIO_PREFILL_BYTES = 16 * 256               # the PET buffers this much first
AUDIO_PREFILL = AUDIO_PREFILL_BYTES * 8 / AUDIO_RATE   # in seconds of sound
AUDIO_METHOD = "clip"   # set by --audio-method: clip or pdm, see convert.py
# After the AUDIO marker the stream is packets of AUDIO_PACKET bytes:
# AUDIO_TIME screen codes of the time or a message (in the title on row 0,
# see AUDIO_TITLE), a picture block of AUDIO_PIC bytes, then the sound.
# The PET shows the text and applies the picture block when the sound of
# the packet starts to play. The picture blocks build a slow slide show,
# see picture_block().
AUDIO_PACKET = 512
AUDIO_TIME, AUDIO_PIC = 9, 16
AUDIO_SOUND = AUDIO_PACKET - AUDIO_TIME - AUDIO_PIC
PIC_END = 0xFF       # ends a picture block early
# The title on row 0 in audio mode, around the time field. client.a has
# the column of the field (TIMEX): 31 for 80 columns, 13 for 40.
AUDIO_TITLE = {80: ("AUDIO MODE ", " - PRESS A FOR VIDEO"), 40: ("AUDIO ", " - A: VIDEO")}


def audio_title(cols, field):
    """(row 0 text, column of the time field) in audio mode."""
    before, after = AUDIO_TITLE[cols]
    line = before + field.ljust(AUDIO_TIME)[:AUDIO_TIME] + after
    start = (cols - len(line)) // 2
    return " " * start + line, start + len(before)


def time_field(seconds):
    """The time for the title: "(1:23)"."""
    return f"({clock(seconds)})"


def audio_packet(text, picture, sound):
    """One packet of the audio stream. The text comes first: its screen
    codes are never LEAVE, so the PET can tell a LEAVE at a packet start."""
    codes = bytes(screen_code(ch) for ch in text.ljust(AUDIO_TIME)[:AUDIO_TIME])
    picture = bytes(picture)[:AUDIO_PIC].ljust(AUDIO_PIC, bytes([PIC_END]))
    return codes + picture + bytes(sound)


def silent_packet(text):
    """A packet without sound and without picture changes, for example
    while the sound is not ready yet."""
    return audio_packet(text, b"", bytes(AUDIO_SOUND))


def picture_block(screen, target, keep=range(0)):
    """The next AUDIO_PIC bytes of picture commands that bring `screen`
    (a bytearray, updated here) closer to `target`, or None when they are
    equal. Cells in `keep` are left alone (the title on row 0). Each block
    stands alone, with absolute positions, so a block that is never sent
    does not confuse the PET:
      literal  hi = (length - 1) << 3 | pos >> 8, lo = pos & 255, then
               length (1-14) screen codes
      repeat   hi = 0x80 | pos >> 8, lo, count (1-255), code
      end      PIC_END
    Positions are 0-2047, so a header is never PIC_END."""
    n = len(target)

    def changed(i):
        return screen[i] != target[i] and i not in keep

    out = bytearray()
    i = 0
    while len(out) < AUDIO_PIC:
        while i < n and not changed(i):
            i += 1
        if i >= n:
            break
        run = 1
        while i + run < n and run < 255 and target[i + run] == target[i] and (i + run) not in keep:
            run += 1
        if run >= 5 and len(out) + 4 <= AUDIO_PIC:
            out += bytes([0x80 | i >> 8, i & 255, run, target[i]])
            screen[i:i + run] = bytes([target[i]]) * run
            i += run
            continue
        room = AUDIO_PIC - len(out) - 2
        if room < 1:
            break
        length = 1
        while length < min(room, 14) and i + length < n and (i + length) not in keep:
            j = i + length
            if not (changed(j) or (j + 1 < n and changed(j + 1))):
                break                          # a gap: cheaper as a new command
            if j + 4 < n and len(set(target[j:j + 5])) == 1 and changed(j):
                break                          # a run of 5: a repeat command
            length += 1
        out += bytes([(length - 1) << 3 | i >> 8, i & 255]) + target[i:i + length]
        screen[i:i + length] = target[i:i + length]
        i += length
    if not out:
        return None
    return bytes(out)


def apply_block(screen, block):
    """Apply a picture block to `screen` (a bytearray), as the PET does."""
    i = 0
    while i + 1 < len(block) and block[i] != PIC_END:
        hi, lo = block[i], block[i + 1]
        pos = (hi & 7) << 8 | lo
        if hi & 0x80:
            count, code = block[i + 2] or 256, block[i + 3]
            screen[pos:pos + count] = bytes([code]) * min(count, len(screen) - pos)
            i += 4
        else:
            length = (hi >> 3 & 15) + 1
            codes = block[i + 2:i + 2 + length]
            screen[pos:pos + len(codes)] = codes
            i += 2 + length


def clock(seconds):
    if seconds is None:
        return "?"
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def pet_text(text):
    """Text the PET can show: accents removed, uppercase, other characters
    replaced with spaces."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = "".join(c if 32 <= ord(c) < 96 else " " for c in t.upper())
    return " ".join(t.split())


def text_screen(lines, cols):
    """Screen codes of a text screen: lines from the top, the rest blank."""
    out = bytearray(b" " * (cols * 25))
    for row, text in enumerate(lines[:25]):
        for col, ch in enumerate(text[:cols]):
            out[row * cols + col] = screen_code(ch)
    return bytes(out)


def screen_code(ch):
    """PET screen code (uppercase/graphics set) of a character."""
    c = ord(ch.upper())
    if 64 <= c <= 95:
        return c - 64
    if 32 <= c <= 63:
        return c
    return 32


def encode_rle(cur):
    """Run-length key frame: RLE, then control bytes until the frame is
    full. A control byte below 128 is followed by that many + 1 literal
    codes (1..128). 128 and above repeat the next code control - 126 times
    (2..129). Runs of 3 or more cells are repeated; shorter ones go in the
    literals, because a repeat ends the literal run and costs a control
    byte to start the next one."""
    n = len(cur)
    out = bytearray([RLE])
    lit = 0          # start of the pending literal run
    i = 0
    while i < n:
        j = i + 1
        while j < n and j - i < 129 and cur[j] == cur[i]:
            j += 1
        if j - i >= 3:
            while lit < i:
                k = min(128, i - lit)
                out += bytes([k - 1]) + cur[lit:lit + k]
                lit += k
            out += bytes([j - i + 126, cur[i]])
            lit = j
        i = j
    while lit < n:
        k = min(128, n - lit)
        out += bytes([k - 1]) + cur[lit:lit + k]
        lit += k
    return bytes(out)


def encode_frame(prev, cur):
    """The shortest frame that takes the PET from prev to cur: a delta or a
    key frame, a key frame when there is no prev."""
    rle = encode_rle(cur)
    key = rle if len(rle) < 1 + len(cur) else bytes([KEY]) + bytes(cur)
    if prev is None:
        return key
    delta = encode_delta(prev, cur)
    return delta if len(delta) < len(key) else key


def encode_delta(prev, cur):
    """Runs of changed cells. Gaps of 1-2 unchanged cells are sent as data,
    because a new run costs two bytes."""
    n = len(cur)
    out = bytearray([DELTA])
    pos = 0          # cell the client points at
    i = 0
    while i < n:
        if prev[i] == cur[i]:
            i += 1
            continue
        j = i + 1    # extend the run while the next change is near
        while j < n:
            if prev[j] != cur[j]:
                j += 1
            elif j + 2 < n and (prev[j + 1] != cur[j + 1] or prev[j + 2] != cur[j + 2]):
                j += 1
            else:
                break
        skip = i - pos
        while skip > 255:
            out += bytes([255, 0])
            skip -= 255
        first = True
        k = i
        while k < j:
            count = min(255, j - k)
            out += bytes([skip if first else 0, count])
            out += cur[k:k + count]
            first = False
            k += count
        pos = j
        i = j
    out += bytes([0, 0])
    return bytes(out)


class LiveSource:
    """Converts a video file or a URL at playback speed in a background
    thread and loops it. The newest frame is due. Conversion settings can be
    changed while it runs, with the hotkeys on the PET or in the terminal.
    Frames are decoded at 640x200 pixels; for 40 columns pairs of pixels are
    averaged to 320x200."""

    def __init__(self, source, matchers, fps, zoom="fit", aspect=1.8):
        import convert
        self.matchers = matchers         # {80: Matcher, 40: Matcher}
        self.aspect = aspect
        # the values from the command line, for the R key
        m = matchers[80]
        self.start_settings = {"brightness": m.brightness, "contrast": m.contrast,
                               "gamma": m.gamma, "dither": int(m.dither)}
        self.cols = 80
        self.latest = None
        self.latest_url = None           # the video that latest belongs to
        self.count = 0
        self.lock = threading.Lock()
        self.ready = threading.Event()
        big = matchers[80]
        self.url = source            # video file or URL now playing, or None
        self.switch = False          # set when a new video was picked
        self.menu = None             # text screen lines while a menu shows
        self.results = []            # [(title, duration, url)] of the last search
        self.query = ""              # the last search words
        self.page = 0                # result page shown, from 0
        self.query_parts = []        # search text pieces sent by the PET
        self.more_possible = False   # the last search filled all it asked for
        self.search_done = threading.Event()
        self.search_done.set()
        self.results_snapshot = {}   # cols -> RESULTS file bytes being read
        self.fps = fps
        self.position = 0.0          # seconds into the video, of the newest frame
        self.duration = None
        self.seek_to = None          # set by the seek keys
        self.paused = False
        self.zoom = zoom             # "fit", "fill" or "stretch"
        self.start_zoom = zoom       # for the R key
        self.loop_start = None       # loop marks in seconds, or None
        self.loop_end = None
        self.settings_version = 0    # counts setting changes
        self.retry_at = None         # time of the next try to get the video
        self.audio = False           # audio mode: sound instead of video
        self.media = {}              # (url, audio) -> Future of (media, duration)
        self.media_lock = threading.Lock()

        def run():
            while True:
                if self.menu is not None or self.url is None or self.audio:
                    time.sleep(0.05)
                    continue
                self.switch = False
                media = self.get_media()
                self.prefetch_sound()
                start, self.seek_to = self.seek_to or 0.0, None
                play(media, start)

        def play(media, start):
            prev, prev_cols, last_gray = None, None, None
            while not self.leaving():
                if media is None:
                    # the terminal view counts down to the next try; a new
                    # video or the search screen ends the wait
                    self.retry_at = time.monotonic() + self.RETRY
                    say(f"could not get the video, trying again in {self.RETRY:.0f} s"
                        if DISPLAY is None else "could not get the video")
                    while time.monotonic() < self.retry_at and not self.leaving():
                        time.sleep(0.1)
                    self.retry_at = None
                    if not self.leaving():
                        media, self.duration = convert.resolve(self.url, log=say)
                    continue
                # ffmpeg starts again after a seek, a zoom change or the
                # end: the previous frame may have another position or
                # zoom, so it must not keep any old characters
                prev = None
                n = 0
                for gray in convert.gray_frames(media, fps, big.width,
                                                big.height, False,
                                                realtime=True, log=say,
                                                start=start, aspect=aspect,
                                                zoom=self.zoom):
                    if self.seek_to is not None or self.paused or self.leaving():
                        break
                    last_gray = gray             # full width, for pause
                    cols = self.cols
                    if cols == 40:
                        gray = gray.reshape(big.height, 320, 2).mean(axis=2)
                    if cols != prev_cols:
                        prev = None
                    codes = self.matchers[cols].codes_for(gray, prev)
                    prev, prev_cols = codes, cols
                    n += 1
                    if self.seek_to is None:
                        self.position = start + n / fps
                        if (self.loop_end is not None
                                and self.position >= self.loop_end):
                            self.seek_to = self.loop_start or 0.0
                    with self.lock:
                        self.latest = codes.tobytes()
                        self.latest_url = self.url
                        self.count += 1
                    self.ready.set()
                if self.leaving():
                    return                   # a menu, a new video or audio mode
                if self.paused:
                    # stop the decoder and keep the last frame on the PET;
                    # convert it again when a setting or the width changes
                    seen = (self.settings_version, self.cols)
                    while self.paused and not self.leaving():
                        if self.seek_to is not None:
                            # seek while paused: decode one frame there
                            at, self.seek_to = self.seek_to, None
                            for g in convert.gray_frames(media, fps, big.width,
                                                         big.height, False,
                                                         max_frames=1, log=say,
                                                         start=at, aspect=aspect,
                                                         zoom=self.zoom):
                                last_gray = g
                            self.position = at
                            seen = None
                        now = (self.settings_version, self.cols)
                        if now != seen and last_gray is not None:
                            seen = now
                            cols = self.cols
                            gray = last_gray
                            if cols == 40:
                                gray = gray.reshape(big.height, 320, 2).mean(axis=2)
                            codes = self.matchers[cols].codes_for(gray, None)
                            prev, prev_cols = codes, cols
                            with self.lock:
                                self.latest = codes.tobytes()
                                self.latest_url = self.url
                                self.count += 1
                            self.ready.set()
                        time.sleep(0.05)
                    start = self.seek_to if self.seek_to is not None else self.position
                    self.seek_to = None
                    prev = None
                    continue
                if self.seek_to is not None:
                    start, self.seek_to = self.seek_to, None
                    continue
                if n == 0:
                    if start > 0:            # seeked past the end
                        start = 0.0
                        continue
                    # a web media URL expires after some hours: get a new one
                    say("the video did not play, getting it again")
                    time.sleep(2)
                    media, self.duration = convert.resolve(self.url, log=say)
                    continue
                start = self.loop_start or 0.0
                say(f"video ended after {n} frames, starting again at {clock(start)}")

        threading.Thread(target=run, daemon=True).start()

    RETRY = 10.0     # seconds before the next try to get the video

    def retry_text(self):
        """While the decoder waits to try the video again: the countdown
        for the terminal view, else None."""
        at = self.retry_at
        if at is None:
            return None
        left = max(1, math.ceil(at - time.monotonic()))
        return f"could not get the video, trying again in {left} s"

    def set_cols(self, cols):
        if cols != self.cols:
            with self.lock:
                self.cols = cols
                self.latest = None
                self.ready.clear()

    def current(self, t0):
        """(index, screen codes) of the newest frame at the current width,
        or of the menu screen while a menu shows."""
        while True:
            if self.menu is not None:
                menu = self.results_lines(self.cols)
                return ("menu", tuple(menu), self.cols), text_screen(menu, self.cols)
            self.ready.wait()
            with self.lock:
                if self.latest is not None and len(self.latest) == self.cols * 25:
                    return self.count, self.latest
            time.sleep(0.01)

    # ---- video choice: menu and YouTube search
    def leaving(self):
        """True when the decoder must stop: a menu shows, a new video was
        picked, or audio mode is on."""
        return self.menu is not None or self.switch or self.audio

    def still(self, at, cols):
        """The picture at `at` seconds as screen codes, for the slide show
        in audio mode, or None."""
        import convert
        media = self.get_media()
        if media is None:
            return None
        big = self.matchers[80]
        for gray in convert.gray_frames(media, 1, big.width, big.height, False, max_frames=1,
                                        start=at, aspect=self.aspect, zoom=self.zoom):
            if cols == 40:
                gray = gray.reshape(big.height, 320, 2).mean(axis=2)
            return self.matchers[cols].codes_for(gray).tobytes()
        return None

    def get_media(self, audio=False):
        """The media for the current URL, the video or the sound stream:
        resolved once, then reused, so that switching between video and
        audio mode does not run yt-dlp again. A caller that comes while
        another thread resolves the same media waits for its result; a
        failed lookup is tried again by the next caller."""
        import convert
        url = self.url
        key = (url, audio)
        with self.media_lock:
            pending = self.media.get(key)
            mine = pending is None or (pending.done() and pending.result()[0] is None)
            if mine:
                pending = self.media[key] = concurrent.futures.Future()
        if mine:
            say(f"opening {url}" + (" (sound)" if audio else ""))
            try:
                pending.set_result(convert.resolve(url, log=say, audio=audio))
            except BaseException:
                pending.set_result((None, None))
                raise
        media, duration = pending.result()
        if media is not None:
            self.duration = duration
        return media

    def prefetch_sound(self):
        """Look up the sound stream in the background, so that a later
        switch to audio mode does not wait for yt-dlp."""
        threading.Thread(target=self.get_media, kwargs={"audio": True}, daemon=True).start()

    PAGE_SIZE = 20

    def add_query_part(self, part):
        """The PET sends the search text in pieces of up to 15 characters
        (Q<text>), then an empty Q to start the search."""
        if part:
            self.query_parts.append(part)
            return
        query = "".join(self.query_parts).strip().lower()
        self.query_parts = []
        if query:
            self.search(query)

    def show_results(self):
        """The search screen opens: stop the video (it continues from here
        later) and show the results in the terminal view."""
        if self.url is not None and self.menu is None:
            self.seek_to = self.position
        self.menu = True

    def close_results(self):
        """Back to the video, if there is one."""
        if self.url is not None:
            self.menu = None

    def entry(self, text):
        """An entry in the search field: a result number plays it, N and P
        turn the page, other text is a new search. Empty goes back."""
        text = text.strip()
        if not text:
            self.close_results()
        elif text.isdigit():
            if not self.pick(int(text)):
                say(f"no result {text}")
        elif text.upper() == "N":
            self.turn_page(1)
        elif text.upper() == "P":
            self.turn_page(-1)
        else:
            self.search(text.lower())

    def turn_page(self, step):
        page = max(0, self.page + step)
        if page == self.page:
            return
        self.page = page
        need = (page + 1) * self.PAGE_SIZE
        if len(self.results) < need and self.more_possible:
            self.search(self.query, count=need, page=page)   # fetch more

    def search(self, query, count=None, page=0):
        """Search YouTube in the background. RESULTS reads wait for it.
        With a count for more pages, the results shown so far stay where
        they are and new videos are added after them."""
        more = count is not None and query == self.query
        count = count or self.PAGE_SIZE
        self.show_results()
        self.query, self.page = query, page
        self.search_done.clear()
        say(f"searching YouTube for '{query}'")

        def run():
            import convert
            try:
                found = convert.youtube_search(query, count)
            except Exception as e:           # network or yt-dlp failure
                say(f"search failed: {e}")
                found = []
            if more:
                seen = {url for _, _, url in self.results}
                self.results = self.results + [r for r in found if r[2] not in seen]
            else:
                self.results = found
            self.more_possible = len(found) >= count
            say(f"{len(self.results)} results for '{query}'")
            self.search_done.set()

        threading.Thread(target=run, daemon=True).start()

    def results_lines(self, cols):
        """The search screen text: one page of results, one line each, at
        most cols - 1 characters so that the PET does not wrap them."""
        w = cols - 1
        if not self.search_done.is_set():
            return [f"YOUTUBE: {self.query.upper()}"[:w], "", "SEARCHING..."]
        if not self.query:
            return ["PETDISK VIDEO (PDV) - YOUTUBE SEARCH"[:w]]
        last = (self.page + 1) * self.PAGE_SIZE >= len(self.results)
        more = "" if last and not self.more_possible else "  N: MORE"
        lines = [f"YOUTUBE: {self.query.upper()}  PAGE {self.page + 1}{more}"[:w], ""]
        first = self.page * self.PAGE_SIZE
        shown = self.results[first:first + self.PAGE_SIZE]
        if not shown:
            lines.append("NO RESULTS")
        for n, (title, duration, _) in enumerate(shown, first + 1):
            tail = f" ({clock(duration)})" if duration else ""
            head = f"{n:2d} "
            lines.append(head + pet_text(title)[:w - len(head) - len(tail)] + tail)
        return lines

    def results_file(self, cols):
        """The RESULTS file for the PET: the search screen as 25 rows of
        screen codes, which the PET copies straight to the screen. The
        bottom two rows stay empty for the input field. Waits for a
        running search."""
        self.search_done.wait(60)
        self.show_results()
        return text_screen(self.results_lines(cols)[:23], cols)

    def pick(self, number):
        if not 1 <= number <= len(self.results):
            return False
        title, duration, url = self.results[number - 1]
        say(f"playing {title}")
        self.url = url
        self.duration = duration
        self.position = 0.0
        self.seek_to = None
        self.loop_start = self.loop_end = None
        self.paused = False
        self.switch = True
        self.menu = None
        return True

    def set_mark(self, start=None, end=None):
        """Set a loop mark. If the marks would be in the wrong order, the
        earlier time becomes the start and the later one the end."""
        a = start if start is not None else self.loop_start
        b = end if end is not None else self.loop_end
        if a is not None and b is not None and b < a:
            a, b = b, a
        self.loop_start, self.loop_end = a, b

    # ---- hotkeys
    SEEK_KEYS = {"1": -60.0, "2": -10.0, "3": 10.0, "4": 60.0}

    def seek(self, delta):
        """Jump delta seconds, but not before the start or past the last
        frame. From the last frame the video ends and loops as usual."""
        # a seek that the decoder has not done yet counts as the position
        base = self.seek_to if self.seek_to is not None else self.position
        pos = max(0.0, base + delta)
        if self.duration:
            pos = min(pos, max(0.0, self.duration - 1.0 / self.fps))
        self.position = pos
        self.seek_to = pos

    @staticmethod
    def pet_key(code):
        """PET key code -> hotkey character. Unshifted letters (65-90) give
        lowercase, shifted letters (193-218) give uppercase."""
        if 65 <= code <= 90:
            return chr(code).lower()
        if 193 <= code <= 218:
            return chr(code - 128)
        if 48 <= code <= 57 or code in (32, 33):   # digits, SPACE, ! (shift-1)
            return chr(code)
        return ""

    def command(self, k):
        """Apply a hotkey: lowercase raises a value, uppercase lowers it.
        Returns "status" (a value changed), "toggle" (H: the overlay on or
        off), or None for an unknown key."""
        m = self.matchers[80]
        if self.menu is not None:
            if k == "h":
                return "toggle"
            return None                 # the search field takes the input
        if k == "b":
            m.brightness = min(1.0, m.brightness + 0.05)
        elif k == "B":
            m.brightness = max(-1.0, m.brightness - 0.05)
        elif k == "c":
            m.contrast = min(5.0, m.contrast + 0.1)
        elif k == "C":
            m.contrast = max(0.1, m.contrast - 0.1)
        elif k == "g":
            m.gamma = min(5.0, m.gamma + 0.1)
        elif k == "G":
            m.gamma = max(0.1, m.gamma - 0.1)
        elif k == "d":
            m.dither = (int(m.dither) + 1) % len(m.DITHER_MODES)
        elif k == "z":
            import convert
            modes = convert.ZOOM_MODES
            self.zoom = modes[(modes.index(self.zoom) + 1) % len(modes)]
            # the decoder starts again here with the new scaling
            self.seek_to = self.seek_to if self.seek_to is not None else self.position
            return "status"
        elif k in self.SEEK_KEYS:
            self.seek(self.SEEK_KEYS[k])
            return "status"
        elif k in ("5", "6"):
            # one frame back or forward; stepping pauses the video
            self.paused = True
            self.seek((-1.0 if k == "5" else 1.0) / self.fps)
            return "status"
        elif k == "8":
            self.set_mark(start=self.position)
            return "status"
        elif k == "9":
            self.set_mark(end=self.position)
            return "status"
        elif k == "0":
            self.position = self.seek_to = self.loop_start or 0.0
            return "status"
        elif k == "7":
            self.loop_start = self.loop_end = None
            return "status"
        elif k == "!":
            self.position = 0.0
            self.seek_to = 0.0
            return "status"
        elif k == " ":
            self.paused = not self.paused
            return "status"
        elif k == "r":
            # all settings back to the command line values
            self.apply_settings(self.start_settings)
            self.settings_version += 1
            if self.zoom != self.start_zoom:
                self.zoom = self.start_zoom
                self.seek_to = self.seek_to if self.seek_to is not None else self.position
            return "status"
        elif k == "h":
            return "toggle"
        elif k == "a":
            # audio mode: the PET plays the sound and shows a still frame
            self.audio = not self.audio
            if not self.audio:
                self.seek_to = self.position    # the video goes on from here
            say("audio mode" if self.audio else "video mode")
            return "status"
        else:
            return None
        self.apply_settings(self.settings())    # same values for both widths
        self.settings_version += 1
        return "status"

    def settings(self):
        m = self.matchers[80]
        return {"brightness": m.brightness, "contrast": m.contrast,
                "gamma": m.gamma, "dither": int(m.dither)}

    def apply_settings(self, st):
        for m in self.matchers.values():
            m.brightness, m.contrast = st["brightness"], st["contrast"]
            m.gamma, m.dither = st["gamma"], st["dither"]

    def status_items(self):
        """Groups of settings, each on its own rows, R)ESET last: it resets
        all of them. A value that changes its width is padded to its widest
        form, so that the items stay in place."""
        m = self.matchers[80]
        return [[f"B)RIGHTNESS:{m.brightness:+.2f}",
                 f"C)ONTRAST:{m.contrast:.1f}",
                 f"G)AMMA:{m.gamma:.1f}",
                 f"D)ITHER:{'ON' if int(m.dither) else 'OFF'}"],
                [f"Z)OOM:{self.zoom.upper():<7}",
                 f"A)UDIO:{'ON' if self.audio else 'OFF':<3}",
                 "R)ESET"]]

    # hotkeys as KEY)WHAT IT DOES, like the settings
    KEY_ITEMS = ["1)BACK 1MIN", "2)BACK 10S", "3)FWD 10S", "4)FWD 1MIN",
                 "5)FRAME BACK", "6)FRAME FWD", "!)REWIND", "@PAUSE",
                 "@LOOP",
                 "S)EARCH YOUTUBE", "H)ELP", "RUNSTOP)QUIT"]

    def loop_help(self):
        # the marks are padded to the widest time, "(1:23:45)", for the same reason
        mark = lambda t: f"({clock(t) if t is not None else 'NONE'})".ljust(9)
        return [f"8)LOOP START {mark(self.loop_start)}",
                f"9)LOOP END {mark(self.loop_end)}", "0)GO TO LOOP START", "7)CLEAR LOOP"]

    def overlay_lines(self, cols):
        """Settings, then hotkeys from a new line, packed into lines of
        `cols`, then the timeline."""
        keys = []
        for i in self.KEY_ITEMS:
            if i == "@PAUSE":
                keys.append(f"SPACE)PAUSE:{'ON' if self.paused else 'OFF':<3}")
            elif i == "@LOOP":
                keys += self.loop_help()
            else:
                keys.append(i)
        lines = []
        for group in self.status_items() + [keys]:
            lines += pack(group, cols - 1)
        lines = [l.rstrip() for l in lines]
        if self.duration:
            lines.append(self.timeline(cols - 1))
        return lines

    def timeline(self, width):
        """'0:00 --[--(4:34)---]-- 1:23:43', exactly `width` characters, with
        the current time at the current position and the loop marks."""
        long = self.duration >= 3600
        start = "0:00:00" if long else "0:00"
        end = clock(self.duration)
        mark = f"({clock(self.position)})"
        n = max(len(mark), width - len(start) - len(end) - 2)
        frac = min(1.0, max(0.0, self.position / self.duration))
        # centre the time on the position, but keep it inside the bar
        at = round(frac * (n - 1)) - len(mark) // 2
        at = min(max(at, 0), n - len(mark))
        bar = ["-"] * n
        for t, ch in ((self.loop_start, "["), (self.loop_end, "]")):
            if t is not None:
                bar[min(n - 1, round(t / self.duration * (n - 1)))] = ch
        bar[at:at + len(mark)] = mark       # the time wins over a mark
        return f"{start} {''.join(bar)} {end}"

    def status_text(self):
        """The settings for the terminal, like "B)rightness +0.05"."""
        out = []
        for item in sum(self.status_items(), []) + (["paused"] if self.paused else []):
            item = item.rstrip()
            out.append(item[:2] + item[2:].lower() if item[1] == ")" else item.lower())
        if self.loop_start is not None or self.loop_end is not None:
            mark = lambda t: clock(t) if t is not None else "none"
            out.append(f"loop {mark(self.loop_start)}-{mark(self.loop_end)}")
        return "  ".join(out)


class AudioFeed:
    """The packets of an audio session. A background thread makes the
    sound ahead: yt-dlp and the start of ffmpeg can take seconds, and a
    request from the PETdisk must not wait that long. packet() returns the
    next packet if its sound is ready, else a silent packet ("BUFFERING").
    Keeps at most AHEAD seconds of sound ready.

    The picture blocks make a slide show: the picture at the time of the
    packet in which the previous slide is complete, sent cell by cell
    where it differs from `screen`, what the PET shows when the feed
    starts. Cells in `keep` (the title) are left alone. With `clear`, the
    picture of the old video is cleared first. The picture goes
    into a packet only when the packet is sent, not when its sound is
    made, so that it is not AHEAD seconds late. Pictures are grabbed in
    another thread, because a grab starts ffmpeg (and yt-dlp for the
    first one), which takes about a second: the sound must not wait for
    it. A still video would ask for a picture at every packet, so a new
    one comes at most every SLIDE seconds of sound."""

    AHEAD = 20.0
    SLIDE = 5.0          # seconds of sound at least between two pictures
    RETRY = 10.0         # seconds of sound before the next try for a picture

    def __init__(self, source, start, rate, method, cols=80, screen=None, keep=range(0),
                 clear=False):
        self.lock = threading.Lock()
        self.sounds = collections.deque()    # (seconds, sound bytes) of the next packets
        self.limit = int(rate / 8 * self.AHEAD / AUDIO_SOUND)
        self.stopped = False
        self.source, self.cols, self.keep = source, cols, keep
        self.screen = bytearray(screen if screen is not None else bytes([32]) * (cols * 25))
        self.target, self.grab, self.next_grab = None, None, start
        if clear:
            # another video: first clear the old picture, then the slides
            self.target = bytes([32]) * (cols * 25)
        self.grabber = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        threading.Thread(target=self.run, args=(source, start, rate, method),
                         daemon=True).start()

    def run(self, source, start, rate, method):
        import convert
        media = source.get_media(audio=True)
        if media is None or self.stopped:
            return
        sound = convert.AudioStream(media, start, rate, log=say,
                                    loop_start=source.loop_start or 0.0, method=method)
        try:
            while not self.stopped:
                with self.lock:
                    full = len(self.sounds) >= self.limit
                if full:
                    time.sleep(0.05)
                    continue
                at = sound.pos
                data = sound.read(AUDIO_SOUND)
                with self.lock:
                    self.sounds.append((at, data))
        finally:
            sound.close()

    def ready(self):
        """Packets of sound ready."""
        with self.lock:
            return len(self.sounds)

    def packet(self):
        with self.lock:
            item = self.sounds.popleft() if self.sounds else None
        if item is None:
            return silent_packet("BUFFERING")
        at, data = item
        return audio_packet(time_field(at), self.picture(at), data)

    def picture(self, at):
        """The picture block for the packet at `at` seconds."""
        if self.grab is not None and self.grab.done():
            try:
                picture = self.grab.result()
            except Exception as e:               # ffmpeg or yt-dlp failed
                say(f"no picture: {e}")
                picture = None
            self.grab = None
            if picture is None:
                self.next_grab = at + self.RETRY
            else:
                self.target = picture
        block = None
        if self.target is not None:
            block = picture_block(self.screen, self.target, self.keep)
        if block is None and self.grab is None and at >= self.next_grab:
            # the slide is complete: the next one shows this moment
            self.grab = self.grabber.submit(self.source.still, at, self.cols)
            self.next_grab = at + self.SLIDE
        return block or b""

    def close(self):
        self.stopped = True
        self.grabber.shutdown(wait=False, cancel_futures=True)


class PausedFeed:
    """The packets of an audio session while the video is paused."""

    def packet(self):
        return silent_packet("PAUSED")

    def close(self):
        pass


class Stream:
    """The virtual VIDEO80.SEQ or VIDEO40.SEQ. Every OPEN on the PET reads
    from offset 0, which starts a new session: a key frame, then delta
    frames of whatever frame is due at that moment. When no new frame is
    due, the rest of the block is padding, so the PET gets each frame as
    soon as possible. In audio mode the session is the AUDIO marker and
    audio packets instead (see AudioFeed). The server can switch within the
    stream: to audio at a frame boundary, back to video with LEAVE at a
    packet boundary, and STOPPED when it stops."""

    KEEP = 65536     # bytes kept behind the newest request, for re-reads


    def __init__(self, source):
        self.source = source
        self.stats = Stats()
        self.cols = 80
        self.last_codes = None           # newest frame sent, for the terminal view
        self.t0 = time.monotonic()   # playback clock, kept across sessions
        self.overlay_on = False      # settings and hotkeys on the PET screen
        self.stopping = False        # Ctrl+C: tell the PET to end PDV, then stop
        self.stop_sent = False       # the PET got STOPPED, or 2 from PICK
        self.reset()

    def show_status(self, kind="status"):
        """H switches the overlay on and off. Other keys change values,
        which the overlay shows live while it is on."""
        if kind == "toggle":
            self.overlay_on = not self.overlay_on

    def overlay(self, codes):
        """Settings, hotkeys and timeline on the top lines. These lines are
        cleared over the full width, so no video shows between the texts."""
        if not self.overlay_on:
            return codes
        out = bytearray(codes)
        for row, text in enumerate(self.source.overlay_lines(self.cols)):
            line = text[:self.cols].ljust(self.cols)
            out[row * self.cols:(row + 1) * self.cols] = bytes(screen_code(ch) for ch in line)
        return bytes(out)

    def terminal_codes(self):
        """The screen for the terminal view: the search screen while it
        shows, with the rows of the RESULTS file, else what the PET shows.
        During a search the PET does not read the video stream, so the
        frame sent last would be an old video frame. In audio mode the PET
        shows a packet only when it plays its sound, after its buffer."""
        if self.source.menu is not None:
            return text_screen(self.source.results_lines(self.cols)[:23], self.cols)
        if self.audio is not None and self.snapshots:
            return self.snapshots[min(self.playing_packet(), len(self.snapshots) - 1)]
        return self.last_codes

    def playing_packet(self):
        """How many packets of this audio session the PET has played: it
        starts after AUDIO_PREFILL_BYTES, then plays the sound in time."""
        if self.play_t0 is None:
            return 0
        per_second = AUDIO_RATE / 8 / AUDIO_SOUND
        return int((time.monotonic() - self.play_t0) * per_second) + 1

    def reset(self):
        self.base = 0                # stream offset of buf[0]
        self.buf = bytearray()
        self.prev = None             # the frame the PET shows now
        self.last_index = None
        self.frames_sent = 0
        self.bytes_frames = 0
        if getattr(self, "audio", None) is not None:
            self.audio.close()
        self.audio = None            # the sound source of an audio session
        self.stop_here = False       # STOPPED is in this session
        self.resume_audio = False    # after a self-restart in audio mode: LEAVE first
        self.snapshots = []          # audio: the screen after each packet sent
        self.play_t0 = None          # audio: when the PET started to play

    def read(self, start, end, cols=80):
        if start == 0:
            self.reset()
            self.cols = cols
            self.source.set_cols(cols)
            self.source.close_results()      # back from the search screen
            say(f"PET opened the video, {cols} columns")
        if self.resume_audio:
            self.resume_audio = False
            self.buf += bytes([LEAVE])       # out of the old audio session
        if self.stopping:
            # the server stops: the PET ends PDV. At a frame or packet
            # boundary, after LEAVE in audio mode; then padding only
            if not self.stop_here:
                if self.audio is not None:
                    self.audio.close()
                    self.audio = None
                    self.buf += bytes([LEAVE])
                self.buf += bytes([STOPPED])
                self.stop_here = self.stop_sent = True
            self.buf += bytes([PAD]) * max(0, end - self.base - len(self.buf))
            return self.cut(start, end)
        if self.source.url is None:
            # nothing to play: send the PET to the search screen
            if start == 0:
                return bytes([SEARCH]) + bytes([PAD]) * (end - start - 1)
            return bytes([PAD]) * (end - start)
        while self.base + len(self.buf) < end:
            # the server can switch the mode within the stream: to audio at
            # a frame boundary, back to video at a packet boundary
            if self.audio is not None:
                self.next_packet()
                continue
            if self.source.audio:
                self.start_audio()
                continue
            if self.source.menu is None and not self.source.ready.is_set():
                # no frame yet (a new video starts): pad, do not keep the
                # PETdisk waiting
                self.buf += bytes([PAD]) * (end - self.base - len(self.buf))
                break
            index, codes = self.source.current(self.t0)
            if self.overlay_on:
                # a new frame also when only the overlay text changes
                text = tuple(self.source.overlay_lines(self.cols))
                codes = self.overlay(codes)
                index = (index, text)
            if index == self.last_index:
                self.buf += bytes([PAD]) * (end - self.base - len(self.buf))
                break
            frame = encode_frame(self.prev, codes)
            self.buf += frame
            self.prev = codes
            self.last_codes = codes
            self.last_index = index
            self.frames_sent += 1
            self.bytes_frames += len(frame)
            self.stats.frame(len(frame), 1 + len(codes))
        if self.audio is not None:
            self.audio_clock()
        return self.cut(start, end)

    def cut(self, start, end):
        """Bytes [start, end) of the stream; drops what is older than KEEP."""
        lo = max(0, start - self.base)
        out = bytes(self.buf[lo:end - self.base])
        drop = start - self.base - self.KEEP
        if drop > 0:
            del self.buf[:drop]
            self.base += drop
        return out

    def time_cells(self):
        """The cells of the time field in the title, which the PET writes
        itself."""
        timex = audio_title(self.cols, "")[1]
        return range(timex, timex + AUDIO_TIME)

    def new_feed(self, clear=False):
        """The sound source from the playback position on; `clear`: another
        video, so the picture of the old one is cleared first."""
        src = self.source
        at = src.seek_to if src.seek_to is not None else src.position
        src.seek_to = None
        src.position = at
        self.audio_start, self.audio_t0 = at, None
        self.audio_url, self.audio_paused = src.url, src.paused
        say(f"audio from {clock(at)}" + (", paused" if src.paused else ""))
        if src.paused or src.url is None:
            return PausedFeed()
        # the slide show leaves row 0 alone: the title is there
        return AudioFeed(src, at, AUDIO_RATE, AUDIO_METHOD, self.cols,
                         bytes(self.screen), range(0, self.cols), clear=clear)

    def next_packet(self):
        """The next audio packet. A change at the server takes effect here:
        audio mode off, another video, a seek or a pause."""
        src = self.source
        if not src.audio:
            self.audio.close()
            self.audio = None
            self.snapshots, self.play_t0 = [], None
            self.buf += bytes([LEAVE])       # at a packet start
            self.prev = self.last_index = None
            say("back to video")
            return
        if (src.url != self.audio_url or src.paused != self.audio_paused
                or src.seek_to is not None):
            self.audio.close()
            self.audio = self.new_feed(clear=src.url != self.audio_url)
        packet = self.audio.packet()
        self.buf += packet
        # what the PET shows after this packet: for the next feed and the
        # terminal view
        apply_block(self.screen, packet[AUDIO_TIME:AUDIO_TIME + AUDIO_PIC])
        cells = self.time_cells()
        self.screen[cells.start:cells.stop] = packet[:AUDIO_TIME]
        self.last_codes = bytes(self.screen)
        self.snapshots.append(self.last_codes)
        if self.play_t0 is None and len(self.snapshots) * AUDIO_PACKET >= AUDIO_PREFILL_BYTES:
            self.play_t0 = time.monotonic()  # the PET starts to play about now

    def start_audio(self):
        """Audio mode: the picture on the screen now, with the title and
        the time on row 0, the AUDIO marker, then the packets of the sound
        from the playback position on."""
        src = self.source
        at = src.seek_to if src.seek_to is not None else src.position
        cols = self.cols
        n = cols * 25
        # the last picture, if it is of this video: after a pick on the
        # PET in audio mode it is of the old one, so the screen is empty
        same = getattr(src, "latest_url", src.url) == src.url
        picture = (src.latest if src.latest is not None and len(src.latest) == n and same
                   else bytes([32]) * n)
        title = audio_title(cols, time_field(at))[0]
        screen = bytearray(picture)
        screen[:cols] = bytes(screen_code(ch) for ch in title.ljust(cols))
        self.screen = screen
        codes = bytes(screen)
        self.buf += encode_frame(None, codes) + bytes([AUDIO])
        self.prev = self.last_codes = codes
        self.snapshots, self.play_t0 = [codes], None
        self.audio = self.new_feed()

    def audio_clock(self):
        """The playback position in audio mode: the PET plays the sound
        about AUDIO_PREFILL seconds after it reads the first bytes."""
        now = time.monotonic()
        if self.audio_t0 is None:
            self.audio_t0 = now
        if not self.source.paused:
            self.source.position = self.audio_start + max(0.0, now - self.audio_t0 - AUDIO_PREFILL)

    def save_state(self):
        return {"elapsed": time.monotonic() - self.t0, "base": self.base,
                "buf": bytes(self.buf), "prev": self.prev,
                "last_index": self.last_index, "frames_sent": self.frames_sent,
                "bytes_frames": self.bytes_frames,
                "settings": self.source.settings(), "cols": self.cols,
                "position": self.source.position,
                "loop": (self.source.loop_start, self.source.loop_end),
                "zoom": self.source.zoom,
                "url": self.source.url, "menu": self.source.menu,
                "results": self.source.results,
                "audio": self.source.audio, "audio_session": self.audio is not None}

    def load_state(self, st):
        self.t0 = time.monotonic() - st["elapsed"]
        self.base = st["base"]
        self.buf = bytearray(st["buf"])
        self.prev = st["prev"]
        self.last_index = st["last_index"]
        self.frames_sent = st["frames_sent"]
        self.bytes_frames = st["bytes_frames"]
        if "settings" in st:
            self.source.apply_settings(st["settings"])
        self.cols = st.get("cols", 80)
        self.source.loop_start, self.source.loop_end = st.get("loop", (None, None))
        self.source.zoom = st.get("zoom", self.source.zoom)
        if "url" in st:
            self.source.url = st["url"]
            self.source.menu = st["menu"]
            self.source.results = st["results"]
        if st.get("position"):
            self.source.position = st["position"]
            self.source.seek_to = st["position"]
        self.source.set_cols(self.cols)
        # The PET may be in its audio loop: the sound source did not survive
        # the restart, so LEAVE at the packet boundary where buf ends, and
        # read() starts audio mode again from the position
        self.source.audio = st.get("audio", False)
        self.resume_audio = st.get("audio_session", False)

    def fps(self):
        dt = time.monotonic() - self.t0
        return self.frames_sent / dt if dt > 0 else 0.0

    def avg_frame(self):
        return self.bytes_frames / self.frames_sent if self.frames_sent else 0


DISPLAY = None    # the terminal view, when the server runs in a terminal


def say(msg):
    """One event line, with the time: in the terminal view's event list, or
    printed when there is no terminal view."""
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    if DISPLAY is not None:
        DISPLAY.event(line)
    else:
        print(line, file=sys.stderr, flush=True)


def spread(values):
    """min / 90th percentile / max of a list, as text."""
    if not values:
        return "-"
    v = sorted(values)
    p90 = v[min(len(v) - 1, int(len(v) * 0.9))]
    return f"{v[0]:.0f}/{p90:.0f}/{v[-1]:.0f}"


class Stats:
    """Request gaps, block and frame sizes over the last WINDOW seconds."""
    WINDOW = 5.0

    def __init__(self):
        self.gaps = collections.deque()      # (time, ms)
        self.frames = collections.deque()    # (time, bytes)
        self.blocks = collections.deque()    # (time, bytes)
        self.last_frame = None               # (bytes sent, bytes of a full frame)
        self.last_print = time.monotonic()

    def _add(self, q, v):
        now = time.monotonic()
        q.append((now, v))
        while q and q[0][0] < now - self.WINDOW:
            q.popleft()

    def gap(self, ms):
        self._add(self.gaps, ms)

    def block(self, n):
        self._add(self.blocks, n)

    def frame(self, sent, full):
        """sent: bytes of the frame as sent; full: bytes of a plain key
        frame, which has all screen codes."""
        self._add(self.frames, sent)
        self.last_frame = (sent, full)

    def summary(self):
        now = time.monotonic()
        cut = now - self.WINDOW
        fr = [v for t, v in self.frames if t >= cut]
        bl = [v for t, v in self.blocks if t >= cut]
        gp = [v for t, v in self.gaps if t >= cut]
        # frames in each of the last whole seconds: the slowest and the fastest
        per_second = [sum(1 for t, _ in self.frames if now - s - 1 <= t < now - s)
                      for s in range(int(self.WINDOW))]
        return {"fps": len(fr) / self.WINDOW, "fps_range": f"{min(per_second)}/{max(per_second)}",
                "kbs": sum(bl) / self.WINDOW / 1024,
                "frames": spread(fr), "gaps": spread(gp), "active": bool(bl),
                "last": self.last_frame_text()}

    def last_frame_text(self):
        """The last frame against a full frame: "last frame 231 B of
        2001 B (12%)"."""
        if self.last_frame is None:
            return ""
        sent, full = self.last_frame
        return f"last frame {sent} B of {full} B ({100 * sent / full:.0f}%)"

    def maybe_print(self):
        """Without the terminal view: a summary line every WINDOW seconds."""
        now = time.monotonic()
        if DISPLAY is not None or now - self.last_print < self.WINDOW:
            return
        self.last_print = now
        st = self.summary()
        if st["active"]:
            say(f"{st['fps']:4.1f} fps (min/max {st['fps_range']})  {st['kbs']:4.2f} KB/s  "
                f"frame bytes min/p90/max {st['frames']}  "
                f"request gap ms {st['gaps']}  {st['last']}")


def pack(items, width):
    """Items joined with two spaces, wrapped into lines of at most width."""
    lines, line = [], ""
    for item in items:
        item = item[:width]
        if line and len(line) + 2 + len(item) > width:
            lines.append(line)
            line = item
        else:
            line = f"{line}  {item}" if line else item
    if line:
        lines.append(line)
    return lines


class Display:
    """Full-screen terminal view: what the PET shows (PETSCII approximated
    with Unicode, green like the PET), or the search screen while the
    terminal search is open, then settings, live statistics, hotkeys and
    recent events. Redrawn at most 10 times a second."""

    GREEN, INV, NOINV, RESET = "\x1b[32m", "\x1b[7m", "\x1b[27m", "\x1b[0m"
    GREY = "\x1b[90m"                  # server-only text
    YELLOW = "\x1b[33m"                # the search field, easy to find
    RED = "\x1b[91m"                   # bright red: the video does not play
    BORDER = "\x1b[38;5;238m"          # dark grey frame border

    def __init__(self):
        import petscii_unicode
        self.map = petscii_unicode.build_map()
        self.events = collections.deque(maxlen=1)    # only the newest event
        self.lock = threading.Lock()
        self.next_draw = 0.0
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")   # own screen, no cursor
        sys.stdout.flush()

    def close(self):
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def event(self, line):
        with self.lock:
            self.events.append(line)

    def frame_lines(self, codes, cols, width):
        lines = []
        shown = min(cols, max(10, width - 2))
        for r in range(25):
            row = codes[r * cols:r * cols + shown] if codes else b""
            out, inv = [self.GREEN], False
            for c in row:
                rev = c >= 128
                if rev != inv:
                    out.append(self.INV if rev else self.NOINV)
                    inv = rev
                out.append(self.map[c & 0x7F])
            if inv:
                out.append(self.NOINV)
            out.append(" " * (shown - len(row)))
            lines.append(f"{self.BORDER}│" + "".join(out) + f"{self.RESET}{self.BORDER}│{self.RESET}")
        return ([f"{self.BORDER}┌" + "─" * shown + f"┐{self.RESET}"] + lines +
                [f"{self.BORDER}└" + "─" * shown + f"┘{self.RESET}"])

    def draw(self, stream, port, keys=None):
        now = time.monotonic()
        if now < self.next_draw:
            return
        self.next_draw = now + 0.1
        width = shutil.get_terminal_size((100, 40)).columns
        st = stream.stats.summary()
        src = stream.source
        box = self.frame_lines(stream.terminal_codes(), stream.cols, width)
        w = min(width, stream.cols + 2)        # text no wider than the frame box
        if src.menu is not None:
            now_playing = "Now: YouTube search"
        elif src.url:
            now_playing = f"Now: Playing ({src.url})"
        else:
            now_playing = "Now: nothing playing"
        lines = [f"PETdisk Video ({stream.cols} cols)  {now_playing}"[:w]]
        lines += box
        if keys is not None and keys.typing is not None:
            field = f"number, N/P page or search words: {keys.typing}_"[:w]
            lines.append(f"{self.YELLOW}{field}{self.RESET}")
        # the same settings, help and timeline lines as on the PET
        lines += src.overlay_lines(w + 1)
        # what only the server shows, in grey
        server = pack(["server only:",
                       "arrows: up/down 1 min, right/left 10 s",
                       "lowercase b/c/g up, uppercase down",
                       "s: YouTube search here", "Ctrl-C stops (the PET first)"], w)
        if st["active"]:
            server += pack([f"{st['fps']:4.1f} fps (min/max {st['fps_range']})",
                            f"{st['kbs']:4.2f} KB/s",
                            f"frame bytes min/p90/max {st['frames']}",
                            f"request gap ms {st['gaps']}", "(last 5 s)"], w)
            server += pack([st["last"]], w)
        else:
            server.append("waiting for the PET to read the video"[:w])
        with self.lock:
            server += [e[:w] for e in self.events]
        lines += [f"{self.GREY}{l}{self.RESET}" for l in server]
        retry = src.retry_text()
        if retry:
            lines.append(f"{self.RED}{retry[:w]}{self.RESET}")
        if stream.stopping:
            text = ("stopping: the PET has ended PDV" if stream.stop_sent
                    else "stopping: waiting for the PET to end PDV (Ctrl-C again stops at once)")
            lines.append(f"{self.RED}{text[:width]}{self.RESET}")
        sys.stdout.write("\x1b[H" + "\n".join(l + "\x1b[K" for l in lines) + "\x1b[J")
        sys.stdout.flush()


class TerminalKeys:
    """The same hotkeys as on the PET, typed in the server's terminal:
    lowercase raises a value, uppercase lowers it, d cycles dithering."""

    def __init__(self):
        self.fd = None
        self.typing = None           # YouTube search text while typing it
        if not sys.stdin.isatty():
            return
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        tty.setcbreak(self.fd)
        import atexit
        atexit.register(self.restore)

    def restore(self):
        # Turn line editing and echo back on explicitly: after a self-restart
        # the saved state is already the key-by-key mode.
        if self.fd is not None:
            import termios
            attrs = termios.tcgetattr(self.fd)
            attrs[3] |= termios.ICANON | termios.ECHO
            termios.tcsetattr(self.fd, termios.TCSADRAIN, attrs)

    def poll(self, stream):
        if self.fd is None:
            return
        import select
        data = b""
        while select.select([self.fd], [], [], 0)[0]:
            data += os.read(self.fd, 64)
        for key in self.split_keys(data):
            if self.typing is not None:
                if key in ("\r", "\n"):
                    stream.source.entry(self.typing.strip())
                    # after a search or a page turn the results show: go on
                    # typing a number, N or P. A picked video or an empty
                    # entry closes the search screen, and the field with it.
                    self.typing = "" if stream.source.menu is not None else None
                elif key == "\x1b":
                    self.typing = None
                    stream.source.close_results()
                elif key in ("\x7f", "\x08"):
                    self.typing = self.typing[:-1]
                elif len(key) == 1 and key >= " ":
                    self.typing += key
                continue
            if key == "s":
                self.typing = ""
                stream.source.show_results()
                continue
            ch = self.ARROWS.get(key, key)
            kind = stream.source.command(ch)
            if kind:
                stream.show_status(kind)
                say(f"key {self.NAMES.get(key, key)}")

    # cursor keys seek: up +1 min, down -1 min, right +10 s, left -10 s
    ARROWS = {"\x1b[A": "4", "\x1b[B": "1", "\x1b[C": "3", "\x1b[D": "2",
              "\x1bOA": "4", "\x1bOB": "1", "\x1bOC": "3", "\x1bOD": "2"}
    NAMES = {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
             "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left"}

    @staticmethod
    def split_keys(data):
        """Bytes typed in the terminal -> keys; escape sequences stay whole."""
        text = data.decode("latin-1", "replace")
        keys, i = [], 0
        while i < len(text):
            if text[i] == "\x1b" and i + 2 < len(text) and text[i + 1] in "[O":
                keys.append(text[i:i + 3])
                i += 3
            else:
                keys.append(text[i])
                i += 1
        return keys


def find_file(name):
    """Case-insensitive lookup in the serve directory, like petdisk.php."""
    want = name.lower()
    for entry in os.listdir(SERVE_DIR):
        if entry.lower() == want:
            return os.path.join(SERVE_DIR, entry)
    return None


def directory_page(page):
    if page > 0:
        return b"\n"
    names = sorted(e.upper() for e in os.listdir(SERVE_DIR)
                   if e.lower().endswith(LIST_EXT))
    # VIDEO80.SEQ and VIDEO40.SEQ are not listed: the player opens them by name
    body = "".join(n + "\n" for n in names) + "\n"
    return body.encode("ascii", "replace")


def response(body, status="200 OK"):
    head = (f"HTTP/1.0 {status}\r\nContent-Length: {len(body)}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    data = head + body
    if len(data) > MAX_RESPONSE:
        say(f"WARNING: response {len(data)} bytes exceeds {MAX_RESPONSE}")
    return data


def handle(query, stream, log):
    """The answer to one petdisk.php request. The PETdisk MAX firmware does
    not look at the HTTP status: it reads a size from the body of a size
    request, and with an empty body it gets a random size. So a size
    request always gets a size, 0 for a missing file (the PET then reports
    FILE NOT FOUND), and a directory request always gets an empty line at
    the end, also when the request is broken."""
    try:
        return answer(query, stream, log)
    except Exception as e:                   # a broken request: answer safely
        say(f"bad request {query[:60]!r}: {e}")
        params = query.split("&")
        if "d=1" in params:
            body = b"\n"                      # an empty directory page
        elif "l=1" in params:
            body = b"0\r\n"                  # size 0: FILE NOT FOUND
        else:
            body = b""
        return response(body, "400 Bad Request")


def answer(query, stream, log):
    q = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
    if q.get("d") == "1":
        page = int(q.get("p", "0") or 0)
        log(f"dir page {page}")
        return response(directory_page(page))

    name = q.get("file")
    if name is None:
        return response(b"0\r\n", "400 Bad Request")
    video_cols = VIDEO_NAMES.get(name.upper())
    is_video = video_cols is not None

    # The search screen text, read by the PET's BASIC part
    res = re.fullmatch(r"RESULTS(80|40)(\.SEQ)?", name.upper())
    if res:
        cols = int(res.group(1))
        if q.get("l") == "1" or cols not in stream.source.results_snapshot:
            stream.source.results_snapshot[cols] = stream.source.results_file(cols)
        data = stream.source.results_snapshot[cols]
        if q.get("l") == "1":
            return response(f"{len(data)}\r\n".encode())
        if "s" in q and "e" in q:
            return response(data[int(q["s"]):int(q["e"])])

    # The PET's search screen asks every few seconds whether the search
    # screen was closed on the server, for example by a video picked in the
    # server terminal: "1" makes the PET start the video
    if re.fullmatch(r"PICK(\.SEQ)?", name.upper()):
        src = stream.source
        if stream.stopping:
            data = b"2"                      # the server stops: end PDV
            if "s" in q:
                stream.stop_sent = True
        else:
            data = b"1" if src.menu is None and src.url is not None else b"0"
        if q.get("l") == "1":
            return response(f"{len(data)}\r\n".encode())
        if "s" in q and "e" in q:
            return response(data[int(q["s"]):int(q["e"])])

    # A real file in serve/ wins over the command names below, so that a
    # file whose name starts with Q, K or C can be loaded
    path = None if is_video else find_file(name)

    # An entry in the search field: C<number>, CN or CP
    ent = re.fullmatch(r"C(\d+|N|P)(\.SEQ)?", name.upper())
    if ent and path is None and q.get("l") == "1":
        say(f"PET entry {ent.group(1)}")
        stream.source.entry(ent.group(1))
        return response(b"0\r\n")

    # Search text from the PET: files named Q<text>, then an empty Q
    qry = re.fullmatch(r"Q(.*?)(\.SEQ|\.PRG)?", name.upper(), re.S)
    if qry and path is None and q.get("l") == "1":
        stream.source.add_query_part(qry.group(1))
        return response(b"0\r\n")

    # Hotkey from the PET: it opens a file named K<key code>, for example
    # K66.SEQ. Answer "size 0", so the open fails and nothing is read.
    cmd = re.fullmatch(r"K(\d+)(\.SEQ|\.PRG)?", name.upper())
    if cmd and path is None and q.get("l") == "1":
        key = int(cmd.group(1))
        k = stream.source.pet_key(key)
        kind = stream.source.command(k)
        if kind:
            stream.show_status(kind)
            say(f"PET key {k}")
        else:
            say(f"PET key {key}: ignored")
        return response(b"0\r\n")

    if q.get("l") == "1":
        if is_video:
            size = VIDEO_SIZE
        elif path is None:
            log(f"size {name}: not found")
            return response(b"0\r\n")          # size 0: FILE NOT FOUND on the PET
        else:
            size = os.path.getsize(path)
        log(f"size {name} = {size}")
        return response(f"{size}\r\n".encode())

    if "s" in q and "e" in q:
        start, end = int(q["s"]), int(q["e"])
        # The PETdisk MAX asks for 512 bytes at a time. Refuse other sizes,
        # so that nobody on the network can make the server build huge
        # amounts of video data in memory.
        if start < 0 or not 0 <= end - start <= MAX_RANGE:
            log(f"refused range [{start},{end}) of {name}")
            return response(b"", "416 Range Not Satisfiable")
        if is_video:
            body = stream.read(start, end, video_cols)
            stream.stats.block(len(body))
            return response(body)
        if path is None:
            log(f"range {name}: not found")
            return response(b"")
        with open(path, "rb") as f:
            f.seek(start)
            body = f.read(max(0, end - start))
        log(f"range {name} [{start},{end})")
        return response(body)

    return response(b"", "400 Bad Request")


TIMING_LOG = os.path.join(HERE, "build", "timing.log")
STATE_FILE = os.path.join(HERE, "build", "stream_state.pickle")
_last_done = [None]


def timing(query, t_accept, t_req, t_done, stream=None):
    """Append one line per request to build/timing.log (milliseconds):
    gap since the previous response finished, time to read the request,
    time to answer, and the query."""
    gap = (t_accept - _last_done[0]) * 1000 if _last_done[0] else -1
    _last_done[0] = t_done
    if stream is not None and gap > 0 and "VIDEO" in query.upper():
        stream.stats.gap(gap)
    try:
        with open(TIMING_LOG, "a") as f:
            f.write(f"{time.time():.3f} gap={gap:.1f} read={(t_req - t_accept) * 1000:.1f} "
                    f"send={(t_done - t_req) * 1000:.1f} "
                    f"frames={stream.frames_sent if stream else 0} {query}\n")
    except OSError:
        pass


STOP_WAIT = 20.0     # seconds at most for the PET to end PDV on Ctrl+C


def graceful_stop(stream):
    """Ctrl+C and SIGTERM: the first one makes the PET end PDV before the
    server stops, because the PETdisk crashes when it cannot connect. The
    second one stops at once."""
    import signal

    def handler(signum, frame):
        if stream.stopping:
            raise KeyboardInterrupt
        stream.stopping = time.monotonic()
        say("stopping: the PET ends PDV first (Ctrl-C again stops at once)")

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def serve(port, stream, verbose, keys):
    def log(msg):
        if verbose:
            say(msg)

    # The PETdisk MAX firmware crashes if a connection is refused, so the
    # listening socket must stay open. A self-restart passes it on to the new
    # process, and connections wait in the backlog meanwhile.
    inherited = os.environ.pop("PETDISK_VIDEO_FD", None)
    if inherited:
        srv = socket.socket(fileno=int(inherited))
    else:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", port))
        srv.listen(16)
    srv.settimeout(0.2)
    say(f"serving {SERVE_DIR} on port {port}")
    watched = {f: os.path.getmtime(f) for f in WATCH}
    next_check = 0.0
    last_request = time.monotonic()
    while True:
        now = time.monotonic()
        if stream.stopping:
            # stop when the PET has ended PDV and sends no more requests, or
            # when no PET runs PDV (no requests), or after STOP_WAIT at most
            idle = now - last_request
            if ((stream.stop_sent and idle > 2.0) or idle > 4.0
                    or now - stream.stopping > STOP_WAIT):
                say("stopped")
                return
        if now >= next_check:
            next_check = now + 1.0
            if any(os.path.getmtime(f) != t for f, t in watched.items()):
                say("code changed, restarting server")
                srv.settimeout(None)
                os.set_inheritable(srv.fileno(), True)
                os.environ["PETDISK_VIDEO_FD"] = str(srv.fileno())
                if stream is not None:
                    # the PET is in the middle of the stream: the new process
                    # must continue with exactly the same bytes
                    with open(STATE_FILE, "wb") as f:
                        pickle.dump(stream.save_state(), f)
                    os.environ["PETDISK_VIDEO_STATE"] = STATE_FILE
                os.execv(sys.executable, [sys.executable] + sys.argv)
        stream.stats.maybe_print()
        keys.poll(stream)
        if DISPLAY is not None:
            DISPLAY.draw(stream, port, keys)
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        t_accept = time.monotonic()
        try:
            conn.settimeout(1.0)             # the PETdisk sends its request at once:
                                             # a silent client must not hold the PET up
            req = b""
            while b"\r\n\r\n" not in req and len(req) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                req += chunk
            line = req.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split(" ")
            if len(parts) < 2 or parts[0] != "GET":
                conn.sendall(response(b"", "405 Method Not Allowed"))
            else:
                query = urlsplit(parts[1]).query
                t_req = time.monotonic()
                conn.sendall(handle(query, stream, log))
                t_done = last_request = time.monotonic()
                timing(query, t_accept, t_req, t_done, stream)
        except (OSError, ValueError) as e:
            log(f"error: {e}")
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000,
                    help="TCP port, default 8000; must match the port in PETDISK.CFG")
    ap.add_argument("source", metavar="VIDEO_OR_URL", nargs="?",
                    help="video file (loops) or URL (through yt-dlp), converted "
                         "while it plays; without it the PET starts with a "
                         "YouTube search")
    ap.add_argument("--fps", type=float, default=10.0, help="conversion frame rate")
    ap.add_argument("--zoom", choices=("fit", "fill", "stretch"), default="fit",
                    help="fit: whole video with black bars (default); fill: whole "
                         "screen, video cut at the edges; stretch: both, distorted")
    ap.add_argument("--aspect", type=float, default=1.8,
                    help="width / height of the PET picture on the monitor, default 1.8 "
                         "(80-column PET in graphics mode, estimated); raise it if "
                         "circles look tall, lower it if they look wide")
    ap.add_argument("--brightness", type=float, default=0.0, help="-1..1, default 0")
    ap.add_argument("--contrast", type=float, default=1.0, help="default 1")
    ap.add_argument("--gamma", type=float, default=1.0, help="default 1")
    ap.add_argument("--dither", type=int, default=0, choices=(0, 1),
                    help="0 none (default), 1 Floyd-Steinberg")
    ap.add_argument("--audio-method", choices=("clip", "pdm"), default="clip",
                    help="1-bit sound in audio mode (A key): clip, the sign of the sound "
                         "(default, suits the small PET speaker), or pdm, delta-sigma")
    ap.add_argument("--stick", type=float, default=6.0,
                    help="keep a cell's previous glyph if it is at most this many "
                         "pixels worse; fewer changes per frame (default 6, 0 = off)")
    ap.add_argument("--range", type=int, default=4000,
                    help="brightness range (2000 ~ pixel A, 4000 ~ pixel B, 0 = off)")
    ap.add_argument("--quiet", action="store_true", help="fewer event lines")
    ap.add_argument("--plain", action="store_true",
                    help="print log lines instead of the full-screen view")
    a = ap.parse_args()
    global AUDIO_METHOD
    AUDIO_METHOD = a.audio_method

    import convert
    matchers = {cols: convert.Matcher(cols, dither=a.dither, brightness=a.brightness,
                                      contrast=a.contrast, gamma=a.gamma,
                                      brightness_range=a.range, stick=a.stick)
                for cols in (80, 40)}
    stream = Stream(LiveSource(a.source, matchers, a.fps, a.zoom, a.aspect))
    keys = TerminalKeys()
    global DISPLAY
    if not a.plain and sys.stdout.isatty():
        DISPLAY = Display()
    state = os.environ.pop("PETDISK_VIDEO_STATE", None)
    if state:
        try:
            with open(state, "rb") as f:
                stream.load_state(pickle.load(f))
            say("continuing the stream after restart")
        except (OSError, KeyError, pickle.PickleError) as e:
            say(f"could not restore stream state: {e}")
    say(stream.source.status_text())
    if DISPLAY is None and keys.fd is not None:
        say("keys: b/B brightness, c/C contrast, g/G gamma, d dither, z zoom, r reset b/c/g/d/z, "
            "1/2/3/4 seek -60/-10/+10/+60 s, 5/6 frame -1/+1, ! rewind to start, space pause, s search, "
            "h toggle status/help on the PET, a audio mode (lowercase up, uppercase down)")
    graceful_stop(stream)
    try:
        serve(a.port, stream, not a.quiet, keys)
    except KeyboardInterrupt:
        pass
    finally:
        keys.restore()
        if DISPLAY is not None:
            DISPLAY.close()


if __name__ == "__main__":
    main()
