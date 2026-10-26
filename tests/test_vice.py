"""Optional: the PET client in the VICE emulator. These tests run only in
the headless Docker container of tests/vice/run.sh, which has VICE on a
virtual display and the PET ROMs of a local VICE. Everywhere else they are
skipped, so they never open a window on the desktop.

The tests play streams that the server's encoders make, from an emulated
8050 drive as unit 10, with the committed serve/PDV80.PRG and
serve/PDV40.PRG. They then compare the PET screen memory with the frame
that the PET must show."""

import os
import socket
import subprocess
import tempfile
import time
import unittest

from pdvtest import ROOT, changed, free_port, noise, runs, seeded, server

ENABLED = os.environ.get("PDV_VICE_DOCKER") == "1"


# ---- the streams

def frame_chain(rnd, cols):
    """A key frame, a run-length key frame and delta frames of every kind,
    with padding between them. Returns (stream, the last frame). There is
    no key frame after the run-length frame, so its errors stay on screen."""
    cells = cols * 25
    pad = bytes([server.PAD])
    first = noise(rnd, cells)
    out = bytearray([server.KEY]) + first + pad * 3
    prev = runs(rnd, cells)
    out += server.encode_rle(prev) + pad * 17
    steps = []
    a = bytearray(prev)                       # long skips and long runs
    a[0] = a[cells - 1] = 7
    a[300:300 + 300] = bytes([1]) * 300
    steps.append(bytes(a))
    steps.append(changed(rnd, a, 40))
    steps.append(changed(rnd, steps[-1], cells // 2))
    for _ in range(6):
        steps.append(changed(rnd, steps[-1], rnd.randrange(1, 300)))
    for cur in steps:
        out += server.encode_delta(prev, cur) + pad * rnd.randrange(0, 40)
        prev = cur
    return bytes(out), prev


def broken_frames(rnd, cols):
    """Broken frames, which the client must drop, then a good frame."""
    cells = cols * 25
    pad = bytes([server.PAD]) * 20
    good = runs(rnd, cells)
    too_long = bytes([server.RLE]) + bytes([255, 1]) * (cells // 129 + 2)
    past_end = bytes([server.DELTA]) + bytes([255, 0]) * (cells // 255 + 1) + bytes([1, 1, 0, 0])
    return (bytes([server.KEY]) + noise(rnd, cells) + pad + too_long + pad +
            past_end + pad + bytes([0x12, 0x00, 0x80]) + server.encode_rle(good) + pad), good


@unittest.skipUnless(ENABLED, "runs only in the VICE container: tests/vice/run.sh")
class ViceTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def disk(self, cols, files):
        """A D80 disk image with the client and the given SEQ files."""
        d = self.tmp.name
        image = os.path.join(d, "test.d80")
        cmd = ["c1541", "-format", "pdv,01", "d80", image,
                            "-write", os.path.join(ROOT, "serve", f"PDV{cols}.PRG"), f"pdv{cols}"]
        for name, data in files.items():
            path = os.path.join(d, name)
            with open(path, "wb") as f:
                f.write(data)
            cmd += ["-write", path, f"{name},s"]
        subprocess.run(cmd, check=True, capture_output=True)
        return image

    def run_pet(self, cols, files, want, rows=25, timeout=180):
        """Run the client on a PET with the disk as unit 10, until the
        screen shows `want` in its first `rows` rows."""
        image = self.disk(cols, files)
        self.answer = None
        port = free_port()
        dump = os.path.join(self.tmp.name, "screen.bin")
        model = "8032" if cols == 80 else "4032"
        log = os.path.join(self.tmp.name, "vice.log")
        log_file = open(log, "w")
        self.addCleanup(log_file.close)
        vice = subprocess.Popen(
            ["xpet", "-model", model, "-warp", "-drive10type", "8050",
                         "-drive10truedrive", "-10", image,
                         "-remotemonitor", "-remotemonitoraddress", f"ip4://127.0.0.1:{port}",
                         "-keybuf", f'load"pdv{cols}",10\\nrun\\n'],
            stdout=log_file, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + timeout
            screen = None
            while time.monotonic() < deadline:
                time.sleep(2)
                screen = self.screen(port, dump, cols)
                if screen is not None and screen[:rows * cols] == want[:rows * cols]:
                    return
            with open(log) as f:
                tail = "".join(f.readlines()[-15:])
            self.fail(f"the PET screen did not show the frame; last screen: {screen!r}\n"
                      f"last monitor answer: {self.answer!r}\nVICE log:\n{tail}")
        finally:
            try:
                self.monitor(port, "quit", timeout=5)
            except OSError:
                pass
            try:
                vice.wait(15)
            except subprocess.TimeoutExpired:
                vice.kill()
                vice.wait()

    @staticmethod
    def monitor(port, command, answer=b"", timeout=10):
        """Send one command to the VICE monitor and wait for its answer.
        The emulator stops while the monitor is open, "x" continues it."""
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(command.encode() + b"\n")
            got = b""
            deadline = time.monotonic() + timeout
            # "quit": wait until VICE closes the connection
            while (command == "quit" or answer not in got) and time.monotonic() < deadline:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                got += chunk
            if command != "quit":
                s.sendall(b"x\n")
            return got

    def screen(self, port, dump, cols):
        """The screen memory, saved by the VICE monitor, or None."""
        if os.path.exists(dump):
            os.remove(dump)
        try:
            self.answer = self.monitor(port, f'bsave "{dump}" 0 8000 {0x8000 + cols * 25 - 1:x}',
                                       b"Saving")
        except OSError as e:
            self.answer = repr(e)
            return None                      # the emulator is not ready yet
        for _ in range(20):
            if os.path.exists(dump) and os.path.getsize(dump) == cols * 25:
                with open(dump, "rb") as f:
                    return f.read()
            time.sleep(0.1)
        return None

    def test_frames_80(self):
        stream, want = frame_chain(seeded(1), 80)
        self.run_pet(80, {"video80": stream + bytes([server.PAD]) * 4000}, want)

    def test_frames_40(self):
        stream, want = frame_chain(seeded(2), 40)
        self.run_pet(40, {"video40": stream + bytes([server.PAD]) * 4000}, want)

    def test_broken_frames_are_dropped(self):
        stream, want = broken_frames(seeded(3), 80)
        self.run_pet(80, {"video80": stream + bytes([server.PAD]) * 4000}, want)

    def test_search_screen(self):
        # $FC sends the PET to the search screen, which reads RESULTS80
        src = FakeResults()
        results = server.LiveSource.results_file(src, 80)
        self.run_pet(80, {"video80": bytes([server.SEARCH]) + bytes([server.PAD]) * 600,
                          "results80": results}, results, rows=23)


class FakeResults:
    """Just enough of a LiveSource for results_file()."""
    PAGE_SIZE = server.LiveSource.PAGE_SIZE
    query, page, more_possible = "commodore pet", 0, False
    results = [("THE COMMODORE PET STORY", 754.0, "u1"), ("PETSCII ART", None, "u2")]

    def __init__(self):
        import threading
        self.search_done = threading.Event()
        self.search_done.set()
        self.url, self.menu, self.position = None, None, 0.0

    def show_results(self):
        self.menu = True

    def results_lines(self, cols):
        return server.LiveSource.results_lines(self, cols)


if __name__ == "__main__":
    unittest.main()
