import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import MagicMock, patch

from websockets.sync.server import serve

from pacvoicemod.companion import paid_trigger_matches
from pacvoicemod.obs import authentication, run_effect
from pacvoicemod.twitch import BITS, POINTS
from pacvoicemod.vcclient import start_desktop


class CompanionTests(unittest.TestCase):
    def test_companion_opens_the_cached_desktop_without_downloading(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "runtime"
            folder = destination / "w-okada"
            folder.mkdir(parents=True)
            (folder / "start_http.bat").touch()
            with (patch("pacvoicemod.vcclient.requests.get") as download,
                  patch("pacvoicemod.vcclient.subprocess.CREATE_NO_WINDOW", 0, create=True),
                  patch("pacvoicemod.vcclient.subprocess.Popen") as launch):
                process = start_desktop(destination, Path(temporary) / "vcclient.log",
                                        Event(), MagicMock())
            self.assertIs(process, launch.return_value)
            self.assertEqual(launch.call_args.kwargs["cwd"], folder)
            self.assertEqual(launch.call_args.args[0][-1], "start_http.bat")
            download.assert_not_called()

    def start_obs(self, fail_switch=False):
        states = {"Dry": False, "Wet": True}
        actions = []

        def handle(socket):
            socket.send(json.dumps({"op": 0, "d": {
                "rpcVersion": 1, "authentication": {"salt": "salt", "challenge": "challenge"},
            }}))
            identify = json.loads(socket.recv())
            if identify["d"]["authentication"] != authentication("password", "salt", "challenge"):
                socket.close()
                return
            socket.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
            for message in socket:
                request = json.loads(message)["d"]
                kind = request["requestType"]
                data = request["requestData"]
                source = data["inputName"]
                result = True
                response = {}
                if kind == "GetInputMute":
                    response["inputMuted"] = states[source]
                elif kind == "SetInputMute":
                    value = data["inputMuted"]
                    if fail_switch and source == "Wet" and not value:
                        result = False
                    else:
                        states[source] = value
                        actions.append((source, value))
                socket.send(json.dumps({"op": 7, "d": {
                    "requestId": request["requestId"], "requestType": kind,
                    "requestStatus": {"result": result, "comment": "test failure"},
                    "responseData": response,
                }}))

        server = serve(handle, "127.0.0.1", 0)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        return server.socket.getsockname()[1], states, actions

    def test_authenticated_switch_and_restore_over_websocket(self):
        port, states, actions = self.start_obs()
        run_effect(port, "password", "Dry", "Wet", 0, Event(), lambda _: None)
        self.assertEqual(actions, [("Dry", True), ("Wet", False), ("Wet", True), ("Dry", False)])
        self.assertEqual(states, {"Dry": False, "Wet": True})

    def test_partial_switch_restores_original_microphone(self):
        port, states, actions = self.start_obs(fail_switch=True)
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            run_effect(port, "password", "Dry", "Wet", 0, Event(), lambda _: None)
        self.assertEqual(actions, [("Dry", True), ("Wet", True), ("Dry", False)])
        self.assertEqual(states, {"Dry": False, "Wet": True})

    def test_bits_and_rewards_require_configured_match(self):
        self.assertTrue(paid_trigger_matches(BITS, {"type": "cheer", "bits": 100}, 100, ""))
        self.assertFalse(paid_trigger_matches(BITS, {"type": "cheer", "bits": 99}, 100, ""))
        self.assertFalse(paid_trigger_matches(BITS, {"type": "power_up", "bits": 100}, 100, ""))
        event = {"reward": {"id": "a123", "title": "Robot Voice"}}
        self.assertTrue(paid_trigger_matches(POINTS, event, 0, "a123"))
        self.assertTrue(paid_trigger_matches(POINTS, event, 0, "robot voice"))
        self.assertFalse(paid_trigger_matches(POINTS, event, 0, ""))
