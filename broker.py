#!/usr/bin/env python3
"""Root-privileged broker for mudrun-headless.

Launched once via pkexec by the (unprivileged) tray. Listens on a Unix
domain socket, verifies the connecting peer's UID via SO_PEERCRED before
accepting any command, and supervises mudrun-headless as a direct child
process (no pattern-matching pkill needed). Exits — and stops mudrun-headless
— as soon as the tray disconnects or sends "quit", so nothing outlives the
tray session.

Usage: mudfish-broker.py <socket_path> <log_path> <allowed_uid> [mudfish_home]

mudfish_home, if given, is used as-is instead of auto-detecting the newest
install under /opt/mudfish — this is how tray.py forwards its own
(possibly --mudfish-home-overridden) choice, so both processes agree on
which Mudfish install to run.
"""
import asyncio
import os
import signal
import socket
import struct
import sys
import time

import mudfish_home


def find_binary():
    if not mudfish_home.MUDFISH_BIN_DIR:
        return None
    path = os.path.join(mudfish_home.MUDFISH_BIN_DIR, "mudrun-headless")
    return path if os.path.isfile(path) else None


class Broker:
    def __init__(self, socket_path, log_path, allowed_uid):
        self.socket_path = socket_path
        self.log_path = log_path
        self.allowed_uid = allowed_uid
        self.proc = None
        self.writer = None
        self.shutdown_event = asyncio.Event()

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, file=sys.stderr, flush=True)
        try:
            with open(self.log_path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def is_running(self):
        return self.proc is not None and self.proc.returncode is None

    async def _push_state(self):
        if not self.writer:
            return
        state = "running" if self.is_running() else "stopped"
        try:
            self.writer.write(f"state: {state}\n".encode())
            await self.writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _watch_proc(self, proc):
        await proc.wait()
        if self.proc is proc:
            self.log(f"mudrun-headless exited (code {proc.returncode})")
            self.proc = None
            await self._push_state()

    async def start_mudfish(self):
        if self.is_running():
            return
        binary = find_binary()
        if not binary:
            self.log("ERROR: mudrun-headless binary not found under /opt/mudfish")
            if self.writer:
                self.writer.write(b"error: mudrun-headless binary not found\n")
                await self.writer.drain()
            return
        self.log(f"starting {binary}")
        self.proc = await asyncio.create_subprocess_exec(binary)
        asyncio.ensure_future(self._watch_proc(self.proc))

    async def stop_mudfish(self):
        if not self.is_running():
            return
        self.log("stopping mudrun-headless")
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.log("mudrun-headless did not exit in time, killing")
            self.proc.kill()
            await self.proc.wait()

    async def handle_client(self, reader, writer):
        sock = writer.get_extra_info("socket")
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        if uid != self.allowed_uid:
            self.log(f"rejected connection from uid {uid} (expected {self.allowed_uid})")
            writer.write(b"error: unauthorized\n")
            await writer.drain()
            writer.close()
            return

        if self.writer is not None:
            self.log("rejected second concurrent client")
            writer.write(b"error: already have a client\n")
            await writer.drain()
            writer.close()
            return

        self.log(f"client connected (uid {uid})")
        self.writer = writer
        await self._push_state()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    self.log("client disconnected without quit")
                    break
                cmd = line.decode(errors="replace").strip()
                if cmd == "start":
                    await self.start_mudfish()
                elif cmd == "stop":
                    await self.stop_mudfish()
                elif cmd == "status":
                    pass  # falls through to the push below
                elif cmd == "quit":
                    self.log("client requested quit")
                    break
                else:
                    self.log(f"unknown command: {cmd!r}")
                await self._push_state()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.writer = None
            await self.stop_mudfish()
            self.shutdown_event.set()

    async def _shutdown_on_signal(self, sig_name):
        self.log(f"received {sig_name}, stopping mudfish before exiting")
        await self.stop_mudfish()
        self.shutdown_event.set()

    async def run(self):
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        old_umask = os.umask(0o177)
        server = await asyncio.start_unix_server(self.handle_client, path=self.socket_path)
        os.umask(old_umask)
        # The socket is created owned by root (mode 0600), which would lock out
        # the very unprivileged client it exists to serve. Hand ownership to
        # that client's uid so only they (and root) can connect.
        os.chown(self.socket_path, self.allowed_uid, -1)
        os.chmod(self.socket_path, 0o600)
        self.log(f"listening on {self.socket_path}, allowed_uid={self.allowed_uid}")

        loop = asyncio.get_running_loop()
        for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
            loop.add_signal_handler(sig, lambda name=name: asyncio.ensure_future(self._shutdown_on_signal(name)))

        await self.shutdown_event.wait()
        self.log("shutting down")
        # Plain server.close() only stops accepting *new* connections; on this
        # asyncio version, waiting on it (e.g. via "async with server") would
        # block until already-connected clients disconnect on their own. Our
        # only client is the tray, which stays connected for the broker's
        # whole life, so that would deadlock here instead of exiting. We're
        # about to exit the process anyway, so don't wait for it.
        server.close()
        if self.writer:
            self.writer.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


def main():
    socket_path, log_path, allowed_uid = sys.argv[1], sys.argv[2], int(sys.argv[3])
    home_override = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
    mudfish_home.configure(home_override)
    broker = Broker(socket_path, log_path, allowed_uid)
    try:
        asyncio.run(broker.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
