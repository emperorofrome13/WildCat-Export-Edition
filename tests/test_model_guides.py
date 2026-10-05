import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from app import export_server
from app.config import Config
from app.guides import GuideLibrary
from app.integrations import Services
from app.orchestrator import JobManager
from app.storage import Storage


ROOT = Path(__file__).resolve().parents[1]
MODEL_GUIDES = {
    "Z-Image-Turbo-enhancer.md": "https://huggingface.co/Tongyi-MAI/Z-Image-Turbo",
    "Krea-2-enhancer.md": "https://github.com/krea-ai/krea-2/blob/main/docs/prompting.md",
    "Qwen-Image-2.1-enhancer.md": "https://huggingface.co/Qwen/Qwen-Image-2.1",
    "Ideogram-4.5-enhancer.md": "https://developer.ideogram.ai/ideogram-api/api-overview",
}


class ModelGuideTests(unittest.TestCase):
    def test_six_bundled_guides_load_with_authorship_and_sources(self):
        with tempfile.TemporaryDirectory() as folder, patch("app.guides.GUIDE_DIR", Path(folder)):
            library = GuideLibrary()
            for path in (ROOT / "example-guides").glob("*.md"):
                library.save(path.name, path.read_text(encoding="utf-8"))
            entries = {entry["name"]: entry for entry in library.list()}
            self.assertEqual(6, len(entries))
            for name, source in MODEL_GUIDES.items():
                content = entries[name]["content"]
                self.assertIn("WildCat-authored", content)
                self.assertIn(source, content)
                self.assertIn("## Output contract", content)
                self.assertGreater(len(content), 1500)

    def test_each_model_guide_generates_a_single_guide_batch(self):
        for name in MODEL_GUIDES:
            with self.subTest(guide=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config = Config(root / "config.json")
                storage = Storage(root / "library.sqlite3")
                service = Mock()
                service.load_lm.return_value = "test-model"
                service.generate_guided_prompts.side_effect = lambda payload: {
                    "results": [{"guide": item["name"], "output": "\n".join(
                        f"{index}. A clear garden scene variation {index}."
                        for index in range(1, payload["count"] + 1)
                    )} for item in payload["guides"]]
                }
                manager = JobManager(storage, config, service)
                guide = {"name": name, "content": (ROOT / "example-guides" / name).read_text(encoding="utf-8")}
                request = {"source": "guides", "brief": "A garden scene", "model": "test-model",
                           "guides": [guide], "prompts_per_guide": 4, "images_per_prompt": 1}
                batch_id = storage.create_batch(request)
                manager._generate_guided_prompts(batch_id, request, 1)
                batch = storage.batch(batch_id)
                self.assertEqual(4, batch["total_prompts"])
                self.assertEqual([guide], service.generate_guided_prompts.call_args.args[0]["guides"])
                self.assertEqual({name}, {item["guide"] for item in batch["prompts"]})
                service.unload_all_lm.assert_called_once()

    def test_zero_guides_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            JobManager._guide_groups([])
        self.assertEqual([[{"name": "one"}]], JobManager._guide_groups([{"name": "one"}]))

    def test_prompt_instruction_includes_entire_model_guide_and_user_limits(self):
        for name in MODEL_GUIDES:
            guide = {"name": name, "content": (ROOT / "example-guides" / name).read_text(encoding="utf-8")}
            instruction = Services._guide_instruction({"count": 4, "wordLimit": 300, "prompt": "A garden scene"}, guide)
            self.assertIn(guide["content"], instruction)
            self.assertIn("Output exactly 4 prompts", instruction)
            self.assertIn("at least 300 words", instruction)
            self.assertIn("A garden scene", instruction)

    def test_http_accepts_one_guide_but_not_zero_without_starting_ai(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "library.sqlite3")
            jobs = Mock()
            jobs.workflow_entry.return_value = {"id": "workflow", "name": "test.json"}
            jobs.active.return_value = {"batch_id": None}
            refs = Mock()
            refs.selected.return_value = []
            refs.list.return_value = []
            server = export_server.legacy.AppServer(("127.0.0.1", 0), export_server.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(export_server.legacy, "storage", storage), patch.object(export_server.legacy, "jobs", jobs), patch.object(export_server.legacy, "references", refs):
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    body = {"source": "guides", "workflow_id": "workflow", "model": "test-model",
                            "brief": "A garden", "guides": [], "queue_only": True, "prompts_per_guide": 4}
                    def request():
                        return urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/batches",
                            data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Wildcat-Token": export_server.TOKEN})
                    with self.assertRaises(urllib.error.HTTPError) as result:
                        opener.open(request())
                    self.assertEqual(409, result.exception.code)
                    body["guides"] = [{"name": "Krea-2-enhancer.md", "content": "Natural-language expansion."}]
                    with opener.open(request()) as response:
                        self.assertEqual(201, response.status)
                        saved = storage.batch(json.load(response)["batch_id"])
                    self.assertEqual(1, len(saved["request"]["guides"]))
                    jobs.start.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
