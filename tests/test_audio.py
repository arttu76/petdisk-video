"""Audio mode (A key): the 1-bit sound for the PET, the audio session in
the video stream, and the switch between video and audio mode."""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from pdvtest import (FakeSource, HAVE_FFMPEG, changed, convert, decode, live_source, noise,
                     runs, seeded, server)

BLOCK = 512


def unpack(data):
    """Audio bytes -> +1/-1 per bit, bit 7 first, as the PET plays them."""
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8)).astype(np.float32) * 2 - 1


def lowpass(x, n=24):
    """What the speaker makes of the bits, roughly: a moving average."""
    return np.convolve(x, np.ones(n) / n, mode="same")


class PdmTest(unittest.TestCase):

    def test_silence_is_half_ones(self):
        bits = unpack(convert.PDM().bytes(np.zeros(8000, np.float32)))
        self.assertAlmostEqual(bits.mean(), 0.0, delta=0.01)

    def test_a_level_becomes_the_density_of_ones(self):
        for level in (-0.8, -0.3, 0.5, 0.9):
            bits = unpack(convert.PDM().bytes(np.full(16000, level, np.float32)))
            self.assertAlmostEqual(bits[800:].mean(), level * convert.PDM_GAIN, delta=0.01)

    def test_state_goes_on_between_calls(self):
        x = np.sin(np.arange(8000) / 9.0).astype(np.float32)
        whole = convert.PDM().bytes(x)
        pdm = convert.PDM()
        self.assertEqual(pdm.bytes(x[:3000]) + pdm.bytes(x[3000:]), whole)

    def test_a_tone_comes_back_out(self):
        rate = server.AUDIO_RATE
        t = np.arange(rate) / rate
        tone = 0.8 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
        heard = lowpass(unpack(convert.PDM().bytes(tone)))
        want = tone * convert.PDM_GAIN
        self.assertGreater(np.corrcoef(heard[200:-200], want[200:-200])[0, 1], 0.95)

    def test_bytes(self):
        self.assertEqual(convert.Silence().read(4), bytes(4))
        self.assertEqual(len(convert.PDM().bytes(np.zeros(800, np.float32))), 100)


class ClipTest(unittest.TestCase):

    def test_a_tone_becomes_a_square_wave_of_the_same_pitch(self):
        rate = server.AUDIO_RATE
        t = np.arange(rate) / rate
        bits = unpack(convert.Clip().bytes((0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)))
        edges = np.flatnonzero(np.diff(bits) > 0)              # rising edges
        self.assertAlmostEqual(len(edges), 440, delta=1)
        self.assertAlmostEqual(np.diff(edges).mean(), rate / 440, delta=0.5)
        self.assertAlmostEqual(bits.mean(), 0.0, delta=0.01)   # half high, half low

    def test_quiet_stays_silent(self):
        rnd = np.random.default_rng(1)
        quiet = (rnd.standard_normal(20000) * convert.Clip.GATE / 4).astype(np.float32)
        for x in (np.zeros(20000, np.float32), quiet):
            data = convert.Clip().bytes(x)
            self.assertEqual(set(data), {0}, "a constant level, no sound")

    def test_state_goes_on_between_calls(self):
        x = (0.3 * np.sin(np.arange(8000) / 7.0)).astype(np.float32)
        x[3000:3100] = 0                                         # inside the gate at the cut
        whole = convert.Clip().bytes(x)
        clip = convert.Clip()
        self.assertEqual(clip.bytes(x[:3048]) + clip.bytes(x[3048:]), whole)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are not installed")
class AudioStreamTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clip = os.path.join(cls.tmp.name, "tone.mp4")
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-shortest", "-pix_fmt", "yuv420p", cls.clip], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_tone_is_in_the_bits(self):
        a = convert.AudioStream(self.clip, 0.5, server.AUDIO_RATE)
        try:
            data = a.read(server.AUDIO_RATE // 8)              # one second
        finally:
            a.close()
        self.assertEqual(len(data), server.AUDIO_RATE // 8)
        heard = lowpass(unpack(data))
        spectrum = np.abs(np.fft.rfft(heard - heard.mean()))
        peak = np.argmax(spectrum[20:]) + 20                    # 1 Hz per bin
        self.assertAlmostEqual(peak, 440, delta=3)

    def test_the_end_loops(self):
        a = convert.AudioStream(self.clip, 2.5, server.AUDIO_RATE)
        try:
            data = a.read(server.AUDIO_RATE // 8 * 2)          # past the end of 3 s
        finally:
            a.close()
        self.assertEqual(len(data), server.AUDIO_RATE // 8 * 2)
        bits = unpack(data)
        self.assertGreater(np.abs(lowpass(bits[-4000:])).max(), 0.2)   # sound again

    def test_missing_media_gives_silence(self):
        a = convert.AudioStream(os.path.join(self.tmp.name, "nope.mp4"), 0, server.AUDIO_RATE)
        try:
            data = a.read(1000)
        finally:
            a.close()
        self.assertEqual(data, bytes(1000))

    def test_pdm_method(self):
        a = convert.AudioStream(self.clip, 0.5, server.AUDIO_RATE, method="pdm")
        try:
            heard = lowpass(unpack(a.read(server.AUDIO_RATE // 8)))
        finally:
            a.close()
        spectrum = np.abs(np.fft.rfft(heard - heard.mean()))
        self.assertAlmostEqual(np.argmax(spectrum[20:]) + 20, 440, delta=3)


class FakeAudio:
    """An AudioStream without ffmpeg: a rising level, so that the order of
    the sound shows; counts the samples it hands out."""

    def __init__(self, media, start, rate, log=None, loop_start=0.0, method="clip"):
        self.start, self.pos, self.closed, self.method = start, start, False, method
        self.n = 0

    def samples(self, n):
        out = np.full(n * 8, (self.n % 100) / 100, np.float32)
        self.n += 1
        self.pos += n * 8 / server.AUDIO_RATE
        return out

    def encode(self, samples):
        return bytes([int(samples[0] * 100)]) * (len(samples) // 8)

    def read(self, n):
        return self.encode(self.samples(n))

    def close(self):
        self.closed = True


class FakeFeed:
    """A synchronous AudioFeed: packet k has the sound byte k."""

    made = []

    def __init__(self, source, start, rate, method, cols=80, screen=None, keep=range(0),
                 clear=False):
        self.start, self.method, self.k, self.closed = start, method, 0, False
        self.url, self.screen, self.keep, self.clear = source.url, screen, keep, clear
        FakeFeed.made.append(self)

    def packet(self):
        self.k += 1
        return server.audio_packet("(0:00)", b"", bytes([self.k]) * server.AUDIO_SOUND)

    def close(self):
        self.closed = True


T, P = server.AUDIO_TIME, server.AUDIO_PIC


def packets(data):
    """The packets after the AUDIO marker, until a LEAVE: [(text, picture,
    sound)] and the rest after the LEAVE byte, or None."""
    out, i = [], 0
    while i < len(data):
        if data[i] == server.LEAVE:
            return out, data[i + 1:]
        p = data[i:i + server.AUDIO_PACKET]
        if len(p) < server.AUDIO_PACKET:
            break
        out.append((p[:T], p[T:T + P], p[T + P:]))
        i += server.AUDIO_PACKET
    return out, None


def after_marker(data):
    """The bytes after the AUDIO marker: the frames before it are decoded,
    so that a $FA inside a frame does not count."""
    frames = decode(data, 2000)
    assert frames and frames[-1][0] == "audio", frames[-1:]
    for i in range(len(data)):
        if data[i] == server.AUDIO and decode(data[:i + 1], 2000)[-1:] == frames[-1:]:
            return data[i + 1:]
    raise AssertionError("no marker")


def codes(text):
    return bytes(server.screen_code(c) for c in text)


class SessionTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(server, "AudioFeed", FakeFeed)
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeFeed.made = []
        self.src = FakeSource(bytes([160]) * 2000)
        self.src.audio, self.src.media, self.src.position = True, "clip.mp4", 75.0
        self.stream = server.Stream(self.src)
        self.offset = 0

    def blocks(self, n=3):
        out = b""
        for _ in range(n):
            out += self.stream.read(self.offset, self.offset + BLOCK)
            self.offset += BLOCK
        return out

    def test_the_picture_with_the_title_then_marker_then_packets(self):
        data = self.blocks(4)
        screen = decode(data, 2000)[-1][1]
        title = "AUDIO MODE (1:15)    - PRESS A FOR VIDEO".center(80)
        self.assertEqual(screen[:80], codes(title.rstrip().ljust(80)))
        self.assertEqual(screen[80:], bytes([160]) * 1920)      # the picture stays
        pk, rest = packets(after_marker(data))
        self.assertIsNone(rest)
        self.assertEqual([p[2][0] for p in pk], list(range(1, len(pk) + 1)))
        self.assertEqual(FakeFeed.made[0].start, 75.0)
        self.assertEqual(FakeFeed.made[0].method, "clip")
        self.assertEqual(FakeFeed.made[0].keep, range(0, 80))   # the slide show leaves row 0
        self.assertEqual(FakeFeed.made[0].screen, screen)

    def test_forty_columns(self):
        self.src = FakeSource(bytes([160]) * 1000, cols=40)
        self.src.audio, self.src.position = True, 5.0
        self.stream = server.Stream(self.src)
        data = self.stream.read(0, BLOCK, 40) + self.stream.read(BLOCK, 2 * BLOCK, 40)
        screen = decode(data, 1000)[-1][1]
        self.assertEqual(screen[:40], codes("       AUDIO (0:05)    - A: VIDEO".ljust(40)))
        self.assertEqual(self.stream.time_cells(), range(13, 22))

    def test_the_model_follows_the_packets(self):
        self.blocks(4)
        self.assertEqual(self.stream.screen[31:40], codes("(0:00)".ljust(9)))
        self.assertEqual(self.stream.last_codes, bytes(self.stream.screen))

    def test_terminal_shows_what_the_pet_plays(self):
        with mock.patch.object(server.time, "monotonic", return_value=1000.0):
            self.blocks(1)
            self.assertEqual(self.stream.terminal_codes(), self.stream.snapshots[0])   # still buffering
            self.blocks(12)                                       # the PET buffer fills up
        sent = len(self.stream.snapshots)
        per_second = server.AUDIO_RATE / 8 / server.AUDIO_SOUND
        with mock.patch.object(server.time, "monotonic", return_value=1000.0 + 3.5 / per_second):
            self.assertEqual(self.stream.playing_packet(), 4)
            self.assertIs(self.stream.terminal_codes(), self.stream.snapshots[4])
        self.assertGreater(sent, 8)                               # the server is ahead

    def test_terminal_shows_the_search_screen(self):
        self.blocks(2)
        self.src.menu = True
        self.src.results_lines = lambda cols: ["YOUTUBE: PET"]
        self.assertTrue(self.stream.terminal_codes().startswith(codes("YOUTUBE: PET")))

    def test_retry_and_new_session(self):
        first = self.blocks()
        self.assertEqual(self.stream.read(BLOCK, 2 * BLOCK), first[BLOCK:2 * BLOCK])
        self.src.seek_to = 30.0
        self.offset = 0
        self.blocks(1)                                           # a key: the PET opens again
        self.assertTrue(FakeFeed.made[0].closed)
        self.assertEqual(FakeFeed.made[-1].start, 30.0)
        self.assertIsNone(self.src.seek_to)

    def test_paused_is_silence(self):
        self.src.paused = True
        pk, _ = packets(after_marker(self.blocks(4)))
        for text, picture, sound in pk:
            self.assertEqual(text, codes("PAUSED".ljust(9)))
            self.assertEqual(picture, bytes([server.PIC_END]) * P)
            self.assertEqual(sound, bytes(server.AUDIO_SOUND))

    def test_video_mode_has_no_marker(self):
        self.src.audio = False
        self.assertNotIn("audio", [m for m, _ in decode(self.blocks(), 2000)])

    def test_the_server_switches_to_audio_within_the_stream(self):
        self.src.audio = False
        video = self.blocks(5)
        self.assertNotIn("audio", [m for m, _ in decode(video, 2000)])
        self.src.audio = True                                     # A in the server terminal
        self.src.show(bytes([102]) * 2000)
        data = video + self.blocks(5)
        self.assertEqual(decode(data, 2000)[-1][0], "audio")
        self.assertGreater(len(packets(after_marker(data))[0]), 3)

    def test_the_server_switches_back_to_video_within_the_stream(self):
        data = self.blocks(4)
        self.src.audio = False
        self.src.show(bytes([81]) * 2000)
        data += self.blocks(8)
        pk, rest = packets(after_marker(data))
        self.assertIsNotNone(rest, "no LEAVE byte at a packet start")
        frames = decode(rest, 2000)
        self.assertIn(frames[0][0], (server.KEY, server.RLE))    # a key frame first
        self.assertEqual(frames[-1][1], bytes([81]) * 2000)
        self.assertTrue(FakeFeed.made[0].closed)

    def test_another_video_while_audio_plays(self):
        self.blocks(4)
        self.src.url, self.src.position = "https://example.com/other", 0.0
        self.blocks(4)
        self.assertEqual(len(FakeFeed.made), 2)
        self.assertTrue(FakeFeed.made[0].closed)
        self.assertEqual((FakeFeed.made[1].url, FakeFeed.made[1].start), ("https://example.com/other", 0.0))
        expected = bytearray(FakeFeed.made[0].screen)            # the screen at the switch
        expected[31:40] = codes("(0:00)".ljust(9))
        self.assertEqual(FakeFeed.made[1].screen, bytes(expected))

    def test_another_video_in_the_stream_clears_the_picture(self):
        self.blocks(4)
        self.assertFalse(FakeFeed.made[0].clear)
        self.src.url, self.src.position = "https://example.com/other", 0.0
        self.blocks(2)
        self.assertTrue(FakeFeed.made[1].clear)
        self.src.seek_to = 30.0                                    # the same video: keep it
        self.blocks(2)
        self.assertFalse(FakeFeed.made[2].clear)

    def test_a_pick_on_the_pet_starts_with_an_empty_screen(self):
        # the last picture is of the old video: the PET picked another one
        self.src.latest_url = "https://example.com/old"
        data = self.blocks(4)
        screen = decode(data, 2000)[-1][1]
        self.assertTrue(screen[:80].strip(b" ").startswith(codes("AUDIO MODE")))
        self.assertEqual(screen[80:], bytes([32]) * 1920)
        self.src.latest_url = self.src.url                         # the same video: keep it
        self.offset = 0
        screen = decode(self.blocks(4), 2000)[-1][1]
        self.assertEqual(screen[80:], bytes([160]) * 1920)

    def test_pause_and_seek_while_audio_plays(self):
        self.blocks(4)
        self.src.paused = True
        self.blocks(2)
        self.src.paused, self.src.seek_to = False, 40.0
        self.blocks(2)
        self.assertEqual(len(FakeFeed.made), 2)
        self.assertEqual(FakeFeed.made[-1].start, 40.0)

    def test_no_frame_yet_is_padding(self):
        self.src.audio = False
        self.src.ready.clear()                                    # a new video starts
        self.assertEqual(self.blocks(1), bytes([server.PAD]) * BLOCK)

    def test_position_follows_the_sound(self):
        with mock.patch.object(server.time, "monotonic", return_value=1000.0):
            self.blocks(1)
        later = 1000.0 + server.AUDIO_PREFILL + 10.0
        with mock.patch.object(server.time, "monotonic", return_value=later):
            self.blocks(1)
        self.assertAlmostEqual(self.src.position, 85.0)


class AudioFeedTest(unittest.TestCase):
    """The background feed: the PETdisk must never wait for yt-dlp or
    ffmpeg, so a request gets a BUFFERING packet until the sound is ready.
    The picture blocks make a slide show."""

    BUFFERING = codes("BUFFERING")

    def setUp(self):
        patcher = mock.patch.object(convert, "AudioStream", FakeAudio)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.src = FakeSource()
        self.src.media = "clip.mp4"

    def feed(self, screen=None):
        f = server.AudioFeed(self.src, 12.0, server.AUDIO_RATE, "clip", 80,
                             screen or bytes([32]) * 2000, range(0, 80))
        self.addCleanup(f.close)
        return f

    def wait(self, cond, timeout=5):
        deadline = time.monotonic() + timeout
        while not cond():
            self.assertLess(time.monotonic(), deadline, "the feed did not get there")
            time.sleep(0.01)

    def test_a_slow_start_does_not_block(self):
        slow = threading.Event()
        self.src.get_media = lambda audio=False: (slow.wait(5), "clip.mp4")[1]
        f = self.feed()
        t = time.monotonic()
        p = f.packet()
        self.assertLess(time.monotonic() - t, 0.1)
        self.assertEqual(p[:T], self.BUFFERING)
        self.assertEqual(p[T + P:], bytes(server.AUDIO_SOUND))
        slow.set()
        self.wait(lambda: f.ready() >= 2)
        p = f.packet()
        self.assertEqual(p[:T], codes("(0:12)".ljust(9)))
        self.assertEqual(p[T + P], 0)                             # the first sound

    def test_the_sound_goes_on_in_order(self):
        f = self.feed()
        self.wait(lambda: f.ready() >= 3)
        self.assertEqual([f.packet()[T + P] for _ in range(3)], [0, 1, 2])

    def test_packets_are_whole(self):
        f = self.feed()
        self.wait(lambda: f.ready() >= 1)
        self.assertEqual(len(f.packet()), server.AUDIO_PACKET)

    def test_it_keeps_only_some_seconds_ahead(self):
        f = self.feed()
        self.wait(lambda: f.ready() >= f.limit)
        time.sleep(0.2)
        self.assertLessEqual(f.ready(), f.limit + 1)

    def test_no_media_is_buffering(self):
        self.src.media = None
        f = self.feed()
        time.sleep(0.1)
        self.assertEqual(f.packet()[:T], self.BUFFERING)

    def test_the_slide_show(self):
        rnd = seeded(5)
        pictures = [bytes(rnd.randrange(255) for _ in range(2000)) for _ in range(3)]
        self.src.still_picture = pictures[0]
        start = bytearray(b"T" * 80 + bytes([32]) * 1920)         # the title on row 0
        f = self.feed(bytes(start))
        pet = bytearray(start)
        shown = []
        for n in range(400):
            self.wait(lambda: f.ready() >= 1)
            p = f.packet()
            server.apply_block(pet, p[T:T + P])
            if pet[80:] == pictures[len(shown)][80:]:
                shown.append(n)
                if len(shown) == 2:
                    break
                self.src.still_picture = pictures[len(shown)]
        self.assertEqual(pet[:80], bytes(start[:80]))             # row 0 untouched
        self.assertEqual(len(shown), 2, "the second slide did not come")
        # each slide is the picture at the time of the packet where the
        # previous one is complete, but at least SLIDE seconds later
        calls = self.src.still_calls
        self.assertAlmostEqual(calls[0], 12.0)
        per_packet = server.AUDIO_SOUND * 8 / server.AUDIO_RATE
        done = 12.0 + shown[0] * per_packet
        self.assertGreaterEqual(calls[1], 12.0 + server.AudioFeed.SLIDE - per_packet)
        self.assertLessEqual(calls[1], max(done, 12.0 + server.AudioFeed.SLIDE) + 2 * per_packet)

    def send(self, f, n):
        """Send n packets, as the stream does, and return them."""
        out = []
        for _ in range(n):
            self.wait(lambda: f.ready() >= 1)
            out.append(f.packet())
            time.sleep(0.002)                                     # the grab thread runs
        return out

    PER_PACKET = server.AUDIO_SOUND * 8 / server.AUDIO_RATE

    def test_the_picture_is_not_late(self):
        # the sound is made far ahead; the picture must still come at once,
        # for example after a pick in audio mode
        self.src.still_picture = bytes([160]) * 2000
        f = self.feed()
        self.wait(lambda: f.ready() >= f.limit)
        sent = self.send(f, 5)
        self.assertTrue(any(p[T] != server.PIC_END for p in sent), "no picture in the first packets")

    def test_clear_first_then_the_slides(self):
        self.src.still_picture = bytes([102]) * 2000
        old = bytes(codes("T" * 80)) + bytes([160]) * 1920
        f = server.AudioFeed(self.src, 12.0, server.AUDIO_RATE, "clip", 80, old, range(0, 80),
                             clear=True)
        self.addCleanup(f.close)
        pet = bytearray(old)
        sent = self.send(f, 3)
        for p in sent[:2]:
            server.apply_block(pet, p[T:T + P])
        self.assertEqual(pet[80:], bytes([32]) * 1920)             # cleared in 2 packets
        self.assertEqual(pet[:80], old[:80])                      # the title stays
        for p in sent[2:] + self.send(f, 200):
            server.apply_block(pet, p[T:T + P])
        self.assertEqual(pet[80:], bytes([102]) * 1920)            # then the slide

    def test_a_still_picture_is_grabbed_seldom(self):
        # a still video: every slide is complete at once
        self.src.still_picture = bytes([32]) * 2000
        f = self.feed()
        self.send(f, 100)
        sound = 100 * self.PER_PACKET
        self.assertLessEqual(len(self.src.still_calls), sound / server.AudioFeed.SLIDE + 2)

    def test_a_slow_grab_does_not_hold_the_sound(self):
        def slow(at, cols):
            time.sleep(1.0)                                       # ffmpeg on a YouTube video
            return bytes([160]) * 2000
        self.src.still = slow
        f = self.feed()
        t = time.monotonic()
        self.send(f, 30)                                          # 5 s of sound
        self.assertLess(time.monotonic() - t, 0.9)

    def test_a_failed_grab_tries_again_later(self):
        calls = []

        def broken(at, cols):
            calls.append(at)
            raise OSError("ffmpeg failed")
        self.src.still = broken
        with mock.patch.object(server, "say"):
            f = self.feed()
            self.send(f, 150)
        self.assertGreaterEqual(len(calls), 1)
        self.assertLessEqual(len(calls), 150 * self.PER_PACKET / server.AudioFeed.RETRY + 2)

    def test_no_picture_tries_again_later(self):
        self.src.still_picture = None
        f = self.feed()
        self.send(f, 150)
        self.assertLessEqual(len(self.src.still_calls), 150 * self.PER_PACKET / server.AudioFeed.RETRY + 2)

    def test_close_stops_the_sound(self):
        made = []

        class Recorder(FakeAudio):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                made.append(self)

        with mock.patch.object(convert, "AudioStream", Recorder):
            f = self.feed()
            self.wait(lambda: made and f.ready() > 0)
            f.close()
            self.wait(lambda: made[0].closed)


class PictureTest(unittest.TestCase):

    def test_packet_layout(self):
        p = server.audio_packet("(1:23)", bytes([1, 2]), bytes([7]) * server.AUDIO_SOUND)
        self.assertEqual(len(p), 512)
        self.assertEqual(p[:T], codes("(1:23)   "))
        self.assertEqual(p[T:T + P], bytes([1, 2]) + bytes([server.PIC_END]) * 14)
        self.assertNotEqual(p[0], server.LEAVE)

    def test_titles(self):
        self.assertEqual(server.audio_title(80, "(1:23)"),
                         (" " * 20 + "AUDIO MODE (1:23)    - PRESS A FOR VIDEO", 31))
        self.assertEqual(server.audio_title(40, "(1:23)"), (" " * 7 + "AUDIO (1:23)    - A: VIDEO", 13))
        self.assertEqual(server.time_field(5025), "(1:23:45)")

    def roundtrip(self, start, target, keep):
        model, pet = bytearray(start), bytearray(start)
        blocks = 0
        while True:
            b = server.picture_block(model, target, keep)
            if b is None:
                break
            self.assertLessEqual(len(b), P)
            server.apply_block(pet, b.ljust(P, bytes([server.PIC_END])))
            self.assertEqual(pet, model)
            blocks += 1
        for i in range(len(target)):
            self.assertEqual(pet[i], start[i] if i in keep else target[i], i)
        return blocks

    def test_blocks_build_the_picture(self):
        rnd = seeded(6)
        for cells in (2000, 1000):
            keep = range(0, cells // 25)
            for _ in range(10):
                start, target = noise(rnd, cells), noise(rnd, cells)
                self.roundtrip(start, target, keep)
                self.roundtrip(runs(rnd, cells), runs(rnd, cells), keep)
                self.roundtrip(start, changed(rnd, start, 30), keep)

    def test_even_areas_are_cheap(self):
        blank = bytes([32]) * 2000
        self.assertLessEqual(self.roundtrip(blank, bytes([160]) * 2000, range(0)), 2)

    def test_headers_are_never_the_end_byte(self):
        rnd = seeded(7)
        start, target = runs(rnd, 2000), noise(rnd, 2000)
        model = bytearray(start)
        while True:
            b = server.picture_block(model, target)
            if b is None:
                break
            i = 0
            while i < len(b):
                self.assertNotEqual(b[i], server.PIC_END)
                i += 4 if b[i] & 0x80 else 2 + (b[i] >> 3 & 15) + 1


class MediaTest(unittest.TestCase):
    """The media cache: the sound stream is looked up in the background
    when a video starts, and a lookup runs only once."""

    def test_callers_wait_for_a_running_lookup(self):
        calls, gate = [], threading.Event()

        def resolve(url, log=None, audio=False):
            calls.append((url, audio))
            gate.wait(5)
            return url + "#sound", 50.0

        src = live_source()
        src.url = "https://example.com/v"
        results = []
        with mock.patch.object(convert, "resolve", resolve):
            threads = [threading.Thread(target=lambda: results.append(src.get_media(audio=True)))
                       for _ in range(3)]
            for t in threads:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in threads:
                t.join(5)
        self.assertEqual(calls, [("https://example.com/v", True)])
        self.assertEqual(results, ["https://example.com/v#sound"] * 3)

    def test_a_failed_lookup_is_tried_again(self):
        answers = [(None, None), ("https://media/ok", 9.0)]
        src = live_source()
        src.url = "https://example.com/v"
        with mock.patch.object(convert, "resolve", lambda url, log=None, audio=False: answers.pop(0)):
            self.assertIsNone(src.get_media(audio=True))
            self.assertEqual(src.get_media(audio=True), "https://media/ok")
            self.assertEqual(src.get_media(audio=True), "https://media/ok")
        self.assertEqual(answers, [])

    def test_the_sound_is_prefetched_when_a_video_starts(self):
        calls = []

        def resolve(url, log=None, audio=False):
            calls.append(audio)
            return url, 100.0

        def frames(*a, **kw):
            while True:
                time.sleep(0.01)
                yield np.zeros((200, 640), np.uint8)

        ms = {cols: convert.Matcher(cols) for cols in (80, 40)}
        with mock.patch.object(convert, "resolve", resolve), \
                mock.patch.object(convert, "gray_frames", frames):
            src = server.LiveSource("https://example.com/v", ms, 10.0)
            try:
                deadline = time.monotonic() + 5
                while sorted(calls) != [False, True] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(sorted(calls), [False, True])
                self.assertEqual(src.get_media(audio=True), "https://example.com/v")
                self.assertEqual(len(calls), 2)                   # no second lookup
            finally:
                src.url, src.switch = None, True


class KeyTest(unittest.TestCase):

    def test_a_switches_the_mode(self):
        src = live_source()
        src.position = 42.0
        self.assertEqual(src.command("a"), "status")
        self.assertTrue(src.audio)
        self.assertTrue(src.leaving())                          # the video decoder stops
        self.assertIn("A)UDIO:ON", "\n".join(src.overlay_lines(80)))
        src.position = 50.0
        src.command("a")
        self.assertFalse(src.audio)
        self.assertEqual(src.seek_to, 50.0)                     # the video goes on there
        self.assertIsNone(src.command("A"))                     # shift-A is no hotkey

    def test_media_is_resolved_once(self):
        calls = []

        def resolve(url, log=None, audio=False):
            calls.append((url, audio))
            return url + ("#sound" if audio else "#video"), 99.0

        src = live_source()
        src.url = "https://example.com/v"
        with mock.patch.object(convert, "resolve", resolve):
            self.assertEqual(src.get_media(), "https://example.com/v#video")
            self.assertEqual(src.get_media(), "https://example.com/v#video")
            self.assertEqual(src.get_media(audio=True), "https://example.com/v#sound")
            self.assertEqual(src.get_media(audio=True), "https://example.com/v#sound")
            src.url = "https://example.com/w"
            src.get_media()
        self.assertEqual(calls, [("https://example.com/v", False), ("https://example.com/v", True),
                                 ("https://example.com/w", False)])
        self.assertEqual(src.duration, 99.0)


if __name__ == "__main__":
    unittest.main()
