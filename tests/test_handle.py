"""The petdisk.php requests that the PETdisk MAX sends, answered by
handle()."""

import os
import tempfile
import unittest
from unittest import mock

from pdvtest import FakeSource, convert, live_source, server


def parse(resp):
    head, body = resp.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n")[0].decode()
    length = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0])
    assert length == len(body), (length, len(body))
    return status, body


class HandleTest(unittest.TestCase):

    def setUp(self):
        self.stream = server.Stream(live_source())
        self.log = []

    def get(self, query):
        return parse(server.handle(query, self.stream, self.log.append))

    def test_directory(self):
        status, body = self.get("d=1&p=0")
        self.assertEqual(status, "HTTP/1.0 200 OK")
        names = body.decode().split("\n")
        self.assertIn("PDV80.PRG", names)
        self.assertIn("PDV40.PRG", names)
        self.assertTrue(body.endswith(b"\n\n"))
        self.assertNotIn(b"VIDEO", body)        # opened by name only
        self.assertEqual(self.get("d=1&p=1"), ("HTTP/1.0 200 OK", b"\n"))

    def test_file_size_and_ranges(self):
        path = os.path.join(server.SERVE_DIR, "PDV80.PRG")
        data = open(path, "rb").read()
        self.assertEqual(self.get("file=PDV80.PRG&l=1")[1], f"{len(data)}\r\n".encode())
        self.assertEqual(self.get("file=pdv80.prg&l=1")[1], f"{len(data)}\r\n".encode())
        got = b"".join(self.get(f"file=PDV80.PRG&s={s}&e={s + 512}")[1]
                       for s in range(0, len(data), 512))
        self.assertEqual(got, data)

    def test_a_missing_file_is_size_0(self):
        # the firmware does not look at the status: an empty body would
        # give it a random size, and the PET would load garbage or hang
        self.assertEqual(self.get("file=KISSA.PRG&l=1"), ("HTTP/1.0 200 OK", b"0\r\n"))
        self.assertEqual(self.get("file=kissa&l=1"), ("HTTP/1.0 200 OK", b"0\r\n"))
        self.assertEqual(self.get("file=KISSA.PRG&s=0&e=512"), ("HTTP/1.0 200 OK", b""))

    def test_broken_requests_get_safe_answers(self):
        self.assertEqual(self.get("x=1"), ("HTTP/1.0 400 Bad Request", b"0\r\n"))
        self.assertEqual(self.get("file=PDV80.PRG"), ("HTTP/1.0 400 Bad Request", b""))
        self.assertEqual(self.get("file=PDV80.PRG&s=abc&e=512"), ("HTTP/1.0 400 Bad Request", b""))
        self.assertEqual(self.get("d=1&p=x"), ("HTTP/1.0 400 Bad Request", b"\n"))

    def test_an_internal_error_gets_a_safe_answer(self):
        with mock.patch.object(server, "find_file", side_effect=OSError("disk gone")), \
                mock.patch.object(server, "say"):
            self.assertEqual(self.get("file=PDV80.PRG&l=1"), ("HTTP/1.0 400 Bad Request", b"0\r\n"))
            self.assertEqual(self.get("file=PDV80.PRG&s=0&e=512"), ("HTTP/1.0 400 Bad Request", b""))

    def test_real_files_win_over_command_names(self):
        with tempfile.TemporaryDirectory() as d:
            for name, data in (("QBERT.PRG", b"q" * 700), ("K5.PRG", b"k" * 10), ("CN.SEQ", b"c" * 3)):
                with open(os.path.join(d, name), "wb") as f:
                    f.write(data)
            with mock.patch.object(server, "SERVE_DIR", d):
                self.assertEqual(self.get("file=QBERT.PRG&l=1")[1], b"700\r\n")
                self.assertEqual(self.get("file=QBERT.PRG&s=512&e=1024")[1], b"q" * 188)
                self.assertEqual(self.get("file=K5.PRG&l=1")[1], b"10\r\n")
                self.assertEqual(self.get("file=CN.SEQ&l=1")[1], b"3\r\n")
                self.assertEqual(self.get("file=K66&l=1")[1], b"0\r\n")    # still a hotkey
        src = self.stream.source
        self.assertEqual(src.query_parts, [])                     # QBERT was no search text
        self.assertEqual(src.matchers[80].brightness, 0.05)       # K66 was B

    def test_errors(self):
        # nobody on the network can make the server build huge responses
        for s, e in ((0, server.MAX_RANGE + 1), (-1, 10), (10, 5)):
            self.assertEqual(self.get(f"file=VIDEO80.SEQ&s={s}&e={e}")[0],
                             "HTTP/1.0 416 Range Not Satisfiable")

    def test_responses_fit_the_firmware(self):
        resp = server.handle("file=VIDEO80.SEQ&s=0&e=512", self.stream, self.log.append)
        self.assertLessEqual(len(resp), server.MAX_RESPONSE)

    def test_video_size_and_search_marker(self):
        for name, cols in (("VIDEO80.SEQ", 80), ("video40.seq", 40)):
            self.assertEqual(self.get(f"file={name}&l=1")[1], f"{server.VIDEO_SIZE}\r\n".encode())
            body = self.get(f"file={name}&s=0&e=512")[1]
            self.assertEqual(body[0], server.SEARCH)
            self.assertEqual(self.stream.cols, cols)

    def test_video_frames(self):
        src = FakeSource(bytes(range(256)) * 7 + bytes(208), cols=80)
        self.stream = server.Stream(src)
        body = self.get("file=VIDEO80.SEQ&s=0&e=512")[1]
        self.assertEqual(len(body), 512)
        self.assertIn(body[0], (server.KEY, server.RLE))

    def test_hotkeys(self):
        src = self.stream.source
        status, body = self.get("file=K90&l=1")             # Z
        self.assertEqual(body, b"0\r\n")
        self.assertEqual(src.zoom, "fill")
        self.get("file=K66.SEQ&l=1")                         # B
        self.assertAlmostEqual(src.matchers[80].brightness, 0.05)
        self.get("file=K194&l=1")                            # shift-B
        self.assertAlmostEqual(src.matchers[80].brightness, 0.0)
        self.get("file=K72&l=1")                             # H: overlay on
        self.assertTrue(self.stream.overlay_on)
        self.get("file=K200&l=1")                            # shift-H: no hotkey
        self.assertTrue(self.stream.overlay_on)
        self.get("file=K32&l=1")                             # SPACE
        self.assertTrue(src.paused)
        self.assertEqual(self.get("file=K13&l=1")[1], b"0\r\n")   # ignored

    def test_search_text_entries_and_results(self):
        src = self.stream.source
        with mock.patch.object(convert, "youtube_search",
                               lambda q, c=9: [("A PET VIDEO", 75.0, "https://youtu.be/x")]):
            self.get("file=QPET%20VID&l=1")
            self.get("file=QEOS&l=1")
            self.get("file=Q&l=1")
            self.assertTrue(src.search_done.wait(5))
            self.assertEqual(src.query, "pet videos")
            size = int(self.get("file=RESULTS80&l=1")[1])
            self.assertEqual(size, 2000)
            screen = b"".join(self.get(f"file=RESULTS80.SEQ&s={s}&e={s + 512}")[1]
                              for s in range(0, size, 512))
            self.assertEqual(screen, src.results_file(80))
            self.assertIn(bytes(server.screen_code(c) for c in "A PET VIDEO"), screen)
            self.assertEqual(self.get("file=C1&l=1")[1], b"0\r\n")
            self.assertEqual(src.url, "https://youtu.be/x")

    def pick_file(self):
        size = int(self.get("file=PICK&l=1")[1])
        return self.get(f"file=PICK.SEQ&s=0&e=512")[1][:size]

    def test_pick_after_a_terminal_search(self):
        # the PET shows its own search screen and asks the server every
        # 2.5 s whether a video was picked in the server terminal
        src = self.stream.source
        self.assertEqual(self.get("file=VIDEO80.SEQ&s=0&e=512")[1][0], server.SEARCH)
        self.get("file=RESULTS80&l=1")                       # the PET's search screen
        self.assertEqual(self.pick_file(), b"0")
        src.results = [("A PET VIDEO", 75.0, "https://youtu.be/x")]
        src.pick(1)                                          # picked in the terminal
        self.assertEqual(self.pick_file(), b"1")

    def test_pick_after_the_terminal_closes_the_search(self):
        src = self.stream.source
        src.url = "https://youtu.be/playing"
        self.get("file=RESULTS80&l=1")                       # S on the PET
        self.assertEqual(self.pick_file(), b"0")
        src.entry("")                                        # empty entry in the terminal
        self.assertEqual(self.pick_file(), b"1")

    def test_no_pick_while_the_search_screen_shows(self):
        src = self.stream.source
        self.get("file=RESULTS40&l=1")
        src.entry("99")                                      # no such result
        self.assertEqual(self.pick_file(), b"0")

    def test_results_are_read_from_one_snapshot(self):
        # a new search while the PET reads the file must not mix two screens
        src = self.stream.source
        self.get("file=RESULTS40&l=1")
        first = self.get("file=RESULTS40&s=0&e=512")[1]
        src.query, src.results = "new", [("NEW", None, "u")]
        self.assertEqual(self.get("file=RESULTS40&s=0&e=512")[1], first)


if __name__ == "__main__":
    unittest.main()
