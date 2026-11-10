import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import numpy as np
import requests

try:
    from pacvoicemod import audio as audio_module
except OSError:
    # The tests exercise the callback without opening a hardware stream.
    with patch.dict(sys.modules, {"sounddevice": MagicMock()}):
        from pacvoicemod import audio as audio_module
from pacvoicemod.engine import Engine, EngineUnavailable, FRAMES, model_files
from pacvoicemod.twitch import is_command
from pacvoicemod.vcclient import LocalVCClient, StartupError, install_vcclient, launch_command
from threading import Event


class ModelImportTests(unittest.TestCase):
    def test_nested_zip_import_uses_fixed_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("folder/export.pth", b"checkpoint")
                archive.writestr("folder/added.index", b"index")
            files = model_files(source, None, root)
            self.assertEqual([(p.name, kind) for p, kind in files],
                             [("voice.pth", "rvcModel"), ("voice.index", "rvcIndex")])
            self.assertEqual(files[0][0].read_bytes(), b"checkpoint")

    def test_ambiguous_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("a.pth", b"a")
                archive.writestr("b.pth", b"b")
            with self.assertRaisesRegex(ValueError, "Multiple"):
                model_files(source, None, root)

    def test_engine_reports_connection_failure_with_the_configured_address(self):
        engine = Engine(18889, 90)
        with patch.object(engine.http, "request", side_effect=requests.ConnectionError):
            with self.assertRaisesRegex(EngineUnavailable, "127.0.0.1:18889"):
                engine.prepare()

    def test_loading_saved_voice_reports_gpu_load_and_warmup(self):
        engine = Engine(18888, 90)
        info = {"gpus": [{"id": 0}], "modelSlots": [{"slotIndex": 90, "voiceChangerType": "RVC"}]}
        status = MagicMock()
        with (patch.object(engine, "request", return_value=info),
              patch.object(engine, "convert") as convert):
            engine.prepare(status=status)
        self.assertEqual([call.args[0] for call in status.call_args_list], [
            "Checking the CUDA engine…", "Loading the voice on the GPU…",
            "Warming up voice conversion…",
        ])
        convert.assert_called_once()

    def test_engine_rejects_one_sample_error_response(self):
        engine = Engine(18888, 90)
        with patch.object(engine, "request", return_value={"changedVoiceBase64": "AAA="}):
            with self.assertRaisesRegex(RuntimeError, "block length"):
                engine.convert(bytes(FRAMES * 2))


class VCClientStartupTests(unittest.TestCase):
    def test_download_is_verified_extracted_and_reused(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("nested/MMVCServerSIO.exe", b"example")
        payload = archive.getvalue()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "runtime" / "build"
            with (patch("pacvoicemod.vcclient.BUILD_SHA256", hashlib.sha256(payload).hexdigest()),
                  patch("pacvoicemod.vcclient.requests.get") as download):
                response = download.return_value.__enter__.return_value
                response.headers = {"Content-Length": str(len(payload))}
                response.iter_content.return_value = [payload]
                status = MagicMock()
                folder = install_vcclient(destination, Event(), status)
                status.assert_any_call("Downloading VCClient CUDA (about 3 GB)…", len(payload), len(payload))
                status.assert_any_call("Extracting VCClient CUDA…", len(b"example"), len(b"example"))
                self.assertEqual(folder, destination / "nested")
                self.assertEqual((folder / "MMVCServerSIO.exe").read_bytes(), b"example")
                self.assertEqual(install_vcclient(destination, Event(), MagicMock()), folder)
                download.assert_called_once()

    def test_invalid_download_is_not_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "build"
            with patch("pacvoicemod.vcclient.requests.get") as download:
                download.return_value.__enter__.return_value.iter_content.return_value = [b"bad"]
                with self.assertRaisesRegex(StartupError, "checksum"):
                    install_vcclient(destination, Event(), MagicMock())
            self.assertFalse(destination.exists())

    def test_existing_server_is_reused(self):
        engine = MagicMock()
        with patch("pacvoicemod.vcclient.subprocess.Popen") as launch:
            LocalVCClient(Path("unused.log")).ensure_running(engine, None, 18888, MagicMock())
        launch.assert_not_called()

    def test_launches_server_on_configured_port_and_waits_for_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            server = folder / "MMVCServerSIO.exe"
            server.touch()
            log = folder / "vcclient.log"
            engine = MagicMock()
            unavailable = EngineUnavailable("http://127.0.0.1:18889")
            engine.request.side_effect = [unavailable, unavailable, {"gpus": []}]
            started = MagicMock()
            manager = LocalVCClient(log)
            with (patch("pacvoicemod.vcclient.subprocess.CREATE_NO_WINDOW", 0, create=True),
                  patch("pacvoicemod.vcclient.subprocess.Popen") as launch,
                  patch.object(manager.closed, "wait")):
                launch.return_value.poll.return_value = None
                manager.ensure_running(engine, folder, 18889, started)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0],
                             [str(server), "--host", "127.0.0.1", "-p", "18889"])
            self.assertEqual(launch.call_args.kwargs["cwd"], folder)
            self.assertEqual(engine.request.call_count, 3)
            started.assert_called_once_with("Starting VCClient…")

    def test_startup_keeps_waiting_when_vcclient_is_still_writing_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "MMVCServerSIO.exe").touch()
            log = folder / "vcclient.log"
            manager = LocalVCClient(log)
            engine = MagicMock()
            unavailable = EngineUnavailable("http://127.0.0.1:18888")
            calls = iter((False, False, False, True))

            def probe(*args, **kwargs):
                if next(calls):
                    return {}
                if log.exists():
                    with log.open("a") as output:
                        output.write("Downloading module\n")
                raise unavailable

            engine.request.side_effect = probe
            status = MagicMock()
            with (patch("pacvoicemod.vcclient.subprocess.CREATE_NO_WINDOW", 0, create=True),
                  patch("pacvoicemod.vcclient.subprocess.Popen") as launch,
                  patch("pacvoicemod.vcclient.time.monotonic", side_effect=[0, 290, 580]),
                  patch.object(manager.closed, "wait")):
                launch.return_value.poll.return_value = None
                manager.ensure_running(engine, folder, 18888, status)
            self.assertEqual(engine.request.call_count, 4)
            status.assert_any_call("VCClient is still setting up its modules and samples…")

    def test_batch_launcher_runs_from_its_own_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "start_http.bat").touch()
            self.assertEqual(launch_command(folder, 18888)[-3:], ["/d", "/c", "start_http.bat"])


class AudioTests(unittest.TestCase):
    def setUp(self):
        devices = patch.object(audio_module.sd, "query_devices", return_value={"max_output_channels": 2})
        stream = patch.object(audio_module.sd, "Stream")
        with devices, stream:
            self.audio = audio_module.Audio(MagicMock(), 1, 2)
        self.audio.closed.set()
        self.audio.worker.join(1)
        self.audio.closed.clear()
        self.addCleanup(self.audio.close)
        self.output = np.empty((FRAMES, 2), dtype=np.int16)

    def render(self, sample):
        source = np.full((FRAMES, 1), sample, dtype=np.int16)
        self.audio.callback(source, self.output, FRAMES, None, False)
        return int(self.output[0, 0])

    def test_timer_starts_on_wet_output_and_expires_to_dry(self):
        self.audio.activate(20)
        for _ in range(4):
            self.render(100)
        self.assertIsNone(self.audio.ends)
        self.audio.wet[0] = np.full(FRAMES, 200, dtype=np.int16)
        self.assertEqual(self.render(100), 200)
        self.assertGreater(self.audio.ends, time.monotonic())
        self.audio.ends = time.monotonic() - 1
        self.audio.wet[1] = np.full(FRAMES, 200, dtype=np.int16)
        self.assertEqual(self.render(100), 100)

    def test_local_recording_opens_only_the_microphone(self):
        with (patch.object(audio_module.sd, "InputStream") as input_stream,
              patch.object(audio_module.sd, "Stream") as duplex_stream):
            engine = MagicMock()
            engine.convert.side_effect = lambda pcm: pcm
            local = audio_module.Audio(engine, 5, None)
            try:
                self.assertEqual(input_stream.call_args.kwargs["device"], 5)
                duplex_stream.assert_not_called()
                self.assertTrue(local.start_sample())
                source = np.full((FRAMES, 1), 100, dtype=np.int16)
                for _ in range(3):
                    local.local_callback(source, FRAMES, None, False)
                local.stop_sample()
                self.assertEqual(local.sample_status()[1], FRAMES)
            finally:
                local.close()

    def test_sample_pairs_the_same_input_block_with_rendered_output(self):
        self.assertTrue(self.audio.start_sample())
        for value in (10, 20, 30, 40):
            self.render(value)
        self.audio.wet[0] = np.full(FRAMES, 200, dtype=np.int16)
        self.assertEqual(self.render(50), 200)
        self.audio.stop_sample()
        original, output = self.audio.sample_tracks()[-1]
        self.assertEqual(int(original[0]), 30)
        self.assertEqual(int(output[0]), 200)
        self.assertEqual(self.audio.sample_status(), (False, 3 * FRAMES, FRAMES))
        self.assertIsNone(self.audio.requested)

    def test_sample_stops_at_the_frame_limit(self):
        self.audio.start_sample()
        with patch.object(audio_module, "SAMPLE_FRAMES", FRAMES + 3):
            for _ in range(4):
                self.render(100)
        self.assertEqual(self.audio.sample_status(), (False, FRAMES + 3, 0))
        self.assertEqual(len(self.audio.sample_tracks()[-1][0]), 3)

    def test_playback_sends_the_selected_track_to_the_selected_device(self):
        dry = np.array([10, 20], dtype=np.int16)
        wet = np.array([100, 200], dtype=np.int16)
        with (patch.object(audio_module.sd, "query_devices",
                           return_value={"max_output_channels": 2}),
              patch.object(audio_module.sd, "WasapiSettings"),
              patch.object(audio_module.sd, "get_stream"),
              patch.object(audio_module.sd, "play") as play):
            audio_module.play_sample(((dry, wet),), 1, 42, start_frame=1)
        np.testing.assert_array_equal(play.call_args.args[0], [[200, 200]])
        self.assertEqual(play.call_args.kwargs["device"], 42)

    def test_stalled_conversion_leaves_dry_mic_running(self):
        self.audio.activate(20)
        self.audio.requested -= 3
        for _ in range(2):
            self.render(100)
        self.assertEqual(self.render(100), 100)
        self.assertIsNone(self.audio.requested)


class TwitchTests(unittest.TestCase):
    def test_chat_permissions_and_cheers(self):
        event = {"message": {"text": "!Voice"}, "chatter_user_id": "viewer", "badges": []}
        self.assertFalse(is_command(event, "owner", "!voice", True))
        self.assertTrue(is_command(event, "owner", "!voice", False))
        event["badges"] = [{"set_id": "moderator"}]
        self.assertTrue(is_command(event, "owner", "!voice", True))
        event["cheer"] = {"bits": 100}
        self.assertFalse(is_command(event, "owner", "!voice", True))


if __name__ == "__main__":
    unittest.main()
