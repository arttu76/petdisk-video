#!/usr/bin/env python3
"""PETSCII conversion for server.py: video frames to PET screen codes.

Screen code 0xFF is never emitted, because the stream uses it as the key
frame marker.
"""
import json
import shutil
import subprocess

import numpy as np
from PIL import Image

SYNC = 0xFF
ROWS = 25
# The 128 glyphs of the PET character ROM (901447-08) in uppercase/graphics
# mode, 8 bytes each, one byte per pixel row, bit 7 is the leftmost pixel.
# Screen codes 128-255 are the same glyphs in reverse video.
PET_GLYPHS = bytes.fromhex("""
    1c 22 4a 56 4c 20 1e 00
    18 24 42 7e 42 42 42 00
    7c 22 22 3c 22 22 7c 00
    1c 22 40 40 40 22 1c 00
    78 24 22 22 22 24 78 00
    7e 40 40 78 40 40 7e 00
    7e 40 40 78 40 40 40 00
    1c 22 40 4e 42 22 1c 00
    42 42 42 7e 42 42 42 00
    1c 08 08 08 08 08 1c 00
    0e 04 04 04 04 44 38 00
    42 44 48 70 48 44 42 00
    40 40 40 40 40 40 7e 00
    42 66 5a 5a 42 42 42 00
    42 62 52 4a 46 42 42 00
    18 24 42 42 42 24 18 00
    7c 42 42 7c 40 40 40 00
    18 24 42 42 4a 24 1a 00
    7c 42 42 7c 48 44 42 00
    3c 42 40 3c 02 42 3c 00
    3e 08 08 08 08 08 08 00
    42 42 42 42 42 42 3c 00
    42 42 42 24 24 18 18 00
    42 42 42 5a 5a 66 42 00
    42 42 24 18 24 42 42 00
    22 22 22 1c 08 08 08 00
    7e 02 04 18 20 40 7e 00
    3c 20 20 20 20 20 3c 00
    00 40 20 10 08 04 02 00
    3c 04 04 04 04 04 3c 00
    00 08 1c 2a 08 08 08 08
    00 00 10 20 7f 20 10 00
    00 00 00 00 00 00 00 00
    08 08 08 08 00 00 08 00
    24 24 24 00 00 00 00 00
    24 24 7e 24 7e 24 24 00
    08 1e 28 1c 0a 3c 08 00
    00 62 64 08 10 26 46 00
    30 48 48 30 4a 44 3a 00
    04 08 10 00 00 00 00 00
    04 08 10 10 10 08 04 00
    20 10 08 08 08 10 20 00
    08 2a 1c 3e 1c 2a 08 00
    00 08 08 3e 08 08 00 00
    00 00 00 00 00 08 08 10
    00 00 00 7e 00 00 00 00
    00 00 00 00 00 18 18 00
    00 02 04 08 10 20 40 00
    3c 42 46 5a 62 42 3c 00
    08 18 28 08 08 08 3e 00
    3c 42 02 0c 30 40 7e 00
    3c 42 02 1c 02 42 3c 00
    04 0c 14 24 7e 04 04 00
    7e 40 78 04 02 44 38 00
    1c 20 40 7c 42 42 3c 00
    7e 42 04 08 10 10 10 00
    3c 42 42 3c 42 42 3c 00
    3c 42 42 3e 02 04 38 00
    00 00 08 00 00 08 00 00
    00 00 08 00 00 08 08 10
    0e 18 30 60 30 18 0e 00
    00 00 7e 00 7e 00 00 00
    70 18 0c 06 0c 18 70 00
    3c 42 02 0c 10 00 10 00
    00 00 00 00 ff 00 00 00
    08 1c 3e 7f 7f 1c 3e 00
    10 10 10 10 10 10 10 10
    00 00 00 ff 00 00 00 00
    00 00 ff 00 00 00 00 00
    00 ff 00 00 00 00 00 00
    00 00 00 00 00 ff 00 00
    20 20 20 20 20 20 20 20
    04 04 04 04 04 04 04 04
    00 00 00 00 e0 10 08 08
    08 08 08 04 03 00 00 00
    08 08 08 10 e0 00 00 00
    80 80 80 80 80 80 80 ff
    80 40 20 10 08 04 02 01
    01 02 04 08 10 20 40 80
    ff 80 80 80 80 80 80 80
    ff 01 01 01 01 01 01 01
    00 3c 7e 7e 7e 7e 3c 00
    00 00 00 00 00 00 ff 00
    36 7f 7f 7f 3e 1c 08 00
    40 40 40 40 40 40 40 40
    00 00 00 00 03 04 08 08
    81 42 24 18 18 24 42 81
    00 3c 42 42 42 42 3c 00
    08 1c 2a 77 2a 08 08 00
    02 02 02 02 02 02 02 02
    08 1c 3e 7f 3e 1c 08 00
    08 08 08 08 ff 08 08 08
    a0 50 a0 50 a0 50 a0 50
    08 08 08 08 08 08 08 08
    00 00 01 3e 54 14 14 00
    ff 7f 3f 1f 0f 07 03 01
    00 00 00 00 00 00 00 00
    f0 f0 f0 f0 f0 f0 f0 f0
    00 00 00 00 ff ff ff ff
    ff 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 ff
    80 80 80 80 80 80 80 80
    aa 55 aa 55 aa 55 aa 55
    01 01 01 01 01 01 01 01
    00 00 00 00 aa 55 aa 55
    ff fe fc f8 f0 e0 c0 80
    03 03 03 03 03 03 03 03
    08 08 08 08 0f 08 08 08
    00 00 00 00 0f 0f 0f 0f
    08 08 08 08 0f 00 00 00
    00 00 00 00 f8 08 08 08
    00 00 00 00 00 00 ff ff
    00 00 00 00 0f 08 08 08
    08 08 08 08 ff 00 00 00
    00 00 00 00 ff 08 08 08
    08 08 08 08 f8 08 08 08
    c0 c0 c0 c0 c0 c0 c0 c0
    e0 e0 e0 e0 e0 e0 e0 e0
    07 07 07 07 07 07 07 07
    ff ff 00 00 00 00 00 00
    ff ff ff 00 00 00 00 00
    00 00 00 00 00 ff ff ff
    01 01 01 01 01 01 01 ff
    00 00 00 00 f0 f0 f0 f0
    0f 0f 0f 0f 00 00 00 00
    08 08 08 08 f8 00 00 00
    f0 f0 f0 f0 00 00 00 00
    f0 f0 f0 f0 0f 0f 0f 0f
""")


def load_glyphs(rom_path=None, allow_reverse=True):
    """Return (glyphs, codes): glyph pixels as (N, 64) floats 0/255 and the
    screen code of each glyph. The graphics set is the first 1024 bytes of a
    PET character ROM: 128 glyphs x 8 rows, bit 7 is the leftmost pixel."""
    if rom_path is None:
        rom = np.frombuffer(PET_GLYPHS, dtype=np.uint8)
    else:
        rom = np.fromfile(rom_path, dtype=np.uint8)
        if rom.size < 1024:
            raise SystemExit(f"{rom_path}: too small for a PET character ROM")
    bits = np.unpackbits(rom[:1024].reshape(128, 8), axis=1)
    glyphs = bits.astype(np.float32) * 255.0
    codes = np.arange(128, dtype=np.uint8)
    if allow_reverse:
        glyphs = np.concatenate([glyphs, 255.0 - glyphs])
        codes = np.concatenate([codes, codes + 128])
    return glyphs, codes


class Matcher:
    def __init__(self, cols=80, rom=None, allow_reverse=True,
                 dither=False, brightness=0.0, contrast=1.0, gamma=1.0,
                 brightness_range=4000, stick=6.0):
        self.cols = cols
        self.width = cols * 8
        self.height = ROWS * 8
        self.frame_size = 1 + cols * ROWS
        self.glyphs, self.codes = load_glyphs(rom, allow_reverse)
        self.glyph_norm = (self.glyphs * self.glyphs).sum(axis=1)[None, :]
        self.exclude = self.codes == SYNC
        self.glyph_sum = self.glyphs.sum(axis=1)[None, :]
        self.code_index = np.full(256, -1, dtype=np.int64)
        self.code_index[self.codes] = np.arange(len(self.codes))
        # bitfixer's "pixel" match: only glyphs whose total brightness (sum of
        # 64 pixels, 0..16320) is within this range of the cell's total are
        # candidates. About 2000 is his "pixel A", 4000 "pixel B". 0 = off.
        self.brightness_range = brightness_range
        # Keep the previous frame's glyph in a cell when it is at most this
        # many pixels worse than the best glyph. Fewer changed cells means
        # smaller delta frames and less flicker.
        self.stick = stick * 255.0 * 255.0
        self.dither = dither
        self.brightness = brightness
        self.contrast = contrast
        self.gamma = gamma

    def adjust(self, gray):
        """Brightness, contrast and gamma, same formulas as petscreens/convert.html."""
        g = gray.astype(np.float32) + self.brightness * 255.0
        g = (g - 128.0) * self.contrast + 128.0
        n = np.clip(g / 255.0, 0.0, 1.0)
        if self.gamma != 1.0:
            n = np.power(n, 1.0 / self.gamma)
        return n * 255.0

    STICK_RATIO = 0.5
    DITHER_MODES = ("none", "floyd-steinberg")

    def prepare(self, gray):
        """gray: (height, width) uint8 -> float32 target pixels 0..255."""
        g = self.adjust(gray)
        mode = self.DITHER_MODES[int(self.dither) % len(self.DITHER_MODES)]
        if mode == "floyd-steinberg":
            img = Image.fromarray(np.clip(g, 0, 255).astype(np.uint8), "L")
            g = np.asarray(img.convert("1"), dtype=np.float32) * 255.0
        return g

    def codes_for(self, gray, prev=None):
        """Pick the closest glyph for every 8x8 cell. Returns (ROWS*cols,) uint8."""
        target = self.prepare(gray)
        tiles = (target.reshape(ROWS, 8, self.cols, 8)
                 .transpose(0, 2, 1, 3)
                 .reshape(ROWS * self.cols, 64))
        # squared distance up to a per-tile constant: -2 t.g + |g|^2
        dist = -2.0 * (tiles @ self.glyphs.T) + self.glyph_norm
        dist[:, self.exclude] = np.inf
        if self.brightness_range > 0:
            far = np.abs(self.glyph_sum - tiles.sum(axis=1)[:, None]) > self.brightness_range
            far |= self.exclude[None, :]
            # a cell with no glyph inside the range falls back to all glyphs
            far[far.all(axis=1)] = self.exclude
            dist[far] = np.inf
        best = dist.argmin(axis=1)
        if prev is not None and self.stick > 0:
            cells = np.arange(len(best))
            prev_idx = self.code_index[np.asarray(prev, dtype=np.uint8)]
            keep = (prev_idx >= 0)
            pi = np.where(keep, prev_idx, 0)
            # dist omits the cell's own |t|^2; add it back for real errors
            own = (tiles * tiles).sum(axis=1)
            best_err = dist[cells, best] + own
            # The margin is at most `stick` pixels, and at most STICK_RATIO
            # of the best glyph's own error: a cell that the best glyph
            # fits almost exactly (a blank on black) gets almost no margin,
            # so moving objects leave no trail.
            margin = np.minimum(self.stick, self.STICK_RATIO * best_err)
            keep &= dist[cells, pi] <= dist[cells, best] + margin
            best = np.where(keep, pi, best)
        return self.codes[best]

    def frame_bytes(self, gray, prev=None):
        return bytes([SYNC]) + self.codes_for(gray, prev).tobytes()

    def render(self, frame, scale_to_4_3=True):
        """Frame bytes -> PIL image of what the PET shows (for previews)."""
        codes = np.frombuffer(frame[1:], dtype=np.uint8)
        full, fcodes = load_glyphs(None, True)
        lut = np.zeros((256, 64), dtype=np.float32)
        lut[fcodes] = full
        px = lut[codes].reshape(ROWS, self.cols, 8, 8).transpose(0, 2, 1, 3)
        img = Image.fromarray(px.reshape(self.height, self.width).astype(np.uint8), "L")
        if scale_to_4_3:
            img = img.resize((self.width, self.width * 3 // 4), Image.NEAREST)
        return img


ZOOM_MODES = ("fit", "fill", "stretch")


def ffmpeg_filter(width, height, stretch, aspect=4 / 3, zoom="fit"):
    """The ffmpeg filter that makes PET-sized gray frames. The PET picture
    on the monitor has the shape width / height = aspect. The PDV client
    selects 8-line rows on BASIC 4 machines (see stub.bas.in), which makes
    the picture wider than the 10-line text mode. Zoom modes:
      fit      the whole video, with black bars where the shapes differ
      fill     the whole screen, the video is cut where the shapes differ
      stretch  the whole video on the whole screen, distorted"""
    if stretch or zoom == "stretch":
        return f"scale={width}:{height}:flags=area,format=gray"
    # Fit or fill a canvas with the shape of the PET picture, then squash
    # the canvas to the pixel grid so the picture keeps its shape on screen.
    canvas_h = round(width / aspect)
    if zoom == "fill":
        fit = (f"scale={width}:{canvas_h}:force_original_aspect_ratio=increase,"
               f"crop={width}:{canvas_h}")
    else:
        fit = (f"scale={width}:{canvas_h}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{canvas_h}:-1:-1")
    return f"{fit},scale={width}:{height}:flags=area,format=gray"


def _pipe_lines(stream, log):
    import threading

    def run():
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                log(line)
    threading.Thread(target=run, daemon=True).start()


def _js_runtime_args():
    """yt-dlp needs a JavaScript runtime for YouTube. By default it only
    looks for deno, so name another runtime when one is installed."""
    if shutil.which("deno"):
        return []
    for rt in ("node", "bun", "qjs"):
        path = shutil.which(rt)
        if path:
            return ["--js-runtimes", f"{'quickjs' if rt == 'qjs' else rt}:{path}"]
    return []


# The PET picture is at most 640x200 pixels (80 columns x 8), so 360p is
# enough: it is 640 pixels wide. H.264 takes about a third of the CPU time
# of AV1 or VP9 to decode. Fall back to other codecs and to 480p.
YTDLP_FORMAT = ("bv*[height<=360][vcodec^=avc1][protocol^=http]/"
                "bv*[height<=360][protocol^=http]/b[height<=360]/"
                "bv*[height<=480][protocol^=http]/b")
# For audio mode: the sound only. The video formats above have no sound on
# YouTube, where video and audio are separate streams.
YTDLP_AUDIO_FORMAT = "ba[protocol^=http]/ba/b[protocol^=http]/b"


def is_url(source):
    return source.startswith(("http://", "https://"))


class Media(str):
    """A media URL with the HTTP headers that ffmpeg must send for it."""
    def __new__(cls, url, headers):
        obj = super().__new__(cls, url)
        obj.headers = headers
        return obj


def resolve(source, log=None, audio=False):
    """(media URL or file name, duration in seconds or None). For a web page
    URL, yt-dlp finds the direct media URL, which ffmpeg can seek in: the
    video stream, or with audio=True the sound stream."""
    if not is_url(source):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", source],
                capture_output=True, text=True, timeout=30).stdout.strip()
            return source, float(out)
        except (ValueError, subprocess.SubprocessError):
            return source, None
    if not shutil.which("yt-dlp"):
        raise SystemExit("yt-dlp not found")
    res = subprocess.run(
        ["yt-dlp", "-q", "--no-warnings"] + _js_runtime_args() +
        ["-f", YTDLP_AUDIO_FORMAT if audio else YTDLP_FORMAT, "-O", "duration", "-O", "%(http_headers)j",
         "-O", "urls", source],
        capture_output=True, text=True, timeout=120)
    if log and res.stderr.strip():
        for line in res.stderr.strip().splitlines():
            log(line)
    lines = res.stdout.strip().splitlines()
    if len(lines) < 3:
        return None, None
    try:
        duration = float(lines[0])
    except ValueError:
        duration = None
    try:
        headers = json.loads(lines[1])
    except ValueError:
        headers = {}
    # the media URL works only with the HTTP headers yt-dlp used
    return Media(lines[2], headers), duration


def youtube_search(query, count=9):
    """[(title, duration in seconds or None, URL)] of YouTube search results."""
    res = subprocess.run(
        ["yt-dlp", "-q", "--no-warnings", "--flat-playlist"] + _js_runtime_args() +
        ["--print", "%(id)s\t%(duration)s\t%(title)s", f"ytsearch{count}:{query}"],
        capture_output=True, text=True, timeout=60)
    out = []
    for line in res.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        vid, dur, title = parts
        try:
            duration = float(dur)
        except ValueError:
            duration = None
        out.append((title, duration, f"https://www.youtube.com/watch?v={vid}"))
    return out


def gray_frames(source, fps, width, height, stretch=False, max_frames=None,
                realtime=False, loop=False, log=None, start=0.0, aspect=4 / 3,
                zoom="fit"):
    """Yield (height, width) uint8 arrays from a file or a media URL.
    realtime: deliver frames at playback speed. loop: repeat forever.
    start: position in seconds to start from.
    log: function that gets the error lines of ffmpeg."""
    err = subprocess.PIPE if log else None
    headers = getattr(source, "headers", None)
    hdr = (["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
           if headers else [])
    ff_in = (hdr + (["-ss", f"{start:.2f}"] if start > 0 else []) +
             (["-re"] if realtime else []) +
             (["-stream_loop", "-1"] if loop else []) + ["-i", source])
    stdin = None
    cmd = ["ffmpeg", "-v", "error", "-nostdin"] + ff_in + [
        "-an", "-vf", ffmpeg_filter(width, height, stretch, aspect, zoom), "-r", str(fps),
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    ff = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=err)
    if log:
        _pipe_lines(ff.stderr, log)
    n = 0
    size = width * height
    try:
        while max_frames is None or n < max_frames:
            buf = ff.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width)
            n += 1
    finally:
        ff.kill()
        ff.wait()


# ---- audio for the PET: 1-bit sound through the VIA shift register

AUDIO_FILTER = "highpass=f=150,lowpass=f=4500,dynaudnorm=f=150:g=15"
PDM_GAIN = 0.6           # a second-order modulator is stable below about 0.7


class PDM:
    """Second-order delta-sigma modulator: float samples -1..1 in, one bit
    per sample out, packed 8 to a byte with the first bit in bit 7 (the
    VIA shifts bit 7 out first). The state goes on from call to call."""

    def __init__(self):
        self.i1 = self.i2 = 0.0
        self.y = 1.0

    def bytes(self, samples):
        i1, i2, y = self.i1, self.i2, self.y
        bits = np.empty(len(samples), dtype=np.uint8)
        for n, x in enumerate((samples * PDM_GAIN).tolist()):
            i1 += x - y
            i2 += i1 - y
            y = 1.0 if i2 >= 0.0 else -1.0
            bits[n] = y > 0
        self.i1, self.i2, self.y = i1, i2, y
        return np.packbits(bits).tobytes()


class Clip:
    """Clipped 1-bit sound: the bit is the sign of the sound, so a tone
    becomes a square wave of the same pitch. Unlike PDM, which needs a
    speaker that plays low frequencies, the harmonics of the square wave
    make the pitch audible on a small beeper too. A Schmitt trigger
    (GATE) keeps quiet parts silent instead of turning noise into hiss.
    The state goes on from call to call."""

    GATE = 0.03

    def __init__(self):
        self.y = 0

    def bytes(self, samples):
        state = np.full(len(samples), -1, dtype=np.int8)
        state[samples > self.GATE] = 1
        state[samples < -self.GATE] = 0
        # inside the gate the bit stays as it was: carry the last decision on
        known = np.where(state >= 0, np.arange(len(samples)), -1)
        last = np.maximum.accumulate(known)
        bits = np.where(last >= 0, state[np.maximum(last, 0)], self.y).astype(np.uint8)
        if len(bits):
            self.y = int(bits[-1])
        return np.packbits(bits).tobytes()


AUDIO_METHODS = {"clip": Clip, "pdm": PDM}


class AudioStream:
    """The sound of a file or a media URL as 1-bit audio bytes for the PET,
    from position `start`: ffmpeg decodes it to `rate` samples per second,
    one sample for each bit, and `method` (clip or pdm) makes the bits. At
    the end of the media it starts again at `loop_start`. samples(n) returns
    the sound for n bytes, encode() makes the bytes of it, read(n) does
    both. pos is the position in seconds of the next sample."""

    def __init__(self, media, start, rate, log=None, loop_start=0.0, method="clip"):
        self.media, self.rate, self.log, self.loop_start = media, rate, log, loop_start
        self.pdm = AUDIO_METHODS[method]()
        self.ff = None
        self.open(start)

    def open(self, start):
        self.close()
        self.pos = start
        headers = getattr(self.media, "headers", None)
        hdr = (["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
               if headers else [])
        cmd = (["ffmpeg", "-v", "error", "-nostdin"] + hdr +
               (["-ss", f"{start:.2f}"] if start > 0 else []) +
               ["-i", self.media, "-vn", "-ac", "1", "-ar", str(self.rate),
                "-af", AUDIO_FILTER, "-f", "s16le", "pipe:1"])
        err = subprocess.PIPE if self.log else subprocess.DEVNULL
        self.ff = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        if self.log:
            _pipe_lines(self.ff.stderr, self.log)

    def samples(self, n):
        """Float samples -1..1 for n bytes: 8 a byte."""
        want = n * 8 * 2                        # 2 bytes a sample
        pcm = b""
        restarts = 0
        while len(pcm) < want:
            chunk = self.ff.stdout.read(want - len(pcm))
            if chunk:
                pcm += chunk
                self.pos += len(chunk) / 2 / self.rate
                continue
            restarts += 1                       # the end: loop, or silence if nothing plays
            if restarts > 2:
                pcm += b"\0" * (want - len(pcm))
                break
            self.open(self.loop_start)
        return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

    def encode(self, samples):
        return self.pdm.bytes(samples)

    def read(self, n):
        return self.encode(self.samples(n))

    def close(self):
        if self.ff is not None:
            self.ff.kill()
            self.ff.wait()
            self.ff = None


class Silence:
    """Audio bytes with no sound: all bits 0, so CB2 does not move."""

    def read(self, n):
        return bytes(n)

    def close(self):
        pass
