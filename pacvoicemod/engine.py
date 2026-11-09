import base64
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import numpy as np
import requests

RATE = 48000
FRAMES = 5760
LIMIT = 512 * 1024 * 1024


def model_files(weights, index, directory):
    """Return files from a plain download or extract only the model entries."""
    if weights.suffix.lower() == ".pth":
        files = [(weights, "rvcModel")]
        if index:
            if index.suffix.lower() != ".index":
                raise ValueError("Select a .index file.")
            files.append((index, "rvcIndex"))
        for path, _ in files:
            if not 0 < path.stat().st_size <= LIMIT:
                raise ValueError("Model files must be nonempty and at most 512 MB.")
        return files

    if weights.suffix.lower() != ".zip":
        raise ValueError("Select an RVC .pth or .zip file.")
    if weights.stat().st_size > LIMIT:
        raise ValueError("ZIP exceeds 512 MB.")
    with zipfile.ZipFile(weights) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or sum(entry.file_size for entry in entries) > LIMIT:
            raise ValueError("ZIP exceeds import limits.")
        files = []
        for extension, kind in ((".pth", "rvcModel"), (".index", "rvcIndex")):
            matches = [entry for entry in entries if not entry.is_dir()
                       and PurePosixPath(entry.filename).suffix.lower() == extension]
            if len(matches) > 1:
                raise ValueError(f"Multiple {extension} files. Extract and select the files manually.")
            if not matches:
                if extension == ".pth":
                    raise ValueError("No .pth checkpoint in ZIP.")
                continue
            # Use fixed local names and never extract an archive path.
            target = directory / ("voice" + extension)
            with archive.open(matches[0]) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            if not target.stat().st_size:
                raise ValueError(f"Empty {extension} file.")
            files.append((target, kind))
        return files


class Engine:
    def __init__(self, port, slot):
        self.base = f"http://127.0.0.1:{port}"
        self.slot = slot
        self.http = requests.Session()
        self.http.trust_env = False

    def request(self, method, path, timeout=5, **kwargs):
        response = self.http.request(method, self.base + path, timeout=timeout,
                                     allow_redirects=False, **kwargs)
        response.raise_for_status()
        if response.is_redirect:
            raise RuntimeError("VCClient must run over loopback HTTP.")
        data = response.json()
        if not isinstance(data, dict) or data.get("status") in {"ERROR", "ng"}:
            raise RuntimeError(f"VCClient rejected {path}. Check its console.")
        return data

    def setting(self, key, value):
        return self.request("POST", "/update_settings", timeout=45,
                            data={"key": key, "val": value})

    def upload(self, path, kind):
        name = uuid.uuid4().hex + path.suffix.lower()
        with path.open("rb") as source:
            count = 0
            while chunk := source.read(4 * 1024 * 1024):
                chunk_name = f"{name}_{count}"
                self.request("POST", "/upload_file", data={"filename": chunk_name},
                             files={"file": (chunk_name, chunk)})
                count += 1
        self.request("POST", "/concat_uploaded_file", timeout=45,
                     data={"filename": name, "filenameChunkNum": count})
        return {"name": name, "kind": kind, "dir": ""}

    def prepare(self, weights=None, index=None):
        info = self.request("GET", "/info")
        if not info.get("gpus"):
            raise RuntimeError("VCClient reports no CUDA GPU. Start the Windows CUDA build.")
        self.setting("enableServerAudio", 0)
        if weights:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                sources = model_files(weights, index, directory)
                checkpoint = directory / "checkpoint.pth"
                shutil.copyfile(sources[0][0], checkpoint)
                validation = subprocess.run(
                    [sys.executable, "-m", "pacvoicemod.validate_model", str(checkpoint)],
                    capture_output=True, text=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if validation.returncode:
                    raise ValueError(validation.stderr.strip() or "RVC checkpoint validation failed.")
                files = [self.upload(checkpoint, "rvcModel")]
                files.extend(self.upload(path, kind) for path, kind in sources[1:])
                params = {"voiceChangerType": "RVC", "slot": self.slot,
                          "isSampleMode": False, "sampleId": "", "files": files, "params": {}}
                self.request("POST", "/load_model", timeout=90,
                             data={"slot": self.slot, "isHalf": "false", "params": json.dumps(params)})
        info = self.request("GET", "/info")
        model = next((item for item in info.get("modelSlots", [])
                      if item and item.get("slotIndex") == self.slot
                      and item.get("voiceChangerType") == "RVC"), None)
        if not model:
            raise RuntimeError(f"No RVC model in VCClient slot {self.slot}. Import a voice first.")
        self.setting("modelSlotIndex", self.slot)
        self.setting("gpu", info["gpus"][0]["id"])
        self.setting("inputSampleRate", RATE)
        self.setting("outputSampleRate", RATE)
        self.setting("indexRatio", 0.25 if model.get("indexFile") else 0)
        samples = np.arange(FRAMES)
        probe = (4000 * np.sin(2 * np.pi * 180 * samples / RATE)).astype("<i2")
        self.convert(probe.tobytes(), timeout=30)

    def convert(self, pcm, timeout=1):
        data = self.request("POST", "/test", timeout=timeout,
                            json={"timestamp": 0, "buffer": base64.b64encode(pcm).decode("ascii")})
        result = base64.b64decode(data["changedVoiceBase64"], validate=True)
        if len(result) != len(pcm):
            raise RuntimeError("VCClient returned an unexpected audio block length.")
        return result
