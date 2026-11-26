#!/usr/bin/env python3
"""Spiky, repeatable system load for the README screenshots (tools/readme-shots.sh).

usage: tools/demo-load.py [SECONDS] [--net URL]

Runs until SECONDS have passed (default: until terminated) and leaves nothing behind:
- CPU: bursts of `xz` compressors on 1 to 14 cores, a few seconds each, with quiet gaps
  (the exiting compressors also fill the History tab and the new/terminated row colours)
- memory: this process grows to a few GiB in steps (checksumming each one: CPU of its
  own), drops back and grows again
- disk: direct-I/O writes and reads of a scratch file under ~/.cache (not tmpfs)
- GPU (where there is one, with GStreamer's GL plugins and ffmpeg with VAAPI): bursts of a
  shader drawing a Mandelbrot set at varying frame rates (3D engine) and of hardware video
  encoding (video engine)
- network (only with --net): short downloads of URL with curl, discarded

The process calls itself demo-load.py, so it can be found by name in Procular.
"""

import ctypes
import hashlib
import os
import shutil
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time

GIB = 1 << 30


def set_name(name):
    try:
        ctypes.CDLL(None).prctl(15, name.encode()[:15], 0, 0, 0)  # PR_SET_NAME
    except (OSError, AttributeError):
        pass


class Load:
    def __init__(self, seconds, net_url):
        self.end = time.monotonic() + seconds if seconds else None
        self.net_url = net_url
        self.stop = threading.Event()
        self.children = set()
        self.lock = threading.Lock()
        self.rng = random.Random(7)  # the same shape on every run
        self.scratch = tempfile.mkdtemp(prefix="procular-demo-",
                                        dir=os.path.expanduser("~/.cache"))

    def running(self):
        if self.end is not None and time.monotonic() >= self.end:
            self.stop.set()
        return not self.stop.is_set()

    def sleep(self, seconds):
        self.stop.wait(seconds)

    def spawn(self, argv, **kw):
        proc = subprocess.Popen(argv, stdin=kw.pop("stdin", subprocess.DEVNULL),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        with self.lock:
            self.children.add(proc)
        return proc

    def reap(self, proc, timeout=None):
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
        with self.lock:
            self.children.discard(proc)

    # ---- CPU: compressor bursts of varying width and length
    def cpu(self):
        rng = random.Random(1)
        while self.running():
            width = rng.choice([1, 2, 3, 4, 6, 8, 10, 14])
            length = rng.uniform(1.5, 6.0)
            with open("/dev/urandom", "rb") as random_data:
                procs = [self.spawn(["xz", "-9", "-T1", "-c"], stdin=random_data) for _ in range(width)]
            self.sleep(length)
            for p in procs:
                p.terminate()
            for p in procs:
                self.reap(p)
            self.sleep(rng.uniform(1.0, 5.0))

    # ---- memory: a sawtooth of a few GiB in this process, with some CPU of its own
    def memory(self):
        rng = random.Random(2)
        blocks = []
        while self.running():
            target = rng.choice([2, 3, 4, 5])
            while len(blocks) < target * 4 and self.running():
                block = bytearray(GIB // 4)
                for i in range(0, len(block), 4096):  # touch the pages: count as resident
                    block[i] = 1
                blocks.append(block)
                # checksum what is there so far, on a few threads (hashlib releases the GIL)
                workers = [threading.Thread(target=lambda b=b: hashlib.sha256(b).digest())
                           for b in blocks[-rng.randint(1, 4):]]
                for t in workers:
                    t.start()
                for t in workers:
                    t.join()
                self.sleep(rng.uniform(0.8, 2.0))
            self.sleep(rng.uniform(4, 10))
            keep = rng.randint(0, 3)
            del blocks[keep:]
            self.sleep(rng.uniform(3, 8))

    # ---- disk: direct I/O, so the page cache does not swallow it
    def disk(self):
        rng = random.Random(3)
        path = os.path.join(self.scratch, "blob")
        while self.running():
            self.sleep(rng.uniform(3, 9))
            if not self.running():
                break
            count = rng.choice([256, 512, 768, 1024])
            self.reap(self.spawn(["dd", "if=/dev/zero", f"of={path}", "bs=1M", f"count={count}",
                                  "oflag=direct", "conv=fsync"]), timeout=60)
            self.sleep(rng.uniform(1, 4))
            self.reap(self.spawn(["dd", f"if={path}", "of=/dev/null", "bs=1M", "iflag=direct"]), timeout=60)

    # ---- GPU: shader and video encoder bursts; quietly nothing where the tools are missing
    def gpu(self):
        rng = random.Random(5)
        render = next((os.path.join("/dev/dri", n) for n in sorted(os.listdir("/dev/dri"))
                       if n.startswith("renderD")), None) if os.path.isdir("/dev/dri") else None
        shader = shutil.which("gst-launch-1.0")
        encoder = shutil.which("ffmpeg") if render else None
        while self.running() and (shader or encoder):
            self.sleep(rng.uniform(2, 7))
            if not self.running():
                break
            length = rng.uniform(2, 6)
            if shader and (not encoder or rng.random() < 0.7):
                fps = rng.choice([4, 8, 15, 30, 60])  # below what the GPU manages: less than 100 %
                argv = [shader, "-q", "gltestsrc", "pattern=mandelbrot", "is-live=true", "!",
                        f"video/x-raw(memory:GLMemory),width=3840,height=2160,framerate={fps}/1",
                        "!", "gldownload", "!", "fakesink", "sync=false"]
            else:
                argv = [encoder, "-loglevel", "quiet", "-vaapi_device", render, "-f", "lavfi",
                        "-i", "testsrc2=s=1920x1080:r=60", "-vf", "format=nv12,hwupload",
                        "-c:v", "hevc_vaapi", "-f", "null", "-"]
            proc = self.spawn(argv)
            self.sleep(length)
            proc.terminate()
            self.reap(proc, timeout=5)

    # ---- network (opt-in): short downloads
    def net(self):
        rng = random.Random(4)
        while self.running():
            self.sleep(rng.uniform(6, 15))
            if not self.running():
                break
            self.reap(self.spawn(["curl", "-s", "-o", "/dev/null", "--max-time", f"{rng.uniform(2, 5):.1f}",
                                  self.net_url]), timeout=30)

    def run(self):
        workers = [self.cpu, self.memory, self.disk, self.gpu] + ([self.net] if self.net_url else [])
        threads = [threading.Thread(target=w, daemon=True) for w in workers]
        for t in threads:
            t.start()
        try:
            while self.running():
                self.sleep(0.5)
        finally:
            self.stop.set()
            with self.lock:
                for p in self.children:
                    p.terminate()
            for t in threads:
                t.join(10)
            for name in os.listdir(self.scratch):
                os.unlink(os.path.join(self.scratch, name))
            os.rmdir(self.scratch)


def main():
    args = sys.argv[1:]
    net_url = None
    if "--net" in args:
        i = args.index("--net")
        net_url = args[i + 1]
        del args[i:i + 2]
    seconds = float(args[0]) if args else 0
    set_name("demo-load.py")
    load = Load(seconds, net_url)
    signal.signal(signal.SIGTERM, lambda *_: load.stop.set())
    signal.signal(signal.SIGINT, lambda *_: load.stop.set())
    load.run()


if __name__ == "__main__":
    main()
