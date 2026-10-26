"""The server as a process, over HTTP, like the PETdisk MAX uses it. Each
test class runs server.py from a copy of the project in a temporary
directory, so the tests write nothing into the project."""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from pdvtest import HAVE_FFMPEG, ROOT, decode, free_port, make_video, server

BLOCK = 512


class ServerProcess:

    def __init__(self, *args):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        for name in ("server.py", "convert.py", "petscii_unicode.py"):
            shutil.copy(os.path.join(ROOT, name), d)
        shutil.copytree(os.path.join(ROOT, "serve"), os.path.join(d, "serve"))
        os.mkdir(os.path.join(d, "build"))
        self.dir = d
        self.port = free_port()
        self.log_path = os.path.join(d, "server.log")
        self.args = args

    def start(self):
        env = dict(os.environ, PDV_NO_UPDATE="1")
        self.log_file = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "server.py", "--plain", "--port", str(self.port)]
            + [a(self) if callable(a) else a for a in self.args],
            cwd=self.dir, env=env, stdin=subprocess.DEVNULL,
            stdout=self.log_file, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return self
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise AssertionError(f"the server did not start:\n{self.log()}")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.log_file.close()
        self.tmp.cleanup()

    def log(self):
        with open(self.log_path) as f:
            return f.read()

    def wait_log(self, text, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.log():
                return
            time.sleep(0.1)
        raise AssertionError(f"not in the server log: {text!r}\n{self.log()}")

    def get(self, query):
        """(HTTP status, body)."""
        url = f"http://127.0.0.1:{self.port}/petdisk.php?{query}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def read_video(self, name, seconds, start=0):
        """Read the video file block by block at about the PETdisk's pace."""
        data = bytearray()
        offset = start
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status, body = self.get(f"file={name}&s={offset}&e={offset + BLOCK}")
            assert status == 200 and len(body) == BLOCK, (status, len(body))
            data += body
            offset += BLOCK
            time.sleep(0.1)
        return bytes(data)


class NoVideoTest(unittest.TestCase):
    """Without a video the PET goes to the YouTube search. No ffmpeg needed."""

    @classmethod
    def setUpClass(cls):
        cls.server = ServerProcess().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_search_marker(self):
        status, body = self.server.get("file=VIDEO80.SEQ&s=0&e=512")
        self.assertEqual(status, 200)
        self.assertEqual(body, bytes([server.SEARCH]) + bytes([server.PAD]) * 511)

    def test_programs(self):
        status, listing = self.server.get("d=1&p=0")
        self.assertIn(b"PDV80.PRG\n", listing)
        for name in ("PDV80.PRG", "PDV40.PRG"):
            with open(os.path.join(ROOT, "serve", name), "rb") as f:
                want = f.read()
            size = int(self.server.get(f"file={name}&l=1")[1])
            got = b"".join(self.server.get(f"file={name}&s={s}&e={s + BLOCK}")[1]
                           for s in range(0, size, BLOCK))
            self.assertEqual(got, want)

    def test_errors(self):
        self.assertEqual(self.server.get("file=NOPE.PRG&l=1")[0], 404)
        self.assertEqual(self.server.get("file=VIDEO80.SEQ&s=0&e=100000")[0], 416)
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as s:
            s.sendall(b"POST /petdisk.php HTTP/1.0\r\n\r\n")
            self.assertTrue(s.recv(100).startswith(b"HTTP/1.0 405"))

    def test_slow_or_broken_clients(self):
        # a client that sends nothing or closes at once must not stop the server
        socket.create_connection(("127.0.0.1", self.server.port), timeout=5).close()
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as s:
            s.sendall(b"GET /petdisk.php?file=PDV")
        self.assertEqual(self.server.get("file=PDV80.PRG&l=1")[0], 200)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are not installed")
class VideoTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ServerProcess(
            lambda s: make_video(os.path.join(s.dir, "clip.mp4"), 10)).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def play(self, name, cols, seconds=3):
        data = self.server.read_video(name, seconds)
        frames = decode(data, cols * 25)
        self.assertIn(frames[0][0], (server.KEY, server.RLE))
        # 10 frames per second are due; HTTP and ffmpeg start-up take some
        self.assertGreaterEqual(len(frames), 3 * seconds)
        self.assertGreater(len({f[1] for f in frames}), seconds)  # the picture moves
        return frames

    def test_80_columns(self):
        self.play("VIDEO80.SEQ", 80)

    def test_40_columns(self):
        self.play("VIDEO40.SEQ", 40)

    def test_hotkey_and_new_session(self):
        self.play("VIDEO80.SEQ", 80, 1)
        self.assertEqual(self.server.get("file=K68&l=1"), (200, b"0\r\n"))
        self.server.wait_log("PET key d")
        self.play("VIDEO80.SEQ", 80, 1)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are not installed")
class RestartTest(unittest.TestCase):
    """When server.py or convert.py changes, the server restarts itself and
    the PET must not notice: the stream goes on with the same bytes."""

    def test_stream_goes_on(self):
        srv = ServerProcess(lambda s: make_video(os.path.join(s.dir, "clip.mp4"), 10)).start()
        try:
            before = srv.read_video("VIDEO80.SEQ", 2)
            t = time.time() + 5
            os.utime(os.path.join(srv.dir, "convert.py"), (t, t))
            after = srv.read_video("VIDEO80.SEQ", 4, start=len(before))
            srv.wait_log("code changed, restarting server")
            srv.wait_log("continuing the stream after restart")
            frames_before = decode(before, 2000)
            frames = decode(before + after, 2000)     # no new key frame needed
            self.assertGreater(len(frames), len(frames_before) + 8)
            self.assertNotIn("could not restore", srv.log())
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
