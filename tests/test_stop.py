"""Ctrl+C on the server: the PETdisk crashes when it cannot connect, so
the server first makes the PET end PDV, then stops."""

import os
import signal
import time
import unittest

from pdvtest import FakeSource, HAVE_FFMPEG, decode, live_source, make_video, noise, seeded, server
from test_audio import FakeFeed, after_marker, packets
from test_http import ServerProcess

from unittest import mock

BLOCK = 512


class StreamStopTest(unittest.TestCase):

    def reader(self, src):
        stream = server.Stream(src)
        state = {"offset": 0, "data": b""}

        def blocks(n):
            for _ in range(n):
                state["data"] += stream.read(state["offset"], state["offset"] + BLOCK)
                state["offset"] += BLOCK
            return state["data"]
        return stream, blocks

    def test_video_mode(self):
        rnd = seeded(1)
        src = FakeSource(noise(rnd, 2000))
        stream, blocks = self.reader(src)
        blocks(5)
        stream.stopping = 1.0
        src.show(noise(rnd, 2000))
        data = blocks(3)
        frames = decode(data, 2000)
        self.assertEqual(frames[-1][0], "stopped")
        self.assertTrue(stream.stop_sent)
        tail = data[data.rindex(bytes([server.STOPPED])) + 1:]
        self.assertEqual(set(tail), {server.PAD})                 # nothing after it

    def test_audio_mode(self):
        with mock.patch.object(server, "AudioFeed", FakeFeed):
            src = FakeSource(bytes([160]) * 2000)
            src.audio = True
            stream, blocks = self.reader(src)
            blocks(4)
            stream.stopping = 1.0
            data = blocks(4)
        pk, rest = packets(after_marker(data))
        self.assertIsNotNone(rest, "no LEAVE at a packet start")
        self.assertEqual(rest[0], server.STOPPED)
        self.assertTrue(FakeFeed.made[-1].closed)

    def test_a_new_session_gets_it_too(self):
        src = FakeSource(bytes([32]) * 2000)
        stream = server.Stream(src)
        stream.stopping = 1.0
        stream.stop_sent = True                                   # sent in an earlier session
        self.assertEqual(stream.read(0, BLOCK)[0], server.STOPPED)

    def test_without_a_video(self):
        src = FakeSource(url=None)
        stream = server.Stream(src)
        stream.stopping = 1.0
        self.assertEqual(stream.read(0, BLOCK)[0], server.STOPPED)

    def test_the_search_screen_gets_2(self):
        stream = server.Stream(live_source())
        get = lambda q: server.handle(q, stream, lambda m: None).split(b"\r\n\r\n", 1)[1]
        self.assertEqual(get("file=PICK&s=0&e=512"), b"0")
        stream.stopping = 1.0
        self.assertEqual(get("file=PICK&l=1"), b"1\r\n")
        self.assertFalse(stream.stop_sent)                        # only the size so far
        self.assertEqual(get("file=PICK.SEQ&s=0&e=512"), b"2")
        self.assertTrue(stream.stop_sent)


class ProcessStopTest(unittest.TestCase):
    """The server process on SIGINT (Ctrl+C) and SIGTERM (docker stop)."""

    def stop_and_wait(self, srv, sig=signal.SIGINT):
        srv.proc.send_signal(sig)
        t = time.monotonic()
        srv.proc.wait(30)
        return time.monotonic() - t

    def test_without_a_pet_it_stops_soon(self):
        srv = ServerProcess().start()
        try:
            took = self.stop_and_wait(srv)
            self.assertLess(took, 8)
            self.assertIn("stopped", srv.log())
        finally:
            srv.stop()

    def test_docker_stop_is_clean_too(self):
        srv = ServerProcess().start()
        try:
            self.assertLess(self.stop_and_wait(srv, signal.SIGTERM), 8)
            self.assertIn("stopping", srv.log())
        finally:
            srv.stop()

    def test_a_second_ctrl_c_stops_at_once(self):
        srv = ServerProcess().start()
        try:
            srv.proc.send_signal(signal.SIGINT)
            srv.wait_log("stopping")
            t = time.monotonic()
            srv.proc.send_signal(signal.SIGINT)
            srv.proc.wait(10)
            self.assertLess(time.monotonic() - t, 2)
        finally:
            srv.stop()

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are not installed")
    def test_the_pet_ends_pdv_first(self):
        srv = ServerProcess(lambda s: make_video(os.path.join(s.dir, "clip.mp4"), 10)).start()
        try:
            data = srv.read_video("VIDEO80.SEQ", 2)
            srv.proc.send_signal(signal.SIGINT)
            srv.wait_log("stopping")
            data += srv.read_video("VIDEO80.SEQ", 1, start=len(data))
            self.assertEqual(decode(data, 2000)[-1][0], "stopped")
            t = time.monotonic()                                  # the PET sends no more
            srv.proc.wait(10)
            self.assertLess(time.monotonic() - t, 4)
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
