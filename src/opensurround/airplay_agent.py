"""Local AirPlay egress service for cliairplay v0.5.15."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from hashlib import sha256
from ipaddress import IPv4Address
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory
import time

from .airplay import AirPlayDevice, _MAX_DEVICES, _MODES, _PIN, _read, _write

_HEX = re.compile(rb"[0-9a-fA-F]{192}")
_ACK = re.compile(rb"\[STATUS\] started requested_unix_ms=([0-9]{1,16}) at_unix_ms=([0-9]{1,16})(?: |$)")
_PROMPT = re.compile(rb"(?:^(?:pin|pairing code):|(?:enter|input|type) [^\r\n:]{0,60}(?:pin|code)[^\r\n:]{0,60}:)", re.I)
_TXT = {"features", "ft", "flags", "sf", "model", "am", "pk", "pw", "et", "osvers", "srcvers", "cn"}
_PTP = 1 << 41
_PAIRING = (1 << 46) | (1 << 48)


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid AirPlay integer")
    return value


def _device(value) -> AirPlayDevice:
    if not isinstance(value, dict) or set(value) != {"id", "name", "address", "port", "txt"}:
        raise ValueError("Invalid AirPlay device")
    for key in ("id", "name", "address"):
        if not isinstance(value[key], str) or not 0 < len(value[key]) <= 255 or not value[key].isprintable():
            raise ValueError("Invalid AirPlay device text")
    address = IPv4Address(value["address"])
    if address.is_multicast or address.is_unspecified or address == IPv4Address("255.255.255.255"):
        raise ValueError("Invalid AirPlay receiver address")
    txt = value["txt"]
    if not isinstance(txt, dict) or len(txt) > 48:
        raise ValueError("Invalid AirPlay discovery TXT")
    clean = {}
    for key, item in txt.items():
        if not isinstance(item, str) or len(item) > 255:
            raise ValueError("Invalid AirPlay discovery value")
        if key.lower() in _TXT and item:
            if item.startswith("-") or not re.fullmatch(r"[!-~]+", item):
                raise ValueError("Invalid AirPlay discovery value")
            clean[key.lower()] = item
    return AirPlayDevice(value["id"], value["name"], str(address), _integer(value["port"], 1, 65535), clean)


def _mask(value: str) -> int:
    if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]{1,16}|[0-9]{1,20})(?:,0[xX][0-9a-fA-F]{1,8})?", value):
        raise ValueError("Invalid AirPlay discovery mask")
    low, _, high = value.partition(",")
    number = int(low, 16 if low.lower().startswith("0x") else 10)
    if number >= (1 << (32 if high else 64)):
        raise ValueError("Invalid AirPlay discovery mask")
    return number | (int(high, 16) << 32 if high else 0)


def _route(mode, txt, auth, ptp):
    features = _mask(txt.get("features", txt.get("ft", "0")))
    flags = _mask(txt.get("flags", txt.get("sf", "0")))
    if mode == "compat":
        return False, False
    shared = bool(ptp and features & _PTP)
    transient = bool(features & _PAIRING) and not (flags & 0x208 or txt.get("pw", "").lower() == "true")
    native = mode == "native" or (shared and (auth or transient))
    return bool(native), bool(native and shared)


async def _terminate(process):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class _Child:
    """Drain bounded output privately and retain only recognized protocol events."""

    def __init__(self, process, pairing):
        self.process = process
        self.pairing = pairing
        self.ready = asyncio.Event()
        self.replies = asyncio.Queue(maxsize=4)
        self.prompt = asyncio.Event()
        self.credentials = None

    async def scan(self, stream, stdout=False):
        fragment = b""
        discard = False
        while chunk := await stream.read(1024):
            lines = chunk.split(b"\n")
            for index, line in enumerate(lines):
                if not discard:
                    fragment += line
                    if len(fragment) > 4096:
                        discard = True
                    elif index < len(lines) - 1:
                        self.detect(fragment.rstrip(b"\r"), stdout)
                    elif self.pairing and _PROMPT.search(fragment):
                        self.prompt.set()
                if index < len(lines) - 1:
                    fragment = b""
                    discard = False
        if fragment and not discard:
            self.detect(fragment.rstrip(b"\r"), stdout)

    def detect(self, line, stdout):
        if self.pairing:
            if _PROMPT.search(line):
                self.prompt.set()
            if stdout and line.startswith(b"CREDENTIALS: ") and _HEX.fullmatch(line[13:]):
                self.credentials = line[13:]
            return
        if line == b"[STATUS] connected" or b"[PTP] daemon up" in line:
            self.ready.set()
        elif line.startswith((b"[STATUS] started ", b"[STATUS] flushed", b"[STATUS] error ")):
            with suppress(asyncio.QueueFull):
                self.replies.put_nowait(line if not line.startswith(b"[STATUS] error ") else b"error")

    async def wait(self, awaitable, seconds):
        pending = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait((pending, self.exited), timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
            if pending in done and self.exited not in done:
                return pending.result()
            raise RuntimeError("AirPlay subprocess did not respond")
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            elif not pending.cancelled():
                pending.exception()


@asynccontextmanager
async def _child(binary, args, *, pairing=False, stdin=True):
    process = await asyncio.create_subprocess_exec(
        binary, *args, stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, close_fds=True, limit=8192,
    )
    child = _Child(process, pairing)
    readers = [
        asyncio.create_task(child.scan(process.stdout, stdout=True)),
        asyncio.create_task(child.scan(process.stderr)),
    ]
    child.exited = asyncio.create_task(process.wait())
    try:
        yield child
    finally:
        if process.stdin and not process.stdin.is_closing():
            process.stdin.transport.abort()
        await _terminate(process)
        await asyncio.gather(child.exited, *readers, return_exceptions=True)


class _Renderer:
    def __init__(self, child, fd, rate, channels, output_rate):
        self.child, self.fd = child, fd
        # FLUSH can drain the kernel pipe once all Python-buffered PCM has
        # reached it. A zero watermark makes drain wait for that boundary.
        child.process.stdin.transport.set_write_buffer_limits(high=0)
        self.rate, self.channels, self.output_rate = rate, channels, output_rate
        self.epoch = self.stamp = -1
        self.origin = self.anchor_frame = self.frames = 0
        self.late = False
        self.resampler = None

    async def command(self, data):
        # One short atomic FIFO write, with a persistent nonblocking writer.
        async with asyncio.timeout(2):
            while True:
                try:
                    os.write(self.fd, data)
                    return
                except BlockingIOError:
                    await asyncio.sleep(0.01)

    async def feed(self, pcm):
        if self.channels == 1 or self.rate != self.output_rate:
            import numpy as np

            samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.channels)
            if self.channels == 1:
                samples = np.repeat(samples, 2, axis=1)
            if self.rate != self.output_rate:
                import av

                if self.resampler is None:
                    self.resampler = av.AudioResampler(format="s16", layout="stereo", rate=self.output_rate)
                frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="stereo")
                frame.sample_rate = self.rate
                pcm = b"".join(frame.to_ndarray().astype("<i2", copy=False).tobytes()
                               for frame in self.resampler.resample(frame))
            else:
                pcm = samples.tobytes()
        if pcm:
            writer = self.child.process.stdin
            writer.write(pcm)
            await self.child.wait(writer.drain(), 2)

    async def push(self, epoch, stamp, pcm):
        if epoch < self.epoch or (epoch == self.epoch and stamp <= self.stamp):
            return "late" if self.late else "playing"
        if epoch != self.epoch:
            if self.epoch >= 0:
                await self.child.wait(self.child.process.stdin.drain(), 2)
                await self.command(b"ACTION=FLUSH\n")
                flushed = await self.child.wait(self.child.replies.get(), 2)
                if not flushed.startswith(b"[STATUS] flushed"):
                    raise RuntimeError("AirPlay flush failed")
            self.resampler = None
            self.frames = 0
            requested = (stamp + 999) // 1000
            await self.command(f"START_JOIN=1\nSTART_UNIX_MS={requested}\nACTION=START\n".encode())
            line = await self.child.wait(self.child.replies.get(), 8)
            ack = _ACK.match(line)
            if not ack or int(ack[1]) != requested or int(ack[2]) < requested:
                raise RuntimeError("AirPlay start acknowledgement invalid")
            self.origin = stamp
            advanced_us = int(ack[2]) * 1000 - stamp
            self.anchor_frame = (advanced_us * self.rate + 999_999) // 1_000_000
            self.late = int(ack[2]) > requested
            self.epoch = epoch
        self.stamp = stamp
        stride = self.channels * 2
        # Map chunks to the original sample grid and keep the first sample
        # on or after the acknowledged start, even between two sample times.
        delta = (stamp - self.origin) * self.rate
        offset = (delta + 500_000) // 1_000_000 - self.anchor_frame
        skip = max(0, self.frames - offset)
        pcm = pcm[skip * stride:]
        if not pcm:
            return "late"
        gap = max(0, offset - self.frames)
        if gap > self.rate * 2:
            raise RuntimeError("AirPlay timeline discontinuity")
        while gap:
            step = min(gap, 4096)
            await self.feed(bytes(step * stride))
            self.frames += step
            gap -= step
        await self.feed(pcm)
        self.frames += len(pcm) // stride
        return "late" if self.late else "playing"


class AirPlayAgent:
    def __init__(self, socket_path, binary, *, state_dir=None, ptp=False):
        self.path = Path(socket_path)
        self.binary = binary
        self.state = Path(state_dir) if state_dir else self.path.with_name(self.path.name + ".state")
        self.ptp = ptp
        self.clock = None
        self.clients = set()
        self.devices = {}

    def _stored(self, name, pattern):
        try:
            fd = os.open(self.state / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                raise ValueError("AirPlay state file permissions invalid")
            value = file.read(193)
        if not pattern.fullmatch(value):
            raise ValueError("AirPlay state file invalid")
        return value

    def _store(self, name, value, *, create=False):
        with NamedTemporaryFile(dir=self.state, delete=False) as file:
            try:
                file.write(value)
                file.flush()
                os.fsync(file.fileno())
                if create:
                    with suppress(FileExistsError):
                        os.link(file.name, self.state / name)
                else:
                    os.replace(file.name, self.state / name)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(file.name)

    @asynccontextmanager
    async def _running(self):
        self.state.mkdir(mode=0o700, exist_ok=True)
        info = self.state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("AirPlay state directory must be private")
        dacp_pattern = re.compile(rb"[0-9A-F]{16}")
        self.dacp = self._stored("dacp", dacp_pattern)
        if self.dacp is None:
            self._store("dacp", secrets.token_hex(8).upper().encode(), create=True)
            self.dacp = self._stored("dacp", dacp_pattern)
        async with AsyncExitStack() as stack:
            if self.ptp:
                clock = await stack.enter_async_context(
                    _child(self.binary, ["--ptp-daemon", "--dacp", self.dacp.decode()], stdin=False)
                )
                await clock.wait(clock.ready.wait(), 3)
                self.clock = clock
            try:
                yield
            finally:
                await self._close_clients()
                self.clock = None

    async def _close_clients(self):
        clients = tuple(self.clients)
        for task in clients:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)

    @asynccontextmanager
    async def listen(self):
        # Bind before creating the DACP identity so duplicate starts on the
        # same pathname cannot replace it. Never remove an existing socket.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            mask = os.umask(0o177)
            try:
                sock.bind(str(self.path))
            finally:
                os.umask(mask)
            identity = self.path.lstat()
            try:
                async with self._running():
                    server = await asyncio.start_unix_server(self._client, sock=sock)
                    try:
                        yield server
                    finally:
                        server.close()
                await server.wait_closed()
            finally:
                with suppress(FileNotFoundError):
                    if os.path.samestat(identity, self.path.lstat()):
                        self.path.unlink()

    async def _client(self, reader, writer):
        task = asyncio.current_task()
        if len(self.clients) >= 2 * _MAX_DEVICES:
            writer.close()
            return
        self.clients.add(task)
        key = None
        try:
            async with asyncio.timeout(5):
                message, pcm = await _read(reader)
            if pcm or message.get("op") not in ("prepare", "pair"):
                raise ValueError("Invalid AirPlay request")
            device = _device(message.get("device"))
            key = (message["op"], device.id)
            previous = self.devices.get(key)
            self.devices[key] = task
            if previous:
                if not previous.cancelling():
                    previous.cancel()
                try:
                    await asyncio.wait((previous,))
                except asyncio.CancelledError:
                    await asyncio.wait((previous,))
                    raise
            filename = sha256(device.id.encode()).hexdigest()
            if message["op"] == "pair":
                await self._pair(reader, writer, device, filename)
            else:
                await self._stream(reader, writer, device, filename, message)
        except EOFError:
            pass
        except (OSError, ValueError, RuntimeError):
            with suppress(OSError):
                async with asyncio.timeout(1):
                    await _write(writer, {"status": "error"})
        finally:
            if self.devices.get(key) is task:
                del self.devices[key]
            self.clients.discard(task)
            writer.close()

    async def _pair(self, reader, writer, device, filename):
        args = ["--pair-setup", "--port", str(device.port), "--dacp", self.dacp.decode(), device.address]
        async with _child(self.binary, args, pairing=True) as child:
            await child.wait(child.prompt.wait(), 10)
            await _write(writer, {"status": "pin"})
            message, pcm = await child.wait(_read(reader), 60)
            pin = message.get("pin")
            if pcm or message.get("op") != "pin" or not isinstance(pin, str) or not _PIN.fullmatch(pin):
                raise ValueError("Invalid AirPlay PIN")
            child.process.stdin.write(pin.encode() + b"\n")
            await child.process.stdin.drain()
            child.process.stdin.close()
            if await asyncio.wait_for(asyncio.shield(child.exited), 10) != 0:
                raise RuntimeError("AirPlay pairing rejected")
        if child.credentials is None:
            raise RuntimeError("AirPlay pairing produced no credentials")
        self._store(filename, child.credentials.lower())
        await _write(writer, {"status": "paired"})

    async def _stream(self, reader, writer, device, filename, message):
        rate = _integer(message.get("rate"), 8000, 192000)
        channels = _integer(message.get("channels"), 1, 2)
        mode = message.get("mode")
        if not isinstance(mode, str) or mode not in _MODES:
            raise ValueError("Invalid AirPlay mode")
        auth = self._stored(filename, _HEX)
        native, shared = _route(mode, device.txt, auth, self.ptp)
        if shared and (not self.clock or self.clock.exited.done()):
            raise RuntimeError("AirPlay PTP daemon unavailable")
        output_rate = rate if native and rate in (44100, 48000) else 44100
        args = ["--protocol", "airplay2" if native else "airplay2-compat",
                "--port", str(device.port), "--dacp", self.dacp.decode(), "--timing", "ptp" if shared else "ntp",
                "--samplerate", str(output_rate), "--bitdepth", "16", "--channels", "2"]
        if device.txt:
            args += ["--txt", " ".join(f"{key}={value}" for key, value in device.txt.items())]
        if native:
            args += ["--auth", auth.decode()] if auth else ["--ap2-native"]
        else:
            for key in ("et", "cn", "pk", "pw", "am"):
                if key in device.txt:
                    args += [f"--{key}", device.txt[key]]
        if shared:
            args += ["--ptp-shared"]
        with TemporaryDirectory(prefix="cmd-", dir=self.state) as directory:
            fifo = Path(directory) / "commands"
            os.mkfifo(fifo, mode=0o600)
            args += ["--cmdpipe", str(fifo), device.address]
            # Hold both ends locally so a child opening its read end before
            # reporting connected cannot deadlock. The descriptor stays here.
            fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                async with _child(self.binary, args) as child:
                    await child.wait(child.ready.wait(), 10)
                    renderer = _Renderer(child, fd, rate, channels, output_rate)
                    await _write(writer, {"status": "ready"})
                    while True:
                        message, pcm = await child.wait(_read(reader), None)
                        if message.get("op") != "pcm" or not pcm or len(pcm) % (channels * 2):
                            raise ValueError("Invalid AirPlay PCM frame")
                        epoch = _integer(message.get("epoch"), 0, 2**63 - 1)
                        now = time.time_ns() // 1000
                        stamp = _integer(message.get("at"), 1, now + 60_000_000)
                        status = "late" if stamp < now - 10_000_000 else await renderer.push(epoch, stamp, pcm)
                        async with asyncio.timeout(2):
                            await _write(writer, {"status": status})
            finally:
                os.close(fd)


async def _main(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    agent = AirPlayAgent(args.socket, args.binary, state_dir=args.state_dir, ptp=args.ptp)
    async with agent.listen():
        await stop.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--ptp", action="store_true")
    try:
        asyncio.run(_main(parser.parse_args()))
    except (OSError, ValueError, RuntimeError):
        parser.exit(1, "AirPlay agent startup failed\n")


if __name__ == "__main__":
    main()
