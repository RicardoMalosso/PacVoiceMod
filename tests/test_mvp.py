from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import numpy as np

try:
    from pacvoicemod import audio as audio_module
except OSError:
    # The tests exercise the callback without opening a hardware stream.
    with patch.dict(sys.modules, {"sounddevice": MagicMock()}):
        from pacvoicemod import audio as audio_module
from pacvoicemod.engine import Engine, FRAMES, model_files
from pacvoicemod.twitch import is_command


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

    def test_engine_rejects_one_sample_error_response(self):
        engine = Engine(18888, 90)
        with patch.object(engine, "request", return_value={"changedVoiceBase64": "AAA="}):
            with self.assertRaisesRegex(RuntimeError, "block length"):
                engine.convert(bytes(FRAMES * 2))


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
