"""OBS WebSocket control for a dry microphone and w-okada's converted output."""

import base64
import hashlib
import json
import time
from uuid import uuid4

from websockets.sync.client import connect


def authentication(password, salt, challenge):
    def digest(value):
        return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()
    return digest(digest(password + salt) + challenge)


class OBS:
    def __init__(self, port, password):
        self.port = port
        self.password = password
        self.socket = None

    def __enter__(self):
        self.socket = connect(f"ws://127.0.0.1:{self.port}", open_timeout=3, close_timeout=1)
        try:
            hello = json.loads(self.socket.recv(timeout=3))
            if hello["op"] != 0:
                raise RuntimeError("OBS did not send a WebSocket v5 greeting.")
            settings = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = hello["d"].get("authentication")
            if auth:
                settings["authentication"] = authentication(self.password, auth["salt"], auth["challenge"])
            self.socket.send(json.dumps({"op": 1, "d": settings}))
            if json.loads(self.socket.recv(timeout=3))["op"] != 2:
                raise RuntimeError("OBS WebSocket identification failed.")
            return self
        except Exception:
            self.socket.close()
            raise

    def __exit__(self, *args):
        self.socket.close()

    def request(self, kind, **data):
        identifier = uuid4().hex
        self.socket.send(json.dumps({"op": 6, "d": {
            "requestType": kind, "requestId": identifier, "requestData": data,
        }}))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            reply = json.loads(self.socket.recv(timeout=max(0, deadline - time.monotonic())))
            if reply["op"] == 7 and reply["d"]["requestId"] == identifier:
                result = reply["d"]
                if not result["requestStatus"]["result"]:
                    detail = result["requestStatus"].get("comment", kind)
                    raise RuntimeError(f"OBS: {detail}")
                return result.get("responseData", {})
        raise TimeoutError("OBS did not answer.")

    def set_muted(self, source, muted):
        self.request("SetInputMute", inputName=source, inputMuted=muted)

    def is_muted(self, source):
        return self.request("GetInputMute", inputName=source)["inputMuted"]


def run_effect(port, password, dry, wet, seconds, stop, status):
    """Restore the original mute states even after a partial switch or OBS reconnect."""
    previous = None
    failure = None
    try:
        with OBS(port, password) as obs:
            if stop.is_set():
                return
            mute_states = {name: obs.is_muted(name) for name in (dry, wet)}
            if not mute_states[wet] or mute_states[dry]:
                raise RuntimeError("In OBS, leave the original mic unmuted and mute the w-okada input before triggering.")
            previous = mute_states
            obs.set_muted(dry, True)
            obs.set_muted(wet, False)
            status("Voice active")
            stop.wait(seconds)
    except Exception as exc:
        failure = exc
    finally:
        if previous is not None:
            for attempt in range(3):
                try:
                    with OBS(port, password) as obs:
                        obs.set_muted(wet, previous[wet])
                        obs.set_muted(dry, previous[dry])
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise RuntimeError("OBS could not restore the mic. Mute the w-okada input and unmute the original mic in OBS.") from exc
                    time.sleep(0.5)
    if failure:
        raise failure
