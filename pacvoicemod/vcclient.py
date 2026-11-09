"""Start a local VCClient installation on demand."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from threading import Event, Lock
import time
import zipfile

import requests
from requests import Timeout

from .engine import EngineUnavailable

SERVER = "MMVCServerSIO.exe"
HTTP_LAUNCHERS = ("start_http.bat", "http_start.bat")

BUILD = "vcclient_win_cuda_2.0.65-beta"
BUILD_URL = (
    "https://huggingface.co/wok000/vcclient000/resolve/"
    f"1679ae1848c3cfdd11388626b1733bb782065156/{BUILD}.zip"
)
BUILD_SHA256 = "ba66c89f1be5d373554e084924e18216c5e7447e2a0adc778b8613d755510859"


def find_installation(directory):
    names = {SERVER.casefold(), *HTTP_LAUNCHERS}
    candidates = [path.parent for path in directory.rglob("*")
                  if path.name.casefold() in names and path.is_file()]
    return min(candidates, key=lambda path: (len(path.parts), str(path)), default=None)


class StartupError(RuntimeError):
    def __init__(self, message, **details):
        self.message = message
        self.details = details
        super().__init__(message.format(**details))


def install_vcclient(destination, closed, status):
    existing = find_installation(destination)
    if existing:
        return existing
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, ignore_cleanup_errors=True) as temporary:
        scratch = Path(temporary)
        archive = scratch / "vcclient.zip"
        digest = hashlib.sha256()
        status("Downloading VCClient CUDA (about 3 GB)…")
        with requests.get(BUILD_URL, stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            with archive.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if closed.is_set():
                        return None
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != BUILD_SHA256:
            raise StartupError("VCClient download failed its checksum. Retry to download a fresh copy.")

        status("Extracting VCClient CUDA…")
        staging = scratch / "build"
        staging.mkdir()
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                if closed.is_set():
                    return None
                name = PurePosixPath(entry.filename)
                if name.is_absolute() or ".." in name.parts or ":" in entry.filename or "\\" in entry.filename:
                    raise StartupError("VCClient archive contains an invalid file path.")
                target = staging.joinpath(*name.parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        folder = find_installation(staging)
        if folder is None:
            raise StartupError("VCClient archive does not contain a supported HTTP launcher.")
        try:
            staging.rename(destination)
        except OSError:
            existing = find_installation(destination)
            if existing:
                return existing
            raise
        return destination / folder.relative_to(staging)


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
    def __init__(self, log, destination=None):
        self.log = log
        self.destination = destination
        self.process = None
        self.lock = Lock()
        self.closed = Event()

    def ensure_running(self, engine, folder, port, status):
        try:
            engine.request("GET", "/info", timeout=1)
            return
        except (EngineUnavailable, Timeout) as exc:
            if folder is None:
                if self.destination is None:
                    raise EngineUnavailable(engine.base) from exc
                folder = install_vcclient(self.destination, self.closed, status)
                if folder is None:
                    return

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
        status("Starting VCClient…")
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
