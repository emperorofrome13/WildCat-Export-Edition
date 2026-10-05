import json
import tempfile
import time
import unittest
from pathlib import Path

from app.config import Config
from app.orchestrator import JobManager
from app.storage import Storage

try:
    from test_orchestrator import WORKFLOW, FakeServices
except ImportError:
    from tests.test_orchestrator import WORKFLOW, FakeServices


def wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class QueueControlTests(unittest.TestCase):
    def _manager(self, root: Path):
        workflow_path = root / "workflow.json"
        workflow_path.write_text(json.dumps(WORKFLOW), encoding="utf-8")
        config = Config(root / "config.json")
        config.update({
            "workflow_file": str(workflow_path),
            "workflow_name": "workflow.json",
            "workflow_positive_fields": ["1:text"],
            "workflow_negative_fields": [],
        })
        storage = Storage(root / "library.sqlite3")
        return storage, config, JobManager(storage, config, FakeServices())

    def _queued_batch(self, storage: Storage, title: str) -> str:
        return storage.create_batch({
            "source": "paste",
            "title": title,
            "pasted_prompts": "portrait",
            "images_per_prompt": 1,
        })

    def test_start_next_queued_respects_pause_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            storage, _config, manager = self._manager(Path(folder))
            batch_id = self._queued_batch(storage, "Paused queue")
            manager.pause_queue()
            self.assertIsNone(manager.start_next_queued())
            self.assertEqual(storage.batch(batch_id, include_details=False)["status"], "queued")
            manager.unpause_queue()
            self.assertIsNotNone(manager.start_next_queued())
            self.assertTrue(wait_until(lambda: not manager.active()["running"]))
            self.assertEqual(storage.batch(batch_id, include_details=False)["status"], "complete")

    def test_queue_batch_parks_while_queue_paused(self):
        with tempfile.TemporaryDirectory() as folder:
            storage, _config, manager = self._manager(Path(folder))
            batch_id = self._queued_batch(storage, "Parked batch")
            manager.pause_queue()
            state = manager.queue_batch(batch_id)
            self.assertTrue(state["queued"])
            self.assertEqual(storage.batch(batch_id, include_details=False)["status"], "queued")
            self.assertIsNone(state.get("active_batch_id"))

    def test_queue_batch_starts_oldest_when_lane_free(self):
        with tempfile.TemporaryDirectory() as folder:
            storage, _config, manager = self._manager(Path(folder))
            batch_id = self._queued_batch(storage, "FIFO starter")
            state = manager.queue_batch(batch_id)
            self.assertEqual(state.get("queued"), False)
            self.assertEqual(state.get("active_batch_id"), batch_id)
            self.assertTrue(wait_until(lambda: not manager.active()["running"]))
            self.assertEqual(storage.batch(batch_id, include_details=False)["status"], "complete")

    def test_queue_batch_rejects_running_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            storage, _config, manager = self._manager(Path(folder))
            batch_id = self._queued_batch(storage, "Already live")
            manager.start(batch_id)
            try:
                self.assertTrue(wait_until(lambda: manager.active()["running"]))
                with self.assertRaises(RuntimeError):
                    manager.queue_batch(batch_id)
            finally:
                manager.cancel(batch_id)
                self.assertTrue(wait_until(lambda: not manager.active()["running"]))


class RecycleSearchTests(unittest.TestCase):
    def test_filename_search_escapes_like_wildcards(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "library.sqlite3")
            batch_id = storage.create_batch({"source": "paste", "title": "Search", "pasted_prompts": "x"})
            storage.add_prompts(batch_id, [{"prompt": "p", "negative_prompt": ""}], 1)
            prompt_id = storage.batch(batch_id)["prompts"][0]["id"]
            filename = "ComfyUI_00024_.png"
            storage.add_asset(batch_id, prompt_id, "images", filename, "data/images/x/ComfyUI_00024_.png", {})
            self.assertIsNotNone(storage.find_asset_by_filename("ComfyUI_00024_.png"))
            self.assertIsNone(storage.find_asset_by_filename("ComfyUIA00024A.png"))
            self.assertIsNone(storage.find_asset_by_filename("ComfyUIX00024Xpng"))

    def test_clone_batch_for_rerun_with_workflow_and_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "library.sqlite3")
            batch_id = storage.create_batch({
                "source": "paste",
                "title": "Original job",
                "pasted_prompts": "a\nb\nc",
                "images_per_prompt": 1,
                "workflow_id": "old-workflow",
                "workflow_name": "old.json",
            })
            storage.add_prompts(batch_id, [
                {"prompt": "good prompt", "negative_prompt": ""},
                {"prompt": "bad prompt", "negative_prompt": ""},
            ], 1)
            clone_prompts = storage.batch(batch_id)["prompts"]
            storage.update_prompt(clone_prompts[1]["id"], error="ComfyUI timed out")
            clone_id = storage.clone_batch_for_rerun(
                batch_id,
                workflow_id="new-workflow-id",
                workflow_name="new.json",
                selection="errors",
            )
            clone = storage.batch(clone_id)
            self.assertEqual(clone["request"]["workflow_id"], "new-workflow-id")
            self.assertEqual(clone["request"]["workflow_name"], "new.json")
            self.assertIn("rerun via new.json", clone["title"])
            self.assertEqual(len(clone["prompts"]), 1)
            self.assertEqual(clone["prompts"][0]["prompt"], "bad prompt")

    def test_clone_batch_for_rerun_unrated_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "library.sqlite3")
            batch_id = storage.create_batch({"source": "paste", "title": "Ratings", "pasted_prompts": "a\nb", "images_per_prompt": 1})
            storage.add_prompts(batch_id, [{"prompt": "rated", "negative_prompt": ""}, {"prompt": "unrated", "negative_prompt": ""}], 1)
            prompts = storage.batch(batch_id)["prompts"]
            storage.add_asset(batch_id, prompts[0]["id"], "images", "a.png", "data/images/x/a.png", {})
            asset = storage.batch(batch_id)["prompts"][0]["assets"][0]
            storage.rate_asset(asset["id"], 4)
            clone_id = storage.clone_batch_for_rerun(batch_id, selection="unrated")
            clone = storage.batch(clone_id)
            self.assertEqual(len(clone["prompts"]), 1)
            self.assertEqual(clone["prompts"][0]["prompt"], "unrated")

    def test_find_asset_by_hash_round_trip(self):
        import hashlib
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "library.sqlite3")
            batch_id = storage.create_batch({"source": "paste", "title": "Hash", "pasted_prompts": "x"})
            storage.add_prompts(batch_id, [{"prompt": "p", "negative_prompt": ""}], 1)
            prompt_id = storage.batch(batch_id)["prompts"][0]["id"]
            digest = "a" * 64
            with patch("app.storage._file_sha256", return_value=digest):
                storage.add_asset(batch_id, prompt_id, "images", "out.png", "data/images/x/out.png", {"vram_relay": {"seed": 1}})
            found = storage.find_asset_by_hash(digest)
            self.assertIsNotNone(found)
            self.assertEqual(found["filename"], "out.png")
            self.assertNotIn("file_hash", found)


if __name__ == "__main__":
    unittest.main()
