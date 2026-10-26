"""Frame encoders: run-length key frames, delta frames and the choice
between them. Every frame must decode back exactly."""

import math
import unittest

from pdvtest import StreamError, changed, decode, noise, runs, seeded, server


def decode_one(frame, cells, prev=None):
    frames = decode(frame, cells, prev)
    if len(frames) != 1:
        raise StreamError(f"{len(frames)} frames in one encoded frame")
    return frames[0][1]


def controls(frame):
    """The (control, data length) pairs of a run-length frame."""
    out, i = [], 1
    while i < len(frame):
        c = frame[i]
        n = c + 1 if c < 128 else 1
        out.append((c, n))
        i += 1 + n
    return out


class RleTest(unittest.TestCase):

    def roundtrip(self, cur):
        frame = server.encode_rle(cur)
        self.assertEqual(frame[0], server.RLE)
        self.assertEqual(decode_one(frame, len(cur)), cur)
        return frame

    def test_run_lengths_at_the_limits(self):
        # a run of 129 is one repeat; 130 needs a second control byte
        for n in (1, 2, 3, 4, 127, 128, 129, 130, 131, 257, 258, 259, 1999):
            for cells in (2000, 1000):
                cur = bytes([1]) * min(n, cells) + bytes(range(2, 2 + 40)) * 50
                self.roundtrip(cur[:cells])

    def test_literal_blocks_at_the_limits(self):
        # no two equal neighbours: only literal blocks of at most 128 codes
        for cells in (2000, 1000):
            cur = bytes(i % 2 * 100 + i // 2 % 50 for i in range(cells))
            frame = self.roundtrip(cur)
            for c, n in controls(frame):
                self.assertLess(c, 128)
                self.assertLessEqual(n, 128)
            self.assertEqual(len(frame), 1 + cells + math.ceil(cells / 128))

    def test_blank_screen_is_small(self):
        for cells in (2000, 1000):
            frame = self.roundtrip(bytes([32]) * cells)
            self.assertEqual(len(frame), 1 + 2 * math.ceil(cells / 129))

    def test_control_bytes_are_valid(self):
        rnd = seeded(2)
        for _ in range(200):
            frame = self.roundtrip(runs(rnd, 2000))
            for c, n in controls(frame):
                if c >= 128:
                    # repeats of 3..129: shorter runs go in the literals
                    self.assertGreaterEqual(c - 126, 3)
                    self.assertLessEqual(c - 126, 129)

    def test_random_frames(self):
        rnd = seeded(3)
        for cells in (2000, 1000):
            for _ in range(300):
                kind = rnd.randrange(3)
                cur = (noise(rnd, cells) if kind == 0 else runs(rnd, cells) if kind == 1
                       else bytes(rnd.choice((32, 160)) for _ in range(cells)))
                self.roundtrip(cur)


class DeltaTest(unittest.TestCase):

    def roundtrip(self, prev, cur):
        frame = server.encode_delta(prev, cur)
        self.assertEqual(frame[0], server.DELTA)
        self.assertEqual(decode_one(frame, len(cur), prev), cur)
        return frame

    def test_no_change(self):
        prev = bytes(range(250)) * 8
        self.assertEqual(self.roundtrip(prev, prev), bytes([server.DELTA, 0, 0]))

    def test_first_and_last_cell(self):
        prev = bytes([32]) * 2000
        for cells in ([0], [1999], [0, 1999], [255], [256], [510], [511], [765]):
            cur = bytearray(prev)
            for c in cells:
                cur[c] = 1
            self.roundtrip(prev, bytes(cur))

    def test_long_skips_and_runs(self):
        # skips over 255 cells and runs over 255 cells take several entries
        prev = bytes([32]) * 2000
        for start, length in ((0, 2000), (300, 256), (1000, 600), (1744, 256), (255, 255)):
            cur = bytearray(prev)
            cur[start:start + length] = bytes([7]) * length
            self.roundtrip(prev, bytes(cur))

    def test_small_gaps_are_sent_as_data(self):
        # gaps of 1-2 unchanged cells cost less as data than as a new run
        prev = bytes([32]) * 2000
        cur = bytearray(prev)
        cur[10] = cur[12] = cur[15] = 1
        frame = self.roundtrip(prev, bytes(cur))
        self.assertEqual(frame, bytes([server.DELTA, 10, 6, 1, 32, 1, 32, 32, 1, 0, 0]))

    def test_random_changes(self):
        rnd = seeded(4)
        for cells in (2000, 1000):
            for _ in range(300):
                prev = runs(rnd, cells) if rnd.random() < 0.5 else noise(rnd, cells)
                self.roundtrip(prev, changed(rnd, prev, rnd.randrange(1, 800)))


class EncodeFrameTest(unittest.TestCase):

    def test_first_frame_is_a_key_frame(self):
        rnd = seeded(5)
        for cur in (bytes([32]) * 2000, noise(rnd, 2000), runs(rnd, 1000)):
            frame = server.encode_frame(None, cur)
            self.assertIn(frame[0], (server.KEY, server.RLE))
            self.assertEqual(len(frame), min(len(server.encode_rle(cur)), 1 + len(cur)))
            self.assertEqual(decode_one(frame, len(cur)), cur)

    def test_noise_is_a_plain_key_frame(self):
        # random codes do not compress: the plain key frame is shorter
        cur = noise(seeded(6), 2000)
        frame = server.encode_frame(None, cur)
        self.assertEqual(frame, bytes([server.KEY]) + cur)
        self.assertGreater(len(server.encode_rle(cur)), len(frame))

    def test_the_shortest_frame_wins(self):
        rnd = seeded(7)
        for _ in range(300):
            cells = rnd.choice((2000, 1000))
            prev = runs(rnd, cells) if rnd.random() < 0.5 else noise(rnd, cells)
            kind = rnd.randrange(3)
            cur = (changed(rnd, prev, rnd.randrange(1, 50)) if kind == 0
                   else runs(rnd, cells) if kind == 1 else noise(rnd, cells))
            frame = server.encode_frame(prev, cur)
            sizes = [len(server.encode_delta(prev, cur)), len(server.encode_rle(cur)),
                     1 + cells]
            self.assertEqual(len(frame), min(sizes))
            self.assertLessEqual(len(frame), 1 + cells)
            self.assertEqual(decode_one(frame, cells, prev), cur)


class NoInterlaceTest(unittest.TestCase):

    def test_interlacing_is_gone(self):
        self.assertFalse(hasattr(server, "interlaced"))
        self.assertFalse(hasattr(server, "INTERLACE_LIMITS"))


if __name__ == "__main__":
    unittest.main()
