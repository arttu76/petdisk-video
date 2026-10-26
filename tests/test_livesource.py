"""LiveSource without its decoder thread: hotkeys, settings, seeking, loop
marks, the overlay text on the PET and the YouTube search screen."""

import os
import re
import time
import unittest
from unittest import mock

import numpy as np

from pdvtest import convert, live_source, server


class PetKeyTest(unittest.TestCase):

    def test_letters_digits_and_signs(self):
        pk = server.LiveSource.pet_key
        self.assertEqual(pk(65), "a")
        self.assertEqual(pk(90), "z")
        self.assertEqual(pk(193), "A")
        self.assertEqual(pk(218), "Z")
        self.assertEqual([pk(c) for c in range(48, 58)], list("0123456789"))
        self.assertEqual(pk(32), " ")
        self.assertEqual(pk(33), "!")
        for code in (0, 13, 20, 64, 91, 160, 192, 219, 255):
            self.assertEqual(pk(code), "", code)

    def test_terminal_keys(self):
        split = server.TerminalKeys.split_keys
        self.assertEqual(split(b"ab\x1b[AB\x1bOD!"), ["a", "b", "\x1b[A", "B", "\x1bOD", "!"])
        self.assertEqual(split(b"\x1b"), ["\x1b"])            # a lone ESC
        self.assertEqual(split(b"\x1b["), ["\x1b", "["])


class KeyTest(unittest.TestCase):

    def setUp(self):
        self.src = live_source(fps=10.0)
        self.m = self.src.matchers[80]

    def test_brightness_contrast_gamma(self):
        s, m = self.src, self.m
        for key, attr, step in (("b", "brightness", 0.05), ("c", "contrast", 0.1),
                                ("g", "gamma", 0.1)):
            before = getattr(m, attr)
            self.assertEqual(s.command(key), "status")
            self.assertAlmostEqual(getattr(m, attr), before + step)
            s.command(key.upper())
            self.assertAlmostEqual(getattr(m, attr), before)

    def test_limits(self):
        s, m = self.src, self.m
        for _ in range(100):
            s.command("b")
            s.command("c")
            s.command("g")
        self.assertEqual((m.brightness, m.contrast, m.gamma), (1.0, 5.0, 5.0))
        for _ in range(200):
            s.command("B")
            s.command("C")
            s.command("G")
        self.assertEqual(m.brightness, -1.0)
        self.assertAlmostEqual(m.contrast, 0.1)
        self.assertAlmostEqual(m.gamma, 0.1)

    def test_both_widths_get_the_settings(self):
        self.src.command("b")
        self.src.command("d")
        m40 = self.src.matchers[40]
        self.assertEqual((m40.brightness, m40.dither), (self.m.brightness, self.m.dither))

    def test_dither(self):
        s = self.src
        self.assertEqual(int(self.m.dither), 0)
        s.command("d")
        self.assertEqual(int(self.m.dither), 1)
        s.command("d")
        self.assertEqual(int(self.m.dither), 0)

    def test_zoom_cycles_and_restarts_the_decoder(self):
        s = self.src
        s.position = 12.0
        modes = []
        for _ in range(3):
            self.assertEqual(s.command("z"), "status")
            modes.append(s.zoom)
        self.assertEqual(modes, ["fill", "stretch", "fit"])
        self.assertEqual(s.seek_to, 12.0)

    def test_reset_restores_the_start_values(self):
        s = live_source(zoom="fill")
        s.matchers[80].brightness = 0.2
        s.start_settings = s.settings()
        for key in "bbccgdz":
            s.command(key)
        s.position, s.seek_to = 30.0, None       # the decoder followed the Z key
        s.command("r")
        self.assertEqual(s.settings(), s.start_settings)
        self.assertEqual(s.zoom, "fill")
        self.assertEqual(s.seek_to, 30.0)        # the zoom changed back

    def test_reset_without_zoom_change_does_not_seek(self):
        s = self.src
        s.command("b")
        s.command("r")
        self.assertIsNone(s.seek_to)

    def test_seek_keys(self):
        s = self.src
        s.duration = 1000.0
        s.position = 300.0
        for key, want in (("4", 360.0), ("3", 370.0), ("2", 360.0), ("1", 300.0)):
            s.command(key)
            self.assertEqual(s.seek_to, want, key)
            self.assertEqual(s.position, want, key)

    def test_seeking_stops_at_the_start_and_the_end(self):
        s = self.src
        s.duration = 100.0
        s.position = 30.0
        s.command("1")                          # 1 minute back from 0:30
        self.assertEqual(s.seek_to, 0.0)
        s.command("2")
        self.assertEqual(s.seek_to, 0.0)
        s.position, s.seek_to = 80.0, None
        s.command("4")                          # 1 minute forward from 1:20
        self.assertAlmostEqual(s.seek_to, 99.9)  # the last frame at 10 fps
        s.command("3")
        self.assertAlmostEqual(s.seek_to, 99.9)
        s.command("6")                          # no frame after the last one
        self.assertAlmostEqual(s.seek_to, 99.9)
        s.command("5")
        self.assertAlmostEqual(s.seek_to, 99.8)

    def test_seeking_without_a_known_length(self):
        s = self.src
        s.duration = None
        s.position = 5.0
        s.command("4")
        self.assertEqual(s.seek_to, 65.0)
        s.command("1")
        s.command("1")
        self.assertEqual(s.seek_to, 0.0)

    def test_very_short_video(self):
        s = self.src
        s.duration = 0.05                       # shorter than one frame
        s.command("4")
        self.assertEqual(s.seek_to, 0.0)

    def test_seeks_add_up_before_the_decoder_follows(self):
        s = self.src
        s.duration = 1000.0
        s.position = 100.0
        s.command("3")
        s.command("3")
        self.assertEqual(s.seek_to, 120.0)

    def test_frame_step_pauses(self):
        s = self.src
        s.position = 10.0
        s.command("6")
        self.assertTrue(s.paused)
        self.assertAlmostEqual(s.seek_to, 10.1)
        s.command("5")
        self.assertAlmostEqual(s.seek_to, 10.0)

    def test_pause_and_rewind(self):
        s = self.src
        s.command(" ")
        self.assertTrue(s.paused)
        s.command(" ")
        self.assertFalse(s.paused)
        s.position = 50.0
        s.command("!")
        self.assertEqual((s.position, s.seek_to), (0.0, 0.0))

    def test_loop_marks(self):
        s = self.src
        s.position = 20.0
        s.command("8")
        s.position = 30.0
        s.command("9")
        self.assertEqual((s.loop_start, s.loop_end), (20.0, 30.0))
        s.position = 50.0
        s.command("0")
        self.assertEqual(s.seek_to, 20.0)
        s.position = 10.0                        # an end before the start: swapped
        s.command("9")
        self.assertEqual((s.loop_start, s.loop_end), (10.0, 20.0))
        s.command("7")
        self.assertEqual((s.loop_start, s.loop_end), (None, None))

    def test_help_and_unknown_keys(self):
        self.assertEqual(self.src.command("h"), "toggle")
        self.assertIsNone(self.src.command("x"))
        self.assertIsNone(self.src.command(""))
        # P no longer pauses, and there is no interlacing for I
        for key in ("p", "i", "I"):
            self.assertIsNone(self.src.command(key))
        self.assertFalse(self.src.paused)
        self.assertNotIn("interlace", self.src.settings())

    def test_no_shifted_variants(self):
        # D, R, Z and H work only without the shift key
        s = self.src
        s.matchers[80].brightness = 0.5
        s.position = 10.0
        for key in ("D", "R", "Z", "H", "S", "I"):
            self.assertIsNone(s.command(key), key)
        self.assertEqual((int(self.m.dither), self.m.brightness, s.zoom, s.seek_to),
                         (0, 0.5, "fit", None))
        # shift-SPACE is key code 160, which is no hotkey
        self.assertEqual(server.LiveSource.pet_key(160), "")

    def test_keys_in_the_search_screen(self):
        s = self.src
        s.menu = True
        self.assertIsNone(s.command("b"))
        self.assertEqual(self.m.brightness, 0.0)
        self.assertEqual(s.command("h"), "toggle")

    def test_settings_saved_by_a_server_with_interlacing(self):
        s = self.src
        st = dict(s.settings(), brightness=0.3, interlace="on")
        s.apply_settings(st)
        self.assertEqual(self.m.brightness, 0.3)
        self.assertNotIn("interlace", s.settings())


class DecoderTest(unittest.TestCase):
    """The decoder thread, with fake ffmpeg frames instead of a video."""

    def test_zoom_change_leaves_no_old_characters(self):
        # The converter keeps a cell's old character when the new one is only
        # a little better. After a zoom change the old frame has another
        # geometry, so the first new frame must be converted from scratch.
        rnd = np.random.default_rng(1)
        before = rnd.integers(0, 256, (200, 640)).astype(np.int16)
        after = np.clip(before + rnd.integers(-15, 16, before.shape), 0, 255).astype(np.uint8)
        frames = {"fit": before.astype(np.uint8), "fill": after, "stretch": after}

        def fake_frames(media, fps, width, height, stretch=False, max_frames=None,
                        realtime=False, loop=False, log=None, start=0.0, aspect=4 / 3,
                        zoom="fit"):
            for _ in range(max_frames or 10000):
                time.sleep(0.005)
                yield frames[zoom]

        ms = {cols: convert.Matcher(cols) for cols in (80, 40)}
        with mock.patch.object(convert, "gray_frames", fake_frames), \
                mock.patch.object(convert, "resolve", lambda url, log=None, audio=False: (url, 100.0)):
            src = server.LiveSource("clip.mp4", ms, 10.0)
            try:
                self.wait(lambda: src.count > 3)
                src.command("z")                        # fit -> fill
                count = src.count
                self.wait(lambda: src.zoom == "fill" and src.count > count + 5)
                latest = src.latest
            finally:
                src.url, src.switch = None, True        # the thread goes idle
        fresh = convert.Matcher(80).codes_for(after).tobytes()
        self.assertEqual(sum(a != b for a, b in zip(latest, fresh)), 0)

    def test_retry_countdown(self):
        src = live_source()
        self.assertIsNone(src.retry_text())
        src.retry_at = 100.0
        for now, left in ((90.0, 10), (92.5, 8), (99.0, 1), (99.95, 1), (101.0, 1)):
            with mock.patch.object(server.time, "monotonic", return_value=now):
                self.assertEqual(src.retry_text(),
                                 f"could not get the video, trying again in {left} s")

    def test_waiting_decoder_counts_down_and_stops_for_a_new_video(self):
        calls = []

        def no_video(url, log=None, audio=False):
            if not audio:                       # the sound prefetch does not count
                calls.append(url)
            return None, None

        ms = {cols: convert.Matcher(cols) for cols in (80, 40)}
        with mock.patch.object(convert, "resolve", no_video):
            src = server.LiveSource("https://example.com/gone", ms, 10.0)
            try:
                self.wait(lambda: src.retry_text() is not None)
                self.assertRegex(src.retry_text(),
                                 r"^could not get the video, trying again in (10|9) s$")
                src.switch = True                       # the user picked another video
                src.url = None
                self.wait(lambda: src.retry_at is None, timeout=2)
            finally:
                src.url, src.switch = None, True
        self.assertEqual(calls, ["https://example.com/gone"])   # no second try

    @staticmethod
    def wait(cond, timeout=10):
        deadline = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > deadline:
                raise AssertionError("the decoder did not get there")
            time.sleep(0.01)


class TerminalSearchTest(unittest.TestCase):
    """The search field in the server terminal: S, then text and RETURN."""

    def setUp(self):
        patcher = mock.patch.object(convert, "youtube_search",
                                    lambda q, c=9: SearchTest.RESULTS[:c])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.src = live_source()
        self.stream = server.Stream(self.src)
        r, self.w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, self.w)
        self.keys = server.TerminalKeys.__new__(server.TerminalKeys)
        self.keys.fd, self.keys.typing = r, None

    def type(self, text):
        os.write(self.w, text.encode())
        self.keys.poll(self.stream)
        self.assertTrue(self.src.search_done.wait(5))

    def test_the_field_stays_open_after_a_search(self):
        self.type("s")
        self.assertEqual(self.keys.typing, "")
        self.type("pet demos\r")
        self.assertEqual(self.src.query, "pet demos")
        self.assertEqual(self.keys.typing, "")       # ready for a number, N or P
        self.type("n\r")
        self.assertEqual(self.src.page, 1)
        self.assertEqual(self.keys.typing, "")
        self.type("99\r")                           # no such result
        self.assertEqual(self.keys.typing, "")
        self.type("23\r")
        self.assertEqual(self.src.url, SearchTest.RESULTS[22][2])
        self.assertIsNone(self.keys.typing)          # the video plays

    def test_empty_entry_goes_back_to_the_video(self):
        self.src.url = "https://example.com/v"
        self.type("s")
        self.type("\r")
        self.assertIsNone(self.src.menu)
        self.assertIsNone(self.keys.typing)

    def test_terminal_view_shows_the_results(self):
        # the PET does not read the video stream during a search, so the
        # terminal view must not show the frame sent last
        self.stream.last_codes = bytes([160]) * 2000
        self.type("s")
        self.type("pet demos\r")
        screen = self.stream.terminal_codes()
        self.assertEqual(screen, server.text_screen(self.src.results_lines(80)[:23], 80))
        self.assertIn(bytes(server.screen_code(c) for c in "YOUTUBE: PET DEMOS"), screen)
        self.type("1\r")                            # a video plays: its frames again
        self.assertEqual(self.stream.terminal_codes(), bytes([160]) * 2000)

    def test_escape_closes_the_field(self):
        self.src.url = "https://example.com/v"
        self.type("s")
        self.type("pet\r")
        self.type("\x1b")
        self.assertIsNone(self.keys.typing)
        self.assertIsNone(self.src.menu)


class OverlayTextTest(unittest.TestCase):

    STATES = [{}, {"paused": True}, {"loop_start": 5.0}, {"loop_start": 3723.0, "loop_end": 4000.0},
              {"duration": 3 * 3600 + 59 * 60}, {"duration": 42.0, "position": 41.0}]

    def sources(self):
        for st in self.STATES:
            for zoom in convert.ZOOM_MODES:
                s = live_source(zoom=zoom)
                for k, v in st.items():
                    setattr(s, k, v)
                s.matchers[80].brightness = -0.95
                yield s

    def test_lines_fit_the_screen(self):
        # the last column would wrap the line on the PET
        for s in self.sources():
            for cols in (80, 40):
                lines = s.overlay_lines(cols)
                self.assertLessEqual(len(lines), 25)
                for line in lines:
                    self.assertLessEqual(len(line), cols - 1, line)

    def test_items_stay_in_place(self):
        # a value that changes its width must not move the items after it
        def places(s, cols):
            out = {}
            for row, line in enumerate(s.overlay_lines(cols)):
                for m in re.finditer(r"[A-Z0-9!]+\)", line):
                    out[m.group()] = (row, m.start())
            return out

        for cols in (80, 40):
            base = None
            for zoom in convert.ZOOM_MODES:
                for paused in (False, True):
                    s = live_source(zoom=zoom)
                    s.paused = paused
                    p = places(s, cols)
                    base = base or p
                    self.assertEqual(p, base, (cols, zoom, paused))

    def test_values_show(self):
        s = live_source()
        s.paused = True
        text = "\n".join(s.overlay_lines(80))
        for want in ("B)RIGHTNESS:+0.00", "D)ITHER:OFF", "Z)OOM:FIT", "R)ESET",
                     "SPACE)PAUSE:ON", "8)LOOP START (NONE)", "H)ELP", "RUNSTOP)QUIT"):
            self.assertIn(want, text)
        self.assertNotIn("P)AUSE", text)
        self.assertNotIn("INTERLACE", text)

    def test_terminal_status_text(self):
        s = live_source()
        s.paused = True
        s.loop_start = 61.0
        text = s.status_text()
        self.assertIn("B)rightness:+0.00", text)
        self.assertIn("Z)oom:fit", text)
        self.assertIn("paused", text)
        self.assertIn("1:01", text)

    def test_timeline(self):
        s = live_source()
        s.duration = 600.0
        s.position = 300.0
        for width in (39, 79):
            line = s.timeline(width)
            self.assertLessEqual(len(line), width)
            self.assertTrue(line.startswith("0:00 "), line)
            self.assertIn("(5:00)", line)
            self.assertTrue(line.endswith(" 10:00"), line)


class SearchTest(unittest.TestCase):

    RESULTS = [(f"Video number {i} with a long title " * 3, 61.0 * i,
                f"https://www.youtube.com/watch?v=id{i}") for i in range(1, 46)]

    def setUp(self):
        self.calls = []

        def fake_search(query, count=9):
            self.calls.append((query, count))
            return self.RESULTS[:count]

        patcher = mock.patch.object(convert, "youtube_search", fake_search)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.src = live_source()

    def search(self, *parts):
        for p in parts:
            self.src.add_query_part(p)
        self.src.add_query_part("")
        self.assertTrue(self.src.search_done.wait(5))

    def test_query_in_pieces(self):
        self.search("PETSCII DEMOS ", "ON THE PET")
        self.assertEqual(self.calls, [("petscii demos on the pet", 20)])
        self.assertEqual(self.src.query_parts, [])
        lines = self.src.results_lines(80)
        self.assertEqual(lines[0], "YOUTUBE: PETSCII DEMOS ON THE PET  PAGE 1  N: MORE")
        self.assertEqual(len(lines), 2 + 20)

    def test_empty_query_does_not_search(self):
        self.src.add_query_part("   ")
        self.src.add_query_part("")
        self.assertEqual(self.calls, [])

    def test_results_fit_the_screen(self):
        self.search("x")
        for cols in (80, 40):
            for line in self.src.results_lines(cols):
                self.assertLessEqual(len(line), cols - 1, line)
            data = self.src.results_file(cols)
            self.assertEqual(len(data), cols * 25)
            self.assertEqual(data[-2 * cols:], bytes([32]) * 2 * cols)   # the input rows

    def test_numbers_and_durations(self):
        self.search("x")
        lines = self.src.results_lines(80)
        self.assertTrue(lines[2].startswith(" 1 VIDEO NUMBER 1"), lines[2])
        self.assertTrue(lines[2].endswith(" (1:01)"), lines[2])
        self.assertTrue(lines[21].startswith("20 "), lines[21])

    def test_pages(self):
        self.search("x")
        self.src.entry("N")
        self.assertTrue(self.src.search_done.wait(5))
        self.assertEqual(self.calls[-1], ("x", 40))
        self.assertTrue(self.src.results_lines(80)[2].startswith("21 "))
        self.src.entry("P")
        self.assertTrue(self.src.results_lines(80)[2].startswith(" 1 "))
        self.src.entry("P")                      # no page before the first
        self.assertEqual(self.src.page, 0)

    def test_pick_plays_the_video(self):
        self.search("x")
        s = self.src
        s.loop_start, s.paused = 5.0, True
        s.entry("3")
        self.assertEqual(s.url, self.RESULTS[2][2])
        self.assertEqual(s.duration, self.RESULTS[2][1])
        self.assertIsNone(s.menu)
        self.assertEqual((s.loop_start, s.paused, s.switch), (None, False, True))

    def test_a_number_that_is_not_shown(self):
        self.search("x")
        self.src.entry("99")
        self.assertIsNone(self.src.url)
        self.src.entry("0")
        self.assertIsNone(self.src.url)

    def test_other_text_is_a_new_search(self):
        self.search("x")
        self.src.entry("C64 Demos")
        self.assertTrue(self.src.search_done.wait(5))
        self.assertEqual(self.calls[-1], ("c64 demos", 20))

    def test_no_results(self):
        with mock.patch.object(convert, "youtube_search", lambda q, c=9: []):
            self.search("nothing")
        self.assertIn("NO RESULTS", self.src.results_lines(80))

    def test_search_failure(self):
        def broken(query, count=9):
            raise OSError("no network")
        with mock.patch.object(convert, "youtube_search", broken):
            self.search("x")
        self.assertIn("NO RESULTS", self.src.results_lines(80))

    def test_while_searching(self):
        s = self.src
        s.query = "slow"
        s.search_done.clear()
        self.assertEqual(s.results_lines(80), ["YOUTUBE: SLOW", "", "SEARCHING..."])
        s.search_done.set()

    def test_search_screen_stops_the_video_and_back(self):
        s = self.src
        s.url = "https://example.com/v"
        s.position = 42.0
        s.show_results()
        self.assertEqual((s.menu, s.seek_to), (True, 42.0))
        s.entry("")
        self.assertIsNone(s.menu)

    def test_start_screen(self):
        self.assertEqual(self.src.results_lines(40), ["PETDISK VIDEO (PDV) - YOUTUBE SEARCH"])


if __name__ == "__main__":
    unittest.main()
