import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.guides import GuideLibrary
from app.integrations import Services


class GuideLibraryTests(unittest.TestCase):
    def test_add_list_replace_and_delete_markdown_guides(self):
        with tempfile.TemporaryDirectory() as folder:
            guide_root = Path(folder) / "guides"
            with patch("app.guides.GUIDE_DIR", guide_root):
                library = GuideLibrary()
                first = library.save("Portrait Rules.md", "# Portrait\nUse soft light.")
                replaced = library.save("Portrait Rules.md", "# Portrait\nUse hard light.")

                self.assertEqual(first["id"], replaced["id"])
                self.assertEqual(1, len(library.list()))
                self.assertIn("hard light", library.list()[0]["content"])
                library.delete(first["id"])
                self.assertEqual([], library.list())

    def test_non_markdown_and_empty_guides_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("app.guides.GUIDE_DIR", Path(folder) / "guides"):
                library = GuideLibrary()
                with self.assertRaisesRegex(ValueError, ".md"):
                    library.save("guide.txt", "text")
                with self.assertRaisesRegex(ValueError, "empty"):
                    library.save("guide.md", "   ")


class NativeGuideGenerationTests(unittest.TestCase):
    def test_four_lm_requests_run_in_parallel_and_results_keep_guide_order(self):
        with tempfile.TemporaryDirectory() as folder:
            services = Services(Config(Path(folder) / "config.json"))
            lock = threading.Lock()
            barrier = threading.Barrier(4)
            active = 0
            maximum_active = 0
            completed = []

            def fake_request(_url, **kwargs):
                nonlocal active, maximum_active
                instruction = kwargs["payload"]["messages"][1]["content"]
                guide_name = next(name for name in ("one.md", "two.md", "three.md", "four.md") if name in instruction)
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                barrier.wait(timeout=2)
                time.sleep({"one.md": 0.08, "two.md": 0.06, "three.md": 0.04, "four.md": 0.02}[guide_name])
                with lock:
                    active -= 1
                    completed.append(guide_name)
                return {"choices": [{"message": {"content": f"1. prompt for {guide_name}"}}]}

            with patch("app.integrations._json_request", side_effect=fake_request):
                result = services.generate_guided_prompts(
                    {
                        "model": "local-model",
                        "prompt": "portrait",
                        "guides": [
                            {"name": "one.md", "content": "One"},
                            {"name": "two.md", "content": "Two"},
                            {"name": "three.md", "content": "Three"},
                            {"name": "four.md", "content": "Four"},
                        ],
                        "count": 1,
                    }
                )

            self.assertEqual(4, maximum_active)
            self.assertEqual(["four.md", "three.md", "two.md", "one.md"], completed)
            self.assertEqual(
                ["one.md", "two.md", "three.md", "four.md"],
                [item["guide"] for item in result["results"]],
            )

    def test_wildcat_calls_lm_studio_directly_once_per_guide(self):
        with tempfile.TemporaryDirectory() as folder:
            services = Services(Config(Path(folder) / "config.json"))
            calls = []

            def fake_request(url, **kwargs):
                calls.append((url, kwargs))
                return {"choices": [{"message": {"content": "1. finished prompt"}}]}

            with patch("app.integrations._json_request", side_effect=fake_request):
                result = services.generate_guided_prompts(
                    {
                        "model": "local-model",
                        "prompt": "summer portrait",
                        "guides": [
                            {"name": "one.md", "content": "Use daylight"},
                            {"name": "two.md", "content": "Use editorial framing"},
                        ],
                        "count": 1,
                        "wordLimit": 300,
                        "temperature": 0.7,
                        "images": [],
                    }
                )

            self.assertEqual(2, len(calls))
            self.assertTrue(all(url.endswith("/v1/chat/completions") for url, _ in calls))
            self.assertEqual(["one.md", "two.md"], [item["guide"] for item in result["results"]])
            instruction = next(
                kwargs["payload"]["messages"][1]["content"]
                for _url, kwargs in calls
                if "Use daylight" in kwargs["payload"]["messages"][1]["content"]
            )
            self.assertIn("Use daylight", instruction)
            self.assertIn("at least 300 words", instruction)

    def test_context_errors_retry_with_less_parallelism_before_failing(self):
        with tempfile.TemporaryDirectory() as folder:
            services = Services(Config(Path(folder) / "config.json"))
            names = ("one.md", "two.md", "three.md", "four.md")
            lock = threading.Lock()
            active = 0
            guide_attempts: dict[str, int] = {}
            peaks: dict[int, int] = {}
            total_requests = 0

            def fake_request(_url, **kwargs):
                nonlocal active, total_requests
                instruction = kwargs["payload"]["messages"][1]["content"]
                guide_name = next(name for name in names if name in instruction)
                with lock:
                    guide_attempts[guide_name] = guide_attempts.get(guide_name, 0) + 1
                    attempt = guide_attempts[guide_name]
                    active += 1
                    total_requests += 1
                    peaks[attempt] = max(peaks.get(attempt, 0), active)
                time.sleep(0.02)
                with lock:
                    active -= 1
                if attempt < 3:
                    raise RuntimeError(
                        "POST http://127.0.0.1:1234/v1/chat/completions failed (400): "
                        "This model's maximum context length is 4096 tokens. However, you requested 8192 tokens."
                    )
                return {"choices": [{"message": {"content": f"1. prompt for {guide_name}"}}]}

            with patch("app.integrations._json_request", side_effect=fake_request):
                result = services.generate_guided_prompts(
                    {
                        "model": "local-model",
                        "prompt": "portrait",
                        "guides": [{"name": name, "content": name} for name in names],
                        "count": 1,
                    }
                )

            self.assertEqual(12, total_requests)
            self.assertEqual({1: 4, 2: 2, 3: 1}, peaks)
            self.assertEqual(names, tuple(item["guide"] for item in result["results"]))
            self.assertEqual(
                [f"1. prompt for {name}" for name in names],
                [item["output"] for item in result["results"]],
            )
            self.assertFalse(any(item.get("error") for item in result["results"]))

    def test_context_errors_surface_after_single_worker_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            services = Services(Config(Path(folder) / "config.json"))

            def fake_request(_url, **kwargs):
                raise RuntimeError(
                    "POST http://127.0.0.1:1234/v1/chat/completions failed (400): "
                    "This model's maximum context length is 4096 tokens. However, you requested 8192 tokens."
                )

            with patch("app.integrations._json_request", side_effect=fake_request):
                result = services.generate_guided_prompts(
                    {
                        "model": "local-model",
                        "prompt": "portrait",
                        "guides": [{"name": "one.md", "content": "One"}],
                        "count": 1,
                    }
                )

            self.assertEqual(["one.md"], [item["guide"] for item in result["results"]])
            self.assertIn("maximum context length", result["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
