"""The virtual VIDEO80.SEQ and VIDEO40.SEQ files: sessions, padding,
re-reads, the overlay and the state for a self-restart. The
stream is read in 512-byte blocks like the PETdisk MAX reads it."""

import pickle
import unittest
from unittest import mock

from pdvtest import FakeSource, changed, decode, noise, runs, seeded, server

BLOCK = 512


class Reader:
    """Reads a Stream block by block, like the PETdisk, and decodes it."""

    def __init__(self, stream, cols=80):
        self.stream, self.cols = stream, cols
        self.offset = 0
        self.data = bytearray()

    def block(self):
        out = self.stream.read(self.offset, self.offset + BLOCK, self.cols)
        assert len(out) == BLOCK, len(out)
        self.offset += BLOCK
        self.data += out
        return out

    def frames(self):
        return decode(self.data, self.cols * 25)

    def screen(self):
        """What the PET shows after the frames read so far."""
        frames = self.frames()
        return frames[-1][1] if frames else None

    def until_sent(self):
        """Read blocks until the PET has every frame that the server sent.
        The server encodes a new frame only when a block is read."""
        self.block()
        for _ in range(20):
            if len(self.frames()) == self.stream.frames_sent:
                return self.screen()
            self.block()
        raise AssertionError("the frames do not arrive")


def stream_for(codes=None, cols=80, **kw):
    src = FakeSource(codes, cols=cols, **kw)
    return server.Stream(src), src


class SessionTest(unittest.TestCase):

    def test_a_session_starts_with_a_key_frame(self):
        rnd = seeded(1)
        for codes in (noise(rnd, 2000), runs(rnd, 2000), bytes([32]) * 2000):
            s, _ = stream_for(codes)
            r = Reader(s)
            self.assertEqual(r.until_sent(), codes)
            self.assertIn(r.frames()[0][0], (server.KEY, server.RLE))

    def test_padding_when_no_new_frame_is_due(self):
        s, _ = stream_for(runs(seeded(2), 2000))
        r = Reader(s)
        r.until_sent()
        self.assertEqual(r.block(), bytes([server.PAD]) * BLOCK)
        self.assertEqual(s.frames_sent, 1)

    def test_new_frames_are_deltas(self):
        rnd = seeded(3)
        s, src = stream_for(runs(rnd, 2000))
        r = Reader(s)
        r.until_sent()
        for _ in range(20):
            src.show(changed(rnd, src.codes, 30))
            self.assertEqual(r.until_sent(), src.codes)
            self.assertEqual(r.frames()[-1][0], server.DELTA)

    def test_a_retry_gets_the_same_bytes(self):
        # the PETdisk asks for a block again after a WiFi error
        rnd = seeded(4)
        s, src = stream_for(noise(rnd, 2000))
        first = s.read(0, BLOCK)
        second = s.read(BLOCK, 2 * BLOCK)
        src.show(changed(rnd, src.codes, 100))
        self.assertEqual(s.read(BLOCK, 2 * BLOCK), second)
        self.assertEqual(s.read(0, BLOCK)[:1], first[:1])   # offset 0: a new session

    def test_offset_zero_starts_a_new_session(self):
        # the PET opens the video again after each key: a new key frame
        rnd = seeded(5)
        s, src = stream_for(runs(rnd, 2000))
        r = Reader(s)
        r.until_sent()
        src.show(changed(rnd, src.codes, 10))
        r = Reader(s)
        self.assertEqual(r.until_sent(), src.codes)
        self.assertIn(r.frames()[0][0], (server.KEY, server.RLE))

    def test_forty_columns(self):
        rnd = seeded(6)
        s, src = stream_for(runs(rnd, 1000), cols=40)
        r = Reader(s, cols=40)
        self.assertEqual(r.until_sent(), src.codes)
        self.assertEqual(src.cols, 40)
        src.show(changed(rnd, src.codes, 40))
        self.assertEqual(r.until_sent(), src.codes)

    def test_nothing_to_play_sends_the_pet_to_the_search(self):
        s, _ = stream_for(url=None)
        self.assertEqual(s.read(0, BLOCK), bytes([server.SEARCH]) + bytes([server.PAD]) * 511)
        self.assertEqual(s.read(BLOCK, 2 * BLOCK), bytes([server.PAD]) * BLOCK)

    def test_old_bytes_are_dropped(self):
        rnd = seeded(7)
        s, src = stream_for(noise(rnd, 2000))
        r = Reader(s)
        for _ in range(200):
            src.show(noise(rnd, 2000))
            r.block()
        self.assertGreater(s.base, 0)
        # a frame goes into the buffer whole, so at most one frame more
        self.assertLessEqual(len(s.buf), s.KEEP + 2 * BLOCK + 2001)

    def test_long_random_run(self):
        # whatever comes, the PET shows each whole frame, also after scene
        # cuts: there is no interlacing
        rnd = seeded(8)
        for cols in (80, 40):
            cells = cols * 25
            s, src = stream_for(runs(rnd, cells), cols=cols)
            r = Reader(s, cols)
            for _ in range(150):
                k = rnd.randrange(6)
                src.show(noise(rnd, cells) if k == 0 else runs(rnd, cells) if k == 1
                         else changed(rnd, src.codes, rnd.randrange(1, 400)))
                self.assertEqual(r.until_sent(), src.codes)
                self.assertEqual(s.prev, src.codes)


class OverlayTest(unittest.TestCase):

    def test_overlay_text_on_the_top_rows(self):
        rnd = seeded(10)
        s, src = stream_for(noise(rnd, 2000))
        s.show_status("toggle")
        r = Reader(s)
        screen = r.until_sent()
        want = bytes(server.screen_code(ch) for ch in "OVERLAY TEST".ljust(80))
        self.assertEqual(screen[:80], want)
        self.assertEqual(screen[80:], src.codes[80:])

    def test_a_new_overlay_text_is_a_new_frame(self):
        s, src = stream_for(runs(seeded(11), 2000))
        s.show_status("toggle")
        r = Reader(s)
        r.until_sent()
        src.overlay = ["B)RIGHTNESS:+0.05"]
        r.block()
        self.assertEqual(s.frames_sent, 2)
        self.assertEqual(r.until_sent()[:17], bytes(server.screen_code(c) for c in src.overlay[0]))

    def test_toggle_off(self):
        s, src = stream_for(runs(seeded(12), 2000))
        s.show_status("toggle")
        s.show_status("toggle")
        self.assertEqual(Reader(s).until_sent(), src.codes)
        s.show_status("status")                # a value changed: no toggle
        self.assertFalse(s.overlay_on)


class RestartStateTest(unittest.TestCase):
    """A self-restart saves the stream state, and the new process must go
    on with exactly the bytes the old one would have sent."""

    def run_both(self, state_of):
        rnd = seeded(13)
        a, src_a = stream_for(noise(rnd, 2000))
        ra = Reader(a)
        for _ in range(5):
            src_a.show(noise(rnd, 2000))
            ra.until_sent()
        b, src_b = stream_for(src_a.codes)
        b.load_state(state_of(a))
        src_b.count = src_a.count
        new = noise(rnd, 2000)
        src_a.show(new)
        src_b.show(new)
        self.assertEqual(b.read(ra.offset, ra.offset + BLOCK), a.read(ra.offset, ra.offset + BLOCK))
        # a retry of a block from before the restart
        self.assertEqual(b.read(ra.offset - BLOCK, ra.offset), a.read(ra.offset - BLOCK, ra.offset))

    def test_state_survives_pickle(self):
        self.run_both(lambda s: pickle.loads(pickle.dumps(s.save_state())))

    def test_state_from_a_server_with_interlacing(self):
        # an older server saved an interlace field and setting: ignored
        s, src = stream_for(runs(seeded(14), 2000))
        Reader(s).until_sent()
        st = s.save_state()
        st["field"] = 1
        st["settings"]["interlace"] = "on"
        b, src_b = stream_for(src.codes)
        b.load_state(st)
        self.assertEqual(b.prev, s.prev)
        self.assertEqual(src_b.settings_applied, st["settings"])


class StatsTest(unittest.TestCase):

    def test_last_frame_text(self):
        st = server.Stats()
        self.assertEqual(st.last_frame_text(), "")
        st.frame(300, 2001)
        self.assertEqual(st.last_frame_text(), "last frame 300 B of 2001 B (15%)")
        self.assertEqual(st.summary()["last"], "last frame 300 B of 2001 B (15%)")

    def test_fps_min_and_max(self):
        st = server.Stats()
        now = 1000.0
        # the last 5 whole seconds have 7, 3, 0, 8 and 5 frames
        for second, count in enumerate((5, 8, 0, 3, 7)):
            for i in range(count):
                st.frames.append((now - second - 1 + (i + 0.5) / count, 100))
        with mock.patch.object(server.time, "monotonic", return_value=now):
            summary = st.summary()
        self.assertEqual(summary["fps_range"], "0/8")
        self.assertAlmostEqual(summary["fps"], 23 / 5)

    def test_only_the_newest_frame_counts(self):
        st = server.Stats()
        st.frame(2001, 2001)
        st.frame(250, 1001)                    # 40 columns
        self.assertEqual(st.last_frame_text(), "last frame 250 B of 1001 B (25%)")

    def test_stream_reports_its_frames(self):
        rnd = seeded(15)
        s, src = stream_for(noise(rnd, 2000))
        r = Reader(s)
        r.until_sent()
        self.assertEqual(s.stats.last_frame_text(), "last frame 2001 B of 2001 B (100%)")
        old = src.codes
        src.show(changed(rnd, old, 1))
        r.until_sent()
        size = len(server.encode_frame(old, src.codes))     # a delta frame of a few bytes
        self.assertEqual(s.stats.last_frame_text(),
                         f"last frame {size} B of 2001 B ({100 * size / 2001:.0f}%)")


if __name__ == "__main__":
    unittest.main()
