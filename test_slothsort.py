import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import slothsort


class SlothSortTests(unittest.TestCase):
    def test_validate_plan_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "invoice.pdf").write_text("data", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "category"):
                slothsort.validate_plan(
                    {"moves": [{"source": "invoice.pdf", "category": "../escape", "reason": "bad"}]},
                    root,
                    ["invoice.pdf"],
                )

    def test_validate_plan_requires_every_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("one.txt", "two.txt"):
                (root / name).write_text(name, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exactly once"):
                slothsort.validate_plan(
                    {"moves": [{"source": "one.txt", "category": "Notes", "reason": "text"}]},
                    root,
                    ["one.txt", "two.txt"],
                )

    def test_apply_refuses_collision_without_moving_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "photo.jpg").write_text("new", encoding="utf-8")
            (root / "Images").mkdir()
            (root / "Images" / "photo.jpg").write_text("existing", encoding="utf-8")
            moves = [slothsort.Move("photo.jpg", "Images", "photo")]

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                slothsort.apply_plan(root, moves)

            self.assertTrue((root / "photo.jpg").exists())
            self.assertEqual((root / "Images" / "photo.jpg").read_text(encoding="utf-8"), "existing")

    def test_apply_and_undo_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "invoice.pdf").write_text("invoice", encoding="utf-8")
            (root / "sunset.jpg").write_text("photo", encoding="utf-8")
            moves = [
                slothsort.Move("invoice.pdf", "Finance", "invoice"),
                slothsort.Move("sunset.jpg", "Photos", "image"),
            ]

            slothsort.apply_plan(root, moves)
            self.assertTrue((root / "Finance" / "invoice.pdf").exists())
            self.assertTrue((root / "Photos" / "sunset.jpg").exists())

            slothsort.undo(root)
            self.assertTrue((root / "invoice.pdf").exists())
            self.assertTrue((root / "sunset.jpg").exists())
            self.assertFalse((root / slothsort.MANIFEST_NAME).exists())

    def test_undo_rejects_tampered_manifest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "root": str(root.resolve()),
                "moves": [
                    {"source": "../outside.txt", "category": "Notes", "reason": "bad"}
                ],
            }
            (root / slothsort.MANIFEST_NAME).write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "inside"):
                slothsort.undo(root)

    def test_ollama_response_is_parsed(self):
        response_plan = {
            "moves": [{"source": "notes.txt", "category": "Notes", "reason": "plain text"}]
        }

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                self.server.seen_model = request["model"]
                body = json.dumps({"response": json.dumps(response_plan)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        try:
            result = slothsort.call_ollama(
                f"http://127.0.0.1:{server.server_port}", "gemma3:1b", "prompt"
            )
        finally:
            worker.join(timeout=2)
            server.server_close()

        self.assertEqual(result, response_plan)
        self.assertEqual(server.seen_model, "gemma3:1b")


if __name__ == "__main__":
    unittest.main()
