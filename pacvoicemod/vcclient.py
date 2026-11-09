"""Start a local VCClient installation on demand."""

import os
from pathlib import Path
import subprocess
from threading import Event, Lock
import time

from requests import Timeout

from .engine import EngineUnavailable

SERVER = "MMVCServerSIO.exe"
HTTP_LAUNCHERS = ("start_http.bat", "http_start.bat")


class StartupError(RuntimeError):
    def __init__(self, message, **details):
        self.message = message
        self.details = details
        super().__init__(message.format(**details))


def launch_command(folder, port):
    server = folder / SERVER
    if server.is_file():
        return [str(server), "--host", "127.0.0.1", "-p", str(port)]
    for name in HTTP_LAUNCHERS:
        if (folder / name).is_file():
            shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
            return [str(shell), "/d", "/c", name]
    raise StartupError("Select the extracted VCClient folder containing its HTTP launcher.")


class LocalVCClient:
    def __init__(self, log):
        self.log = log
        self.process = None
        self.lock = Lock()
        self.closed = Event()

    def ensure_running(self, engine, folder, port, starting):
        try:
            engine.request("GET", "/info", timeout=1)
            return
        except (EngineUnavailable, Timeout) as exc:
            if folder is None:
                raise EngineUnavailable(engine.base) from exc

        with self.lock:
            if self.closed.is_set():
                return
            if self.process is None or self.process.poll() is not None:
                command = launch_command(folder, port)
                self.log.parent.mkdir(parents=True, exist_ok=True)
                with self.log.open("wb") as log:
                    self.process = subprocess.Popen(
                        command, cwd=folder, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
            process = self.process
        starting()
        deadline = time.monotonic() + 180
        while not self.closed.is_set():
            try:
                engine.request("GET", "/info", timeout=3)
                return
            except (EngineUnavailable, Timeout):
                if process.poll() is not None:
                    raise StartupError("VCClient exited during startup. See {log}.", log=self.log)
                if time.monotonic() >= deadline:
                    raise StartupError("VCClient did not respond at {address}. See {log}.",
                                       address=engine.base, log=self.log)
                self.closed.wait(0.5)

    def close(self):
        self.closed.set()
        with self.lock:
            process = self.process
        if process is not None and process.poll() is None:
            try:
                subprocess.run(["taskkill.exe", "/pid", str(process.pid), "/t", "/f"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=4, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                if process.poll() is None:
                    process.terminate()
