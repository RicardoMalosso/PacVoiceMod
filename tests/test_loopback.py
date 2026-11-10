import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest

from pacvoicemod.engine import Engine, EngineAPIError, FRAMES
from pacvoicemod.vcclient import LocalVCClient


class LoopbackTests(unittest.TestCase):
    def start_server(self, legacy):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if legacy and self.path == "/info":
                    self.reply(200, {"gpus": [{"id": 0}], "modelSlots": []})
                else:
                    self.reply(404, {"detail": "Not Found"})

            def do_POST(self):
                if legacy and self.path == "/test":
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    self.reply(200, {"changedVoiceBase64": request["buffer"]})
                else:
                    self.reply(404, {"detail": "Not Found"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def test_existing_server_and_conversion_over_real_http(self):
        port = self.start_server(legacy=True)
        engine = Engine(port, 90)
        LocalVCClient(None).ensure_running(engine, None, port, self.fail)
        sample = b"\x01\x00" * FRAMES
        self.assertEqual(engine.convert(sample), sample)

    def test_wrong_api_reports_mismatch_over_real_http(self):
        port = self.start_server(legacy=False)
        with self.assertRaisesRegex(EngineAPIError, "/info and /test"):
            LocalVCClient(None).ensure_running(Engine(port, 90), None, port, self.fail)
