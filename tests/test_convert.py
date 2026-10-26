"""The PETSCII conversion, the ffmpeg decoding and the yt-dlp calls. The
ffmpeg tests need ffmpeg; yt-dlp is replaced by recorded output."""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np

from pdvtest import HAVE_FFMPEG, convert, make_video, seeded


def render(m, codes):
    """The pixels that the PET shows for these screen codes."""
    return np.asarray(m.render(bytes([convert.SYNC]) + bytes(codes), scale_to_4_3=False))


class MatcherTest(unittest.TestCase):

    def test_shapes(self):
        for cols in (80, 40):
            m = convert.Matcher(cols)
            codes = m.codes_for(np.zeros((200, cols * 8), np.uint8))
            self.assertEqual(codes.shape, (cols * 25,))
            self.assertEqual(codes.dtype, np.uint8)

    def test_black_and_white(self):
        for cols in (80, 40):
            m = convert.Matcher(cols)
            for level in (0, 255):
                img = np.full((200, cols * 8), level, np.uint8)
                self.assertTrue((render(m, m.codes_for(img)) == level).all())

    def test_glyph_pictures_convert_back(self):
        # a picture made of PET glyphs converts to glyphs with the same pixels
        rnd = seeded(1)
        for cols in (80, 40):
            m = convert.Matcher(cols)
            # 255 is the sync marker and never sent
            codes = bytes(rnd.choice([c for c in range(256) if c != convert.SYNC])
                          for _ in range(cols * 25))
            picture = render(m, codes)
            self.assertTrue((render(m, m.codes_for(picture)) == picture).all())

    def test_never_the_sync_code(self):
        rnd = np.random.default_rng(2)
        m = convert.Matcher(80)
        for _ in range(5):
            img = rnd.integers(0, 256, (200, 640), dtype=np.uint8)
            self.assertNotIn(convert.SYNC, m.codes_for(img))

    def test_stick_keeps_the_previous_glyphs(self):
        rnd = np.random.default_rng(3)
        img = rnd.integers(0, 256, (200, 640)).astype(np.int16)
        moved = np.clip(img + rnd.integers(-12, 13, img.shape), 0, 255).astype(np.uint8)
        img = img.astype(np.uint8)
        sticky, loose = convert.Matcher(80, stick=6.0), convert.Matcher(80, stick=0)
        prev = loose.codes_for(img)
        n_sticky = (sticky.codes_for(moved, prev) != prev).sum()
        n_loose = (loose.codes_for(moved, prev) != prev).sum()
        self.assertLess(n_sticky, n_loose)
        self.assertTrue((loose.codes_for(moved, prev) == loose.codes_for(moved)).all())

    def test_brightness_contrast_gamma(self):
        mid = np.full((200, 640), 128, np.uint8)
        self.assertTrue((convert.Matcher(80, brightness=1.0).adjust(mid) == 255).all())
        self.assertTrue((convert.Matcher(80, brightness=-1.0).adjust(mid) == 0).all())
        ramp = np.tile(np.arange(256, dtype=np.uint8), (200, 3))[:, :640]
        flat = convert.Matcher(80, contrast=0.5).adjust(ramp)
        self.assertLess(flat.max() - flat.min(), 130)
        bright = convert.Matcher(80, gamma=2.0).adjust(mid)
        self.assertGreater(bright.mean(), 128)

    def test_dithering_gives_black_and_white(self):
        ramp = np.tile(np.arange(256, dtype=np.uint8), (200, 3))[:, :640]
        g = convert.Matcher(80, dither=1).prepare(ramp)
        self.assertEqual(set(np.unique(g)), {0.0, 255.0})
        self.assertGreater(len(np.unique(convert.Matcher(80, dither=0).prepare(ramp))), 2)

    def test_character_rom_file(self):
        with tempfile.NamedTemporaryFile(suffix=".bin") as f:
            f.write(convert.PET_GLYPHS[:1024])
            f.flush()
            glyphs, codes = convert.load_glyphs(f.name)
            self.assertEqual(glyphs.shape, (256, 64))
            short = tempfile.NamedTemporaryFile(suffix=".bin")
            short.write(b"\0" * 100)
            short.flush()
            with self.assertRaises(SystemExit):
                convert.load_glyphs(short.name)
            short.close()


class FilterTest(unittest.TestCase):

    def test_filters(self):
        self.assertEqual(convert.ffmpeg_filter(640, 200, False, zoom="stretch"),
                         "scale=640:200:flags=area,format=gray")
        self.assertIn("pad=640:356", convert.ffmpeg_filter(640, 200, False, 1.8, "fit"))
        self.assertIn("crop=640:356", convert.ffmpeg_filter(640, 200, False, 1.8, "fill"))
        self.assertTrue(convert.is_url("https://www.youtube.com/watch?v=x"))
        self.assertFalse(convert.is_url("/videos/clip.mp4"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are not installed")
class FfmpegTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.moving = make_video(os.path.join(cls.tmp.name, "moving.mp4"), 4)
        # a white picture, much wider than the PET screen
        cls.wide = make_video(os.path.join(cls.tmp.name, "wide.mp4"), 2, "1280x360", "color=white")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def frames(self, source, **kw):
        return list(convert.gray_frames(source, 10, 640, 200, **kw))

    def test_frames(self):
        frames = self.frames(self.moving, max_frames=5)
        self.assertEqual(len(frames), 5)
        for f in frames:
            self.assertEqual((f.shape, f.dtype), ((200, 640), np.uint8))
        self.assertFalse((frames[0] == frames[4]).all())          # it moves

    def test_whole_video_and_start(self):
        self.assertAlmostEqual(len(self.frames(self.moving)), 40, delta=2)
        self.assertAlmostEqual(len(self.frames(self.moving, start=3.0)), 10, delta=2)

    def test_zoom_modes(self):
        # fit: black bars above and below the wide picture; fill and stretch: none
        fit = self.frames(self.wide, max_frames=1, aspect=1.8, zoom="fit")[0]
        self.assertLess(fit[:20].mean(), 10)
        self.assertGreater(fit[90:110].mean(), 245)
        for zoom in ("fill", "stretch"):
            f = self.frames(self.wide, max_frames=1, aspect=1.8, zoom=zoom)[0]
            self.assertGreater(f.mean(), 245, zoom)

    def test_duration_of_a_file(self):
        media, duration = convert.resolve(self.moving)
        self.assertEqual(media, self.moving)
        self.assertAlmostEqual(duration, 4.0, delta=0.1)

    def test_missing_file(self):
        self.assertEqual(convert.resolve(os.path.join(self.tmp.name, "nope.mp4")),
                         (os.path.join(self.tmp.name, "nope.mp4"), None))
        log = []
        self.assertEqual(self.frames(os.path.join(self.tmp.name, "nope.mp4"),
                                     log=log.append), [])


def completed(stdout, stderr=""):
    return subprocess.CompletedProcess([], 0, stdout, stderr)


class YtDlpTest(unittest.TestCase):

    def test_search_output(self):
        out = ("abc\t61.0\tFirst video\n"
               "def\tNA\tLive stream\twith a tab\n"
               "broken line\n"
               "ghi\t3600\tThird\n")
        with mock.patch.object(convert.subprocess, "run", return_value=completed(out)) as run:
            found = convert.youtube_search("pet demos", 3)
        self.assertEqual(found, [
            ("First video", 61.0, "https://www.youtube.com/watch?v=abc"),
            ("Live stream\twith a tab", None, "https://www.youtube.com/watch?v=def"),
            ("Third", 3600.0, "https://www.youtube.com/watch?v=ghi")])
        self.assertIn("ytsearch3:pet demos", run.call_args[0][0])

    def test_resolve_a_url(self):
        out = '212.5\n{"User-Agent": "test"}\nhttps://media.example/v.mp4\n'
        log = []
        with mock.patch.object(convert.shutil, "which", return_value="/usr/bin/yt-dlp"), \
                mock.patch.object(convert.subprocess, "run",
                                  return_value=completed(out, "WARNING: slow\n")):
            media, duration = convert.resolve("https://www.youtube.com/watch?v=x", log.append)
        self.assertEqual((media, duration), ("https://media.example/v.mp4", 212.5))
        self.assertEqual(media.headers, {"User-Agent": "test"})
        self.assertEqual(log, ["WARNING: slow"])

    def test_resolve_failure(self):
        with mock.patch.object(convert.shutil, "which", return_value="/usr/bin/yt-dlp"), \
                mock.patch.object(convert.subprocess, "run", return_value=completed("")):
            self.assertEqual(convert.resolve("https://www.youtube.com/watch?v=x"), (None, None))

    def test_no_yt_dlp(self):
        with mock.patch.object(convert.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit):
                convert.resolve("https://www.youtube.com/watch?v=x")

    def test_javascript_runtime(self):
        def which(found):
            return lambda name: f"/usr/bin/{name}" if name in found else None
        for found, want in (({"deno", "node"}, []),
                            ({"node"}, ["--js-runtimes", "node:/usr/bin/node"]),
                            ({"qjs"}, ["--js-runtimes", "quickjs:/usr/bin/qjs"]),
                            (set(), [])):
            with mock.patch.object(convert.shutil, "which", which(found)):
                self.assertEqual(convert._js_runtime_args(), want, found)


if __name__ == "__main__":
    unittest.main()
