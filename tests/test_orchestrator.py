import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.orchestrator import JobManager
from app.storage import Storage


WORKFLOW = {
    "1": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "old"},
        "_meta": {"title": "Positive Prompt"},
    },
    "2": {
        "class_type": "KSampler",
        "inputs": {"positive": ["1", 0], "seed": 1},
    },
}


class FakeServices:
    def __init__(self):
        self.calls = []
        self.job_count = 0
        self.guided_counts = []
        self.guided_group_sizes = []
        self.guided_images = []

    def ensure_comfy(self): self.calls.append("ensure_comfy")
    def free_comfy(self): self.calls.append("free_comfy")
    def ensure_lm(self): self.calls.append("ensure_lm")
    def load_lm(self, model): self.calls.append(f"load_lm:{model}"); return model
    def unload_all_lm(self): self.calls.append("unload_lm"); return []
    def interrupt_comfy(self): self.calls.append("interrupt_comfy")
    def lm_models(self): return [{"id": "vision-model", "vision": True}]

    def compare_guide_images(self, model, goal, focus, labeled_images):
        self.calls.append(f"compare:{model}:{len(labeled_images)}")
        self.comparison_images = list(labeled_images)
        return "1. Guide B\n2. Guide A"

    def submit_comfy(self, workflow, client_id):
        self.job_count += 1
        self.calls.append(f"submit:{workflow['1']['inputs']['text']}")
        return f"job-{self.job_count}"

    def comfy_history(self, prompt_id):
        return {
            prompt_id: {
                "outputs": {
                    "3": {"images": [{"filename": f"{prompt_id}.png", "subfolder": "", "type": "output"}]}
                }
            }
        }

    def download_comfy_output(self, output):
        return b"fake-image", "image/png"

    def generate_guided_prompts(self, payload):
        count = int(payload["count"])
        self.guided_counts.append(count)
        self.guided_group_sizes.append(len(payload["guides"]))
        self.guided_images.append([image.get("name") for image in payload.get("images", [])])
        results = []
        for guide in payload["guides"]:
            output = "\n".join(f"{index}. {guide['name']} variation {len(self.guided_counts)}-{index}" for index in range(1, count + 1))
            results.append({"guide": guide["name"], "output": output})
        return {"model": "test-model", "results": results}


class OrchestratorTests(unittest.TestCase):
    def test_pause_interrupts_current_comfy_run_and_resume_retries_it(self):
        class BlockingFirstRunServices(FakeServices):
            def __init__(self):
                super().__init__()
                self.first_history_polled = threading.Event()

            def comfy_history(self, prompt_id):
                if prompt_id == "job-1":
                    self.first_history_polled.set()
                    return {}
                return super().comfy_history(prompt_id)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
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
            services = BlockingFirstRunServices()
            manager = JobManager(storage, config, services)
            batch_id = storage.create_batch({
                "source": "paste",
                "title": "Pause retry",
                "pasted_prompts": "portrait",
                "images_per_prompt": 1,
            })

            with patch("app.orchestrator.ROOT", root), patch("app.orchestrator.IMAGE_DIR", root / "data" / "images"):
                manager.start(batch_id)
                self.assertTrue(services.first_history_polled.wait(3))
                manager.pause(batch_id)
                deadline = time.time() + 5
                paused_retry_recorded = False
                while time.time() < deadline:
                    batch = storage.batch(batch_id)
                    paused_retry_recorded = any(
                        "was interrupted for pause and will retry on resume" in event["message"]
                        for event in batch["events"]
                    )
                    if paused_retry_recorded:
                        break
                    time.sleep(0.05)
                self.assertTrue(paused_retry_recorded)
                self.assertEqual(0, storage.batch(batch_id)["completed_runs"])
                manager.resume(batch_id)
                deadline = time.time() + 5
                while time.time() < deadline and storage.batch(batch_id)["status"] not in {"complete", "failed"}:
                    time.sleep(0.05)
                while time.time() < deadline and manager.active()["running"]:
                    time.sleep(0.05)

            batch = storage.batch(batch_id)
            self.assertEqual("complete", batch["status"])
            self.assertEqual(1, batch["completed_runs"])
            self.assertEqual(2, len([call for call in services.calls if call.startswith("submit:")]))
            self.assertIn("interrupt_comfy", services.calls)

    def test_batch_uses_its_selected_workflow_from_multiple_saved_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first_path = root / "first-api.json"
            second_path = root / "second-api.json"
            first_path.write_text(json.dumps(WORKFLOW), encoding="utf-8")
            second_path.write_text(json.dumps(WORKFLOW), encoding="utf-8")
            config = Config(root / "config.json")
            config.update({
                "workflows": [
                    {
                        "id": "first",
                        "name": "first-api.json",
                        "file": str(first_path),
                        "positive_fields": ["1:text"],
                        "negative_fields": [],
                    },
                    {
                        "id": "second",
                        "name": "second-api.json",
                        "file": str(second_path),
                        "positive_fields": ["1:text"],
                        "negative_fields": [],
                    },
                ],
                "active_workflow_id": "first",
            })
            storage = Storage(root / "library.sqlite3")
            manager = JobManager(storage, config, FakeServices())
            batch_id = storage.create_batch({
                "source": "paste",
                "title": "Second workflow",
                "workflow_id": "second",
                "workflow_name": "second-api.json",
                "pasted_prompts": "portrait",
                "images_per_prompt": 1,
            })

            with patch("app.orchestrator.ROOT", root), patch("app.orchestrator.IMAGE_DIR", root / "data" / "images"):
                manager._run(batch_id)

            provenance = storage.batch(batch_id)["prompts"][0]["assets"][0]["source"]["vram_relay"]
            self.assertEqual("second", provenance["workflow_id"])
            self.assertEqual("second-api.json", provenance["workflow_name"])

    def test_resume_continues_only_unfinished_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(WORKFLOW), encoding="utf-8")
            config = Config(root / "config.json")
            config.update({
                "workflow_file": str(workflow_path),
                "workflow_positive_fields": ["1:text"],
                "workflow_negative_fields": [],
            })
            storage = Storage(root / "library.sqlite3")
            services = FakeServices()
            manager = JobManager(storage, config, services)
            batch_id = storage.create_batch({
                "source": "paste",
                "title": "Interrupted",
                "pasted_prompts": "first\nsecond",
                "images_per_prompt": 1,
            })
            storage.add_prompts(batch_id, [{"prompt": "first"}, {"prompt": "second"}], 1)
            first_prompt = storage.pending_prompts(batch_id)[0]
            storage.increment_run(batch_id, first_prompt["id"], 123)
            storage.update_batch(batch_id, status="interrupted", phase="Stopped")

            with patch("app.orchestrator.ROOT", root), patch("app.orchestrator.IMAGE_DIR", root / "data" / "images"):
                result = manager.resume(batch_id)
                deadline = time.time() + 3
                while time.time() < deadline and storage.batch(batch_id)["status"] not in {"complete", "failed"}:
                    time.sleep(0.02)
                while time.time() < deadline and manager.active()["running"]:
                    time.sleep(0.02)

            batch = storage.batch(batch_id)
            self.assertEqual(batch_id, result["active_batch_id"])
            self.assertEqual("complete", batch["status"])
            self.assertEqual(2, batch["completed_runs"])
            self.assertEqual(1, len([call for call in services.calls if call.startswith("submit:")]))

    def test_fifo_queue_starts_next_batch_automatically(self):
        class ControlledManager(JobManager):
            def __init__(self, *args):
                super().__init__(*args)
                self.started = []
                self.first_started = threading.Event()
                self.release_first = threading.Event()

            def _run(self, batch_id):
                self.started.append(batch_id)
                self.storage.update_batch(batch_id, status="running", phase="Test run")
                if len(self.started) == 1:
                    self.first_started.set()
                    self.release_first.wait(3)
                self.storage.update_batch(batch_id, status="complete", phase="Complete")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            storage = Storage(root / "library.sqlite3")
            manager = ControlledManager(storage, Config(root / "config.json"), FakeServices())
            first = storage.create_batch({"source": "paste", "title": "First"})
            second = storage.create_batch({"source": "paste", "title": "Second"})

            manager.start(first)
            self.assertTrue(manager.first_started.wait(2))
            queued = manager.start(second)
            self.assertTrue(queued["queued"])
            self.assertEqual(1, queued["queue_position"])

            manager.release_first.set()
            deadline = time.time() + 3
            while time.time() < deadline and storage.batch(second)["status"] != "complete":
                time.sleep(0.02)
            self.assertEqual([first, second], manager.started)
            self.assertEqual("complete", storage.batch(second)["status"])
            self.assertEqual(0, storage.queued_count())

    def test_cancel_removes_waiting_batch_from_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            storage = Storage(root / "library.sqlite3")
            manager = JobManager(storage, Config(root / "config.json"), FakeServices())
            batch_id = storage.create_batch({"source": "paste", "title": "Waiting"})

            manager.cancel(batch_id)

            self.assertEqual("cancelled", storage.batch(batch_id)["status"])
            self.assertEqual(0, storage.queued_count())

    def test_completed_batch_cannot_be_relabeled_cancelled(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            storage = Storage(root / "library.sqlite3")
            manager = JobManager(storage, Config(root / "config.json"), FakeServices())
            batch_id = storage.create_batch({"source": "paste", "title": "Finished"})
            storage.update_batch(
                batch_id,
                status="complete",
                phase="Finished",
                total_runs=2,
                completed_runs=2,
            )

            with self.assertRaisesRegex(ValueError, "already complete"):
                manager.cancel(batch_id)

            self.assertEqual("complete", storage.batch(batch_id)["status"])

    def test_starting_later_batch_does_not_jump_fifo_queue(self):
        class BlockingManager(JobManager):
            def __init__(self, *args):
                super().__init__(*args)
                self.started = []
                self.release = threading.Event()

            def _run(self, batch_id):
                self.started.append(batch_id)
                self.release.wait(3)
                self.storage.update_batch(batch_id, status="complete", phase="Complete")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            storage = Storage(root / "library.sqlite3")
            manager = BlockingManager(storage, Config(root / "config.json"), FakeServices())
            first = storage.create_batch({"source": "paste", "title": "Older"})
            second = storage.create_batch({"source": "paste", "title": "Newer"})

            result = manager.start(second)
            deadline = time.time() + 2
            while time.time() < deadline and not manager.started:
                time.sleep(0.01)

            self.assertTrue(result["queued"])
            self.assertEqual(first, result["active_batch_id"])
            self.assertEqual([first], manager.started)
            self.assertEqual(1, result["queue_position"])
            manager.cancel(second)
            manager.release.set()
            deadline = time.time() + 2
            while time.time() < deadline and manager.active()["running"]:
                time.sleep(0.01)

    def test_optional_comparison_samples_each_guide_and_saves_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            storage = Storage(root / "library.sqlite3")
            services = FakeServices()
            manager = JobManager(storage, Config(root / "config.json"), services)
            batch_id = storage.create_batch({
                "source": "guides",
                "title": "Compare",
                "brief": "Strong fantasy city concept art",
            })
            storage.add_prompts(batch_id, [
                {"guide": "Guide A", "prompt": "A1"},
                {"guide": "Guide A", "prompt": "A2"},
                {"guide": "Guide B", "prompt": "B1"},
                {"guide": "Guide B", "prompt": "B2"},
            ], 1)
            storage.update_batch(batch_id, status="complete", phase="Complete")
            image_folder = root / "data" / "images"
            image_folder.mkdir(parents=True)
            for index, prompt in enumerate(storage.batch(batch_id)["prompts"], start=1):
                filename = f"sample-{index}.png"
                (image_folder / filename).write_bytes(b"fake-image")
                storage.add_asset(
                    batch_id,
                    prompt["id"],
                    "images",
                    filename,
                    f"data/images/{filename}",
                    {},
                )

            with patch("app.orchestrator.ROOT", root):
                result = manager.compare_guides(batch_id, "vision-model", "composition", 2)

            batch = storage.batch(batch_id)
            self.assertEqual("1. Guide B\n2. Guide A", result)
            self.assertEqual(4, len(services.comparison_images))
            self.assertEqual({"Guide A", "Guide B"}, {item[0] for item in services.comparison_images})
            self.assertEqual("composition", batch["comparisons"][0]["focus"])
            self.assertIn("compare:vision-model:4", services.calls)
            self.assertEqual("unload_lm", services.calls[-1])

    def test_pasted_prompts_run_sequentially_and_free_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(WORKFLOW), encoding="utf-8")
            config = Config(root / "config.json")
            config.update({
                "workflow_file": str(workflow_path),
                "workflow_positive_fields": ["1:text"],
                "workflow_negative_fields": [],
            })
            storage = Storage(root / "library.sqlite3")
            services = FakeServices()
            manager = JobManager(storage, config, services)
            batch_id = storage.create_batch({
                "source": "paste",
                "title": "Import",
                "pasted_prompts": "first\nsecond",
                "images_per_prompt": 2,
            })
            with patch("app.orchestrator.ROOT", root), patch("app.orchestrator.IMAGE_DIR", root / "data" / "images"):
                manager._run(batch_id)
            batch = storage.batch(batch_id)
            self.assertEqual("complete", batch["status"])
            self.assertEqual(4, batch["completed_runs"])
            self.assertEqual(4, sum(len(prompt["assets"]) for prompt in batch["prompts"]))
            provenance = batch["prompts"][0]["assets"][0]["source"]["vram_relay"]
            self.assertEqual("workflow.json", provenance["workflow_name"])
            self.assertEqual("workflow.json", provenance["workflow_file"])
            self.assertEqual(4, len([call for call in services.calls if call.startswith("submit:")]))
            self.assertLess(services.calls.index("unload_lm"), services.calls.index("ensure_comfy"))
            self.assertEqual("free_comfy", services.calls[-1])

    def test_native_guide_generation_chunks_past_existing_twenty_prompt_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = Config(root / "config.json")
            storage = Storage(root / "library.sqlite3")
            services = FakeServices()
            manager = JobManager(storage, config, services)
            request = {
                "source": "guides",
                "title": "Large",
                "brief": "Distinct fantasy cities",
                "model": "test-model",
                "guides": [{"name": "Guide A", "content": "A"}, {"name": "Guide B", "content": "B"}],
                "prompts_per_guide": 25,
                "images_per_prompt": 1,
            }
            batch_id = storage.create_batch(request)
            manager._generate_guided_prompts(batch_id, request, 1)
            self.assertEqual([20, 5], services.guided_counts)
            self.assertEqual(50, storage.batch(batch_id)["total_prompts"])
            self.assertNotIn("ensure_comfy", services.calls)
            self.assertLess(services.calls.index("free_comfy"), services.calls.index("load_lm:test-model"))
            self.assertEqual("unload_lm", services.calls[-1])

    def test_each_reference_is_used_separately_and_adds_one_prompt_per_guide(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = Config(root / "config.json")
            storage = Storage(root / "library.sqlite3")
            services = FakeServices()
            manager = JobManager(storage, config, services)
            references = [
                {"id": "ref-one", "name": "one.png", "processed_path": "unused-one.png"},
                {"id": "ref-two", "name": "two.png", "processed_path": "unused-two.png"},
                {"id": "ref-three", "name": "three.png", "processed_path": "unused-three.png"},
            ]
            request = {
                "source": "guides",
                "title": "Reference queue",
                "brief": "Fashion portraits",
                "model": "vision-model",
                "guides": [{"name": "Guide A", "content": "A"}, {"name": "Guide B", "content": "B"}],
                "prompts_per_guide": 4,
                "images_per_prompt": 1,
                "references": references,
                "reference_mode": "inspiration",
            }
            batch_id = storage.create_batch(request)
            prompt_images = [
                {"id": item["id"], "name": item["name"], "dataUrl": "data:image/png;base64,eA=="}
                for item in references
            ]

            with patch.object(manager.references, "prompt_images", return_value=prompt_images):
                manager._generate_guided_prompts(batch_id, request, 1)

            batch = storage.batch(batch_id)
            self.assertEqual([3, 2, 2], services.guided_counts)
            self.assertEqual([["one.png"], ["two.png"], ["three.png"]], services.guided_images)
            self.assertEqual(14, batch["total_prompts"])
            self.assertEqual({"one.png", "two.png", "three.png"}, {item["reference_name"] for item in batch["prompts"]})

    def test_more_than_four_guides_are_split_into_safe_native_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = Config(root / "config.json")
            storage = Storage(root / "library.sqlite3")
            services = FakeServices()
            manager = JobManager(storage, config, services)
            guides = [{"name": f"Guide {index}", "content": str(index)} for index in range(1, 10)]
            request = {
                "source": "guides",
                "title": "Many guides",
                "brief": "Distinct city concepts",
                "model": "test-model",
                "guides": guides,
                "prompts_per_guide": 25,
                "images_per_prompt": 1,
            }
            batch_id = storage.create_batch(request)

            manager._generate_guided_prompts(batch_id, request, 1)

            self.assertEqual([4, 4, 3, 3, 2, 2], services.guided_group_sizes)
            self.assertEqual([20, 5, 20, 5, 20, 5], services.guided_counts)
            self.assertEqual(225, storage.batch(batch_id)["total_prompts"])


if __name__ == "__main__":
    unittest.main()
