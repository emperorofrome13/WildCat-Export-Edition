import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from app.core import (
    analyze_image_inputs,
    analyze_workflow,
    parse_numbered_prompts,
    prepare_image_workflow,
    prepare_workflow,
    read_comfy_prompt_graph,
    read_png_text_metadata,
    split_pasted_prompts,
    validate_workflow_json,
    workflow_match_score,
)
from app.integrations import Services
from app.metadata import convert_png_to_civitai, extract_generation_data, inspect_civitai_metadata
from app.recycle import RecyclePixelBin
from app.storage import Storage, image_dimensions


WORKFLOW = {
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "old positive", "clip": ["4", 1]},
        "_meta": {"title": "Positive Prompt"},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "old negative", "clip": ["4", 1]},
        "_meta": {"title": "Negative Prompt"},
    },
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "positive": ["6", 0],
            "negative": ["7", 0],
            "seed": 1,
            "model": ["4", 0],
        },
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}},
}


class PromptParsingTests(unittest.TestCase):
    def test_numbered_prompts_and_negatives(self):
        parsed = parse_numbered_prompts(
            "1. Warm desert city\nNegative Prompt: rain, snow\n\n2. Frozen orbital city\nNegative Prompt: heat"
        )
        self.assertEqual(2, len(parsed))
        self.assertEqual("Warm desert city", parsed[0]["prompt"])
        self.assertEqual("rain, snow", parsed[0]["negative_prompt"])

    def test_one_prompt_per_line_import(self):
        parsed = split_pasted_prompts("first image\nsecond image\nthird image")
        self.assertEqual(["first image", "second image", "third image"], [item["prompt"] for item in parsed])


class WorkflowTests(unittest.TestCase):
    def test_image_tool_replaces_only_detected_load_image_inputs(self):
        workflow = {
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": "old.png", "upload": "image"},
                "_meta": {"title": "Source image"},
            },
            "2": {"class_type": "SomeProcessor", "inputs": {"image": ["1", 0], "strength": 0.5}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "edited"}},
        }
        fields = analyze_image_inputs(workflow)
        self.assertEqual([field["field_id"] for field in fields], ["1:image"])
        prepared = prepare_image_workflow(workflow, "vram-relay/new.png")
        self.assertEqual(prepared["1"]["inputs"]["image"], "vram-relay/new.png")
        self.assertEqual(prepared["2"]["inputs"]["strength"], 0.5)
        self.assertEqual(workflow["1"]["inputs"]["image"], "old.png")

    def test_image_tool_maps_short_instruction_only_to_positive_text(self):
        workflow = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "old edit"}, "_meta": {"title": "Positive Prompt"}},
            "3": {"class_type": "KSampler", "inputs": {"positive": ["2", 0], "image": ["1", 0]}},
        }
        prepared = prepare_image_workflow(
            workflow,
            "wildcat/source.png",
            ["1:image"],
            "remove water",
            ["2:text"],
        )
        self.assertEqual("wildcat/source.png", prepared["1"]["inputs"]["image"])
        self.assertEqual("remove water", prepared["2"]["inputs"]["text"])
        self.assertEqual("old edit", workflow["2"]["inputs"]["text"])

    def test_image_tool_supports_comfy_node_ids_containing_colons(self):
        workflow = {
            "10:11": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
            "75:74": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "old edit"},
                "_meta": {"title": "Positive Prompt"},
            },
            "80": {"class_type": "KSampler", "inputs": {"positive": ["75:74", 0], "image": ["10:11", 0]}},
        }
        prepared = prepare_image_workflow(
            workflow,
            "wildcat/source.png",
            ["10:11:image"],
            "change her hair color",
            ["75:74:text"],
        )
        self.assertEqual("wildcat/source.png", prepared["10:11"]["inputs"]["image"])
        self.assertEqual("change her hair color", prepared["75:74"]["inputs"]["text"])

    def test_analysis_traces_sampler_conditioning(self):
        analysis = analyze_workflow(WORKFLOW)
        self.assertIn("6:text", analysis["recommended_positive"])
        self.assertIn("7:text", analysis["recommended_negative"])
        self.assertIn("3:seed", analysis["seed_fields"])

    def test_prepare_changes_only_mapped_text_and_seed(self):
        prepared, seed = prepare_workflow(
            WORKFLOW,
            "new positive",
            "new negative",
            ["6:text"],
            ["7:text"],
            seed=42,
        )
        self.assertEqual("new positive", prepared["6"]["inputs"]["text"])
        self.assertEqual("new negative", prepared["7"]["inputs"]["text"])
        self.assertEqual(42, prepared["3"]["inputs"]["seed"])
        self.assertEqual("model.safetensors", prepared["4"]["inputs"]["ckpt_name"])
        self.assertEqual(42, seed)
        self.assertEqual("old positive", WORKFLOW["6"]["inputs"]["text"])

    def test_rejects_editor_workflow_format(self):
        with self.assertRaisesRegex(ValueError, r"Export Workflow \(API\)"):
            validate_workflow_json(json.dumps({"nodes": [], "links": []}))

    def test_matches_embedded_workflow_while_ignoring_prompt_seed_and_number_format(self):
        executed = json.loads(json.dumps(WORKFLOW))
        executed["6"]["inputs"]["text"] = "a different prompt"
        executed["3"]["inputs"]["seed"] = 999
        executed["3"]["inputs"]["cfg"] = 1.0
        candidate = json.loads(json.dumps(WORKFLOW))
        candidate["3"]["inputs"]["cfg"] = 1
        score = workflow_match_score(executed, candidate, {"6:text", "3:seed"})
        self.assertEqual(1.0, score)

    def test_reads_comfy_prompt_graph_from_png_text_metadata(self):
        graph = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}}}
        payload = b"prompt\0" + json.dumps(graph).encode("utf-8")

        def chunk(kind, data):
            checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", payload)
            + chunk(b"IEND", b"")
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metadata.png"
            path.write_bytes(png)
            self.assertEqual(graph, read_comfy_prompt_graph(path))

    def test_deterministic_civitai_conversion_preserves_comfy_metadata_and_pixels(self):
        graph = {
            "1": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "A café at night", "clip": ["5", 1]},
                "_meta": {"title": "Positive Prompt"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "blurry", "clip": ["5", 1]},
                "_meta": {"title": "Negative Prompt"},
            },
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["5", 0],
                    "positive": ["1", 0],
                    "negative": ["2", 0],
                    "seed": 123,
                    "steps": 28,
                    "cfg": 6.5,
                    "sampler_name": "dpmpp_2m",
                    "scheduler": "karras",
                    "denoise": 0.8,
                },
            },
            "4": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["6", 0]},
            },
            "5": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "models/portrait-v2.safetensors"},
            },
            "6": {"class_type": "VAELoader", "inputs": {"vae_name": "clearvae.safetensors"}},
            "7": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "Test/Test", "images": ["4", 0]},
            },
        }

        def chunk(kind, data):
            checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

        prompt_json = json.dumps(graph, ensure_ascii=False)
        workflow_json = json.dumps({"nodes": [{"id": 7}]})
        pixel_payload = b"pixel-payload-must-not-change"
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 640, 832, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"prompt\0" + prompt_json.encode("utf-8"))
            + chunk(b"tEXt", b"workflow\0" + workflow_json.encode("utf-8"))
            + chunk(b"IDAT", pixel_payload)
            + chunk(b"IEND", b"")
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Test_00001_.png"
            path.write_bytes(png)

            preview = inspect_civitai_metadata(path, "Test_00001_.png")
            self.assertEqual("ready", preview["status"])
            self.assertIn("A café at night\nNegative prompt: blurry", preview["parameters"])
            self.assertIn(
                "Steps: 28, Sampler: dpmpp_2m, Schedule type: karras, CFG scale: 6.5, "
                "Seed: 123, Size: 640x832, Model: portrait-v2, VAE: clearvae, "
                "Denoising strength: 0.8",
                preview["parameters"],
            )

            legacy_itxt = b"parameters\0\0\0\0\0" + preview["parameters"].encode("utf-8")
            path.write_bytes(png[:-12] + chunk(b"iTXt", legacy_itxt) + chunk(b"IEND", b""))
            legacy_preview = inspect_civitai_metadata(path, "Test_00001_.png")
            self.assertEqual("ready", legacy_preview["status"])
            self.assertEqual("iTXt", legacy_preview["parameters_chunk"])

            converted = convert_png_to_civitai(path, "Test_00001_.png")
            first_bytes = path.read_bytes()
            metadata = read_png_text_metadata(path)
            self.assertTrue(converted["changed"])
            self.assertEqual(prompt_json, metadata["prompt"])
            self.assertEqual(workflow_json, metadata["workflow"])
            self.assertEqual(preview["parameters"], metadata["parameters"])
            self.assertIn(pixel_payload, first_bytes)
            self.assertIn(b"tEXtparameters\0", first_bytes)
            self.assertNotIn(b"iTXtparameters\0", first_bytes)

            unchanged = convert_png_to_civitai(path, "Test_00001_.png")
            self.assertFalse(unchanged["changed"])
            self.assertEqual("tEXt", unchanged["parameters_chunk"])
            self.assertEqual(first_bytes, path.read_bytes())

        zero_negative_graph = json.loads(json.dumps(graph))
        zero_negative_graph["8"] = {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["1", 0]},
        }
        zero_negative_graph["3"]["inputs"]["negative"] = ["8", 0]
        extracted = extract_generation_data(zero_negative_graph, 640, 832, "Test_00001_.png")
        self.assertEqual("A café at night", extracted["prompt"])
        self.assertEqual("", extracted["negative_prompt"])

    def test_civitai_resources_are_hashed_from_exact_comfy_files(self):
        graph = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "portrait", "clip": ["8", 1]}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["8", 1]}},
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["8", 0],
                    "positive": ["1", 0],
                    "negative": ["2", 0],
                    "seed": 42,
                    "steps": 20,
                    "cfg": 5,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                },
            },
            "4": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["5", 2]}},
            "5": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "sets/model.safetensors"},
            },
            "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "Test", "images": ["4", 0]}},
            "8": {
                "class_type": "Power Lora Loader (rgthree)",
                "inputs": {
                    "model": ["5", 0],
                    "clip": ["5", 1],
                    "lora_1": {
                        "on": True,
                        "lora": "people/style-one.safetensors",
                        "strength": 0.65,
                    },
                    "lora_2": {
                        "on": False,
                        "lora": "people/disabled.safetensors",
                        "strength": 1,
                    },
                },
            },
        }

        def chunk(kind, data):
            checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            models = root / "ComfyUI" / "models"
            checkpoint = models / "checkpoints" / "sets" / "model.safetensors"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_bytes(b"small checkpoint fixture")
            lora = models / "loras" / "people" / "style-one.safetensors"
            lora.parent.mkdir(parents=True)
            payload = b"tensor payload fixture"
            payload_hash = hashlib.sha256(payload).hexdigest()
            header = json.dumps(
                {"__metadata__": {"sshs_model_hash": payload_hash}},
                separators=(",", ":"),
            ).encode("utf-8")
            lora.write_bytes(struct.pack("<Q", len(header)) + header + payload)

            prompt_json = json.dumps(graph, ensure_ascii=False)
            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 768, 8, 2, 0, 0, 0))
                + chunk(b"tEXt", b"prompt\0" + prompt_json.encode("utf-8"))
                + chunk(b"IDAT", b"pixels")
                + chunk(b"IEND", b"")
            )
            image = root / "Test_00001_.png"
            image.write_bytes(png)
            cache = root / "hashes.json"

            preview = inspect_civitai_metadata(image, image.name, root, cache)
            model_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()[:10]
            self.assertEqual(model_hash, preview["extracted"]["model_hash"])
            self.assertEqual(payload_hash[:12], preview["extracted"]["loras"][0]["hash"])
            self.assertIn("<lora:style-one:0.65>", preview["parameters"])
            self.assertIn(f"Model hash: {model_hash}", preview["parameters"])
            self.assertIn(f'Lora hashes: "style-one: {payload_hash[:12]}"', preview["parameters"])
            self.assertNotIn("disabled", preview["parameters"])
            self.assertTrue(cache.is_file())

            converted = convert_png_to_civitai(image, image.name, root, cache)
            self.assertTrue(converted["changed"])
            self.assertEqual(preview["parameters"], read_png_text_metadata(image)["parameters"])


class StorageTests(unittest.TestCase):
    def test_individual_asset_delete_and_image_tool_results(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.sqlite3")
            batch_id = storage.create_batch({"source": "paste", "title": "Edit batch"})
            storage.add_prompts(batch_id, [{"prompt": "portrait"}], 1)
            prompt = storage.pending_prompts(batch_id)[0]
            asset_id = storage.add_asset(
                batch_id,
                prompt["id"],
                "images",
                "edit.png",
                "data/images/edit.png",
                {"vram_relay": {"image_tool_kind": "edit", "edit_instruction": "change hair color"}},
            )
            self.assertEqual(asset_id, storage.image_tool_results()[0]["id"])
            self.assertEqual("change hair color", storage.image_tool_results()[0]["source"]["vram_relay"]["edit_instruction"])
            self.assertEqual("data/images/edit.png", storage.delete_asset(asset_id))
            self.assertIsNone(storage.asset(asset_id))

    def test_recycle_bin_keeps_one_rgba_pixel_per_deleted_image(self):
        with tempfile.TemporaryDirectory() as folder:
            recycle = RecyclePixelBin(Path(folder) / "recycle-bin")
            recycle.add({"id": "one", "filename": "one.png"}, [10, 20, 30, 255])
            summary = recycle.add({"id": "two", "filename": "two.png"}, [200, 150, 100, 128])
            self.assertEqual(2, summary["count"])
            self.assertEqual((512, 512), (summary["width"], summary["height"]))
            self.assertEqual(262144, summary["capacity_per_collage"])
            self.assertEqual(1, summary["collage_count"])
            self.assertEqual(2, len(json.loads(recycle.index_path.read_text(encoding="utf-8"))))
            collage = recycle.collage_path(1).read_bytes()
            self.assertEqual(b"\x89PNG\r\n\x1a\n", collage[:8])
            self.assertEqual((512, 512), struct.unpack(">II", collage[16:24]))
            cursor = 8
            compressed = bytearray()
            while cursor < len(collage):
                length = struct.unpack(">I", collage[cursor : cursor + 4])[0]
                kind = collage[cursor + 4 : cursor + 8]
                data = collage[cursor + 8 : cursor + 8 + length]
                if kind == b"IDAT":
                    compressed.extend(data)
                cursor += 12 + length
            first_row = zlib.decompress(bytes(compressed))[: 1 + (512 * 4)]
            self.assertEqual(bytes([10, 20, 30, 255, 200, 150, 100, 128]), first_row[1:9])

    def test_recycle_bin_starts_a_numbered_canvas_after_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            recycle = RecyclePixelBin(Path(folder) / "recycle-bin")
            recycle.COLLAGE_WIDTH = 2
            recycle.COLLAGE_HEIGHT = 1
            recycle.add({"id": "one"}, [1, 2, 3, 255])
            recycle.add({"id": "two"}, [4, 5, 6, 255])
            summary = recycle.add({"id": "three"}, [7, 8, 9, 255])
            self.assertEqual(2, summary["collage_count"])
            self.assertTrue(summary["collages"][0]["complete"])
            self.assertEqual(1, summary["collages"][1]["pixel_count"])
            self.assertTrue(recycle.collage_path(1).is_file())
            self.assertTrue(recycle.collage_path(2).is_file())

    def test_backfills_only_assets_without_existing_workflow_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.sqlite3")
            batch_id = storage.create_batch({"source": "paste", "title": "Older batch"})
            storage.add_prompts(batch_id, [{"prompt": "hello"}], 1)
            prompt = storage.pending_prompts(batch_id)[0]
            storage.add_asset(
                batch_id,
                prompt["id"],
                "images",
                "old.png",
                "data/images/old.png",
                {"filename": "old.png"},
            )
            storage.add_asset(
                batch_id,
                prompt["id"],
                "images",
                "new.png",
                "data/images/new.png",
                {"vram_relay": {"workflow_name": "already.json"}},
            )
            updated = storage.set_inferred_workflow_provenance(
                batch_id,
                {"workflow_name": "found.json", "inferred_from_metadata": True},
            )
            assets = storage.batch(batch_id)["prompts"][0]["assets"]
            self.assertEqual(1, updated)
            self.assertEqual("found.json", assets[0]["source"]["vram_relay"]["workflow_name"])
            self.assertEqual("already.json", assets[1]["source"]["vram_relay"]["workflow_name"])

    def test_clone_batch_for_rerun_preserves_prompts_negatives_and_run_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.sqlite3")
            original = storage.create_batch({
                "source": "pbi",
                "title": "Original",
                "workflow_id": "workflow-one",
                "workflow_name": "portrait-api.json",
            })
            storage.add_prompts(original, [{
                "guide": "Guide A",
                "prompt": "positive text",
                "negative_prompt": "negative text",
                "target_runs": 3,
            }], 1)

            rerun = storage.clone_batch_for_rerun(original)
            copied = storage.batch(rerun)

            self.assertEqual(original, copied["request"]["rerun_of"])
            self.assertEqual("workflow-one", copied["request"]["workflow_id"])
            self.assertEqual("positive text", copied["prompts"][0]["prompt"])
            self.assertEqual("negative text", copied["prompts"][0]["negative_prompt"])
            self.assertEqual(3, copied["prompts"][0]["target_runs"])
            self.assertEqual(3, copied["total_runs"])

    def test_delete_assets_keeps_job_and_prompts(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.sqlite3")
            batch_id = storage.create_batch({"source": "paste", "title": "Keep job"})
            storage.add_prompts(batch_id, [{"prompt": "hello"}], 1)
            prompt = storage.pending_prompts(batch_id)[0]
            storage.add_asset(batch_id, prompt["id"], "images", "one.png", "data/images/one.png", {})

            paths = storage.delete_assets(batch_id)

            batch = storage.batch(batch_id)
            self.assertEqual(["data/images/one.png"], paths)
            self.assertIsNotNone(batch)
            self.assertEqual(1, len(batch["prompts"]))
            self.assertEqual([], batch["prompts"][0]["assets"])

    def test_queued_batches_survive_restart_in_fifo_order(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite3"
            storage = Storage(path)
            first = storage.create_batch({"source": "paste", "title": "First"})
            second = storage.create_batch({"source": "paste", "title": "Second"})

            reopened = Storage(path)

            self.assertEqual("queued", reopened.batch(first)["status"])
            self.assertEqual("queued", reopened.batch(second)["status"])
            self.assertEqual(first, reopened.next_queued())
            self.assertEqual(2, reopened.queued_count())

    def test_batch_prompt_asset_and_question_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.sqlite3")
            batch_id = storage.create_batch(
                {"source": "paste", "title": "Test", "pasted_prompts": "hello", "images_per_prompt": 1}
            )
            self.assertEqual(1, storage.add_prompts(batch_id, [{"prompt": "hello"}], 2))
            prompt = storage.pending_prompts(batch_id)[0]
            storage.add_asset(batch_id, prompt["id"], "images", "x.png", "data/images/x.png", {"filename": "x.png"})
            storage.increment_run(batch_id, prompt["id"], 123)
            storage.add_question(batch_id, prompt["id"], "why?", "because")
            batch = storage.batch(batch_id)
            self.assertEqual(1, batch["completed_runs"])
            self.assertEqual(1, len(batch["prompts"][0]["assets"]))
            self.assertEqual("because", batch["questions"][0]["answer"])

    def test_asset_star_rating_persists_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite3"
            storage = Storage(path)
            batch_id = storage.create_batch({"source": "paste", "title": "Rated"})
            storage.add_prompts(batch_id, [{"prompt": "hello", "guide": "guide.md"}], 1)
            prompt = storage.pending_prompts(batch_id)[0]
            asset_id = storage.add_asset(
                batch_id, prompt["id"], "images", "rated.png", "data/images/rated.png", {}
            )

            self.assertEqual(5, storage.rate_asset(asset_id, 5)["rating"])
            self.assertEqual(5, Storage(path).batch(batch_id)["prompts"][0]["assets"][0]["rating"])
            self.assertEqual(0, storage.rate_asset(asset_id, 0)["rating"])
            with self.assertRaisesRegex(ValueError, "between 0 and 5"):
                storage.rate_asset(asset_id, 6)

    def test_guide_leaderboard_combines_ratings_across_all_batches(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.sqlite3")
            first_batch = storage.create_batch({"source": "pbi", "title": "First"})
            storage.add_prompts(
                first_batch,
                [
                    {"prompt": "one", "guide": "Guide A.md"},
                    {"prompt": "two", "guide": "Guide B.md"},
                    {"prompt": "three", "guide": "Guide C.md"},
                ],
                1,
            )
            first_prompts = storage.pending_prompts(first_batch)
            first_assets = [
                storage.add_asset(
                    first_batch,
                    prompt["id"],
                    "images",
                    f"{index}.png",
                    f"data/images/{index}.png",
                    {},
                )
                for index, prompt in enumerate(first_prompts, start=1)
            ]
            storage.rate_asset(first_assets[0], 4)
            storage.rate_asset(first_assets[1], 5)

            second_batch = storage.create_batch({"source": "pbi", "title": "Second"})
            storage.add_prompts(
                second_batch,
                [{"prompt": "four", "guide": "  guide a.MD  "}],
                1,
            )
            second_prompt = storage.pending_prompts(second_batch)[0]
            second_asset = storage.add_asset(
                second_batch,
                second_prompt["id"],
                "images",
                "four.png",
                "data/images/four.png",
                {},
            )
            storage.rate_asset(second_asset, 3)

            leaderboard = storage.guide_leaderboard()

            self.assertEqual(3, leaderboard["summary"]["total_guides"])
            self.assertEqual(2, leaderboard["summary"]["ranked_guides"])
            self.assertEqual(2, leaderboard["summary"]["batch_count"])
            self.assertEqual(4, leaderboard["summary"]["total_images"])
            self.assertEqual(3, leaderboard["summary"]["rated_images"])
            self.assertEqual("Guide B.md", leaderboard["guides"][0]["guide"])
            self.assertEqual(5, leaderboard["guides"][0]["average_rating"])
            guide_a = leaderboard["guides"][1]
            self.assertEqual("Guide A.md", guide_a["guide"])
            self.assertEqual(3.5, guide_a["average_rating"])
            self.assertEqual(2, guide_a["batch_count"])
            self.assertEqual(2, guide_a["rated_images"])
            self.assertIsNone(leaderboard["guides"][2]["rank"])

    def test_unfinished_workflow_dependencies_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.sqlite3")
            batch_id = storage.create_batch({
                "source": "paste",
                "title": "Needs workflow",
                "workflow_id": "workflow-one",
            })

            matches = storage.unfinished_batches_for_workflow("workflow-one")

            self.assertEqual(batch_id, matches[0]["id"])
            self.assertEqual([], storage.unfinished_batches_for_workflow("another-workflow"))

    def test_image_dimensions_are_saved_before_lazy_gallery_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image_path = root / "data" / "images" / "sized.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 123, 456)
            )
            storage = Storage(root / "data" / "library.sqlite3")
            batch_id = storage.create_batch({"source": "paste", "title": "Sized"})
            storage.add_prompts(batch_id, [{"prompt": "hello"}], 1)
            prompt = storage.pending_prompts(batch_id)[0]

            storage.add_asset(
                batch_id, prompt["id"], "images", "sized.png", "data/images/sized.png", {}
            )

            self.assertEqual((123, 456), image_dimensions(image_path))
            asset = storage.batch(batch_id)["prompts"][0]["assets"][0]
            self.assertEqual(123, asset["width"])
            self.assertEqual(456, asset["height"])


class ProcessSafetyTests(unittest.TestCase):
    def test_comfy_exit_accepts_only_verified_process_inside_configured_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ComfyUI_portable"
            executable = root / "python_embeded" / "python.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            valid = {
                "ExecutablePath": str(executable),
                "CommandLine": r".\python_embeded\python.exe -s ComfyUI\main.py",
            }
            wrong_folder = {
                "ExecutablePath": str(Path(folder) / "other" / "python.exe"),
                "CommandLine": r"python.exe -s ComfyUI\main.py",
            }
            wrong_command = {
                "ExecutablePath": str(executable),
                "CommandLine": "python.exe unrelated_server.py",
            }

            self.assertTrue(Services._verified_comfy_process(valid, root))
            self.assertFalse(Services._verified_comfy_process(wrong_folder, root))
            self.assertFalse(Services._verified_comfy_process(wrong_command, root))


class StaticUiTests(unittest.TestCase):
    def test_live_prompt_retry_skip_and_lm_regeneration_controls_are_present(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        self.assertIn('id="retryPrompt"', index)
        self.assertIn('id="regeneratePrompt"', index)
        self.assertIn('id="skipPrompt"', index)
        self.assertIn('data-regenerate-library-prompt="${prompt.id}"', script)
        self.assertIn("prompts/${prompt.id}/regenerate", script)
        self.assertIn("current-prompt/${action}", script)
        self.assertIn("current-prompt/(retry|skip|regenerate)", server)
        self.assertIn("prompts/([a-f0-9]+)/regenerate", server)
        self.assertIn("Original prompt before regeneration", script)

    def test_guide_upload_handler_does_not_shadow_the_guide_library(self):
        server = (Path(__file__).resolve().parents[1] / "app" / "server.py").read_text(encoding="utf-8")
        self.assertIn('selected_guides = body.get("guides") or []', server)
        self.assertNotIn('\n                    guides = body.get("guides") or []', server)

    def test_guide_cards_keep_readable_rows_inside_scrolling_library(self):
        styles = (Path(__file__).resolve().parents[1] / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("grid-auto-rows: max-content", styles)
        self.assertIn("align-content: start", styles)

    def test_hidden_guide_model_does_not_block_pasted_prompt_submit(self):
        index = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<select id="promptModel">', index)
        self.assertNotIn('id="promptModel" required', index)

    def test_large_batch_defaults_and_unlimited_guide_label(self):
        index = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="promptsPerGuide" type="number" min="1" step="1" value="4"', index)
        self.assertIn('id="wordLimit" type="number" min="0" step="1" value="300"', index)
        self.assertIn("Select one or more MD guides", index)
        self.assertNotIn("state.selectedGuides.size >= 4", script)
        self.assertNotIn('id="pbiUrl"', index)
        self.assertNotIn('id="pbiPath"', index)

    def test_multiple_workflows_rerun_and_copy_controls_are_present(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="workflowFile" type="file" accept=".json,.png,application/json,image/png" multiple', index)
        self.assertIn('id="batchWorkflow"', index)
        self.assertIn('id="workflowLibrary"', index)
        self.assertIn("Run batch again", script)
        self.assertIn("Copy all prompts", script)
        self.assertIn("data-copy-prompt", script)
        self.assertIn("<strong>Workflow</strong>", script)
        self.assertIn("found in PNG metadata", script)

    def test_live_pause_and_stop_comfy_controls_are_present(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="pauseBatch" type="button" disabled>Pause</button>', index)
        self.assertIn('id="stopComfyNow" type="button">Release GPU memory</button>', index)
        self.assertIn("/api/actions/stop-comfy", script)
        self.assertIn('id="exitComfyApp" type="button">Close ComfyUI</button>', index)
        self.assertIn("/api/actions/exit-comfy", script)

    def test_gallery_is_separate_large_multi_image_view_and_library_is_restored(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-tab="gallery"', index)
        self.assertIn('data-panel="gallery"', index)
        self.assertIn('id="galleryBatchSelect"', index)
        self.assertIn('id="imageDetailDialog"', index)
        self.assertIn('class="prompt-gallery"', script)
        self.assertIn('class="asset-grid"', script)
        self.assertIn('class="gallery-feed"', script)
        self.assertIn('data-gallery-index="${index}"', script)
        self.assertIn(".gallery-feed { columns: 3 360px;", styles)
        self.assertIn(".gallery-feed-image img", styles)
        self.assertIn(".image-detail-layout", styles)

    def test_ratings_dimensions_workflow_delete_and_image_wall_are_present(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="deleteWorkflow"', index)
        self.assertIn("/api/workflows/", script)
        self.assertIn(r'/api/workflows/([a-f0-9]+)', server)
        self.assertIn('id="detailDimensions"', script)
        self.assertIn("naturalWidth", script)
        self.assertIn("data-rating-index", script)
        self.assertIn("Markdown-guide rankings", script)
        self.assertIn('data-tab="wall"', index)
        self.assertIn('data-panel="wall"', index)
        self.assertIn('id="wallBatchSelect"', index)
        self.assertIn('id="imageLightboxDialog"', index)
        self.assertIn(".image-wall-grid", styles)
        self.assertIn(".image-lightbox-dialog", styles)

    def test_gallery_image_tools_and_large_keyboard_image_wall_are_present(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="imageToolWorkflowFile"', index)
        self.assertIn('id="imageToolKind"', index)
        self.assertIn("Send for editing", script)
        self.assertIn("Send for upscale", script)
        self.assertIn("/image-tool", script)
        self.assertIn('path == "/api/image-tool-workflows"', server)
        self.assertIn("run_image_tool", server)
        self.assertIn('event.key === "ArrowLeft"', script)
        self.assertIn('event.key === "ArrowRight"', script)
        self.assertIn(".image-wall-grid { display: grid; grid-template-columns: repeat(var(--wall-cols, 5)", styles)

    def test_edit_instructions_results_gallery_delete_and_recycle_collage_are_present(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="detailEditInstruction"', script)
        self.assertIn('data-tab="results"', index)
        self.assertIn('data-panel="results"', index)
        self.assertIn('data-tab="recycle"', index)
        self.assertIn('data-delete-gallery-index', script)
        self.assertIn('path == "/api/image-tool-results"', server)
        self.assertIn(r'/api/assets/([a-f0-9]+)', server)
        self.assertIn('instruction = str(body.get("instruction")', server)
        self.assertIn("function galleryImageFamily(item)", script)
        self.assertIn('id="previousImageVersion"', script)
        self.assertIn('id="nextImageVersion"', script)
        self.assertIn("Original + edits", script)
        self.assertIn("renderGalleryBatch(refreshed.batch)", script)
        self.assertIn("showImageDetail(resultIndex >= 0 ? resultIndex : sourceIndex)", script)
        self.assertNotIn('closeImageDetail();\n    switchTab("results");', script)
        self.assertIn(".image-detail-version-navigation", styles)

    def test_image_library_folder_carousel_and_fixed_recycle_canvases_are_present(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        recycle = (root / "app" / "recycle.py").read_text(encoding="utf-8")
        self.assertIn('id="openBatchFolder"', script)
        self.assertIn("/open-folder", script)
        self.assertIn(r'/api/batches/([a-f0-9]+)/open-folder', server)
        self.assertIn("data-library-carousel", script)
        self.assertIn("source_asset_id", script)
        self.assertIn("data-carousel-previous", script)
        self.assertIn("data-carousel-next", script)
        self.assertIn(".library-carousel-controls", styles)
        self.assertIn(".library-grid { grid-template-columns: minmax(0, 1fr); }", styles)
        self.assertIn("COLLAGE_WIDTH = 512", recycle)
        self.assertIn("COLLAGE_HEIGHT = 512", recycle)
        self.assertIn('f"recycle-collage-{number:04d}.png"', recycle)

    def test_all_batches_markdown_guide_leaderboard_is_a_separate_page(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-tab="leaderboard"', index)
        self.assertIn('data-panel="leaderboard"', index)
        self.assertIn('id="leaderboardTable"', index)
        self.assertIn('id="leaderboardSort"', index)
        self.assertIn('<option value="coverage">Highest percentage rated</option>', index)
        self.assertIn('<option value="five-stars">Most 5-star ratings</option>', index)
        self.assertIn('sort === "coverage"', script)
        self.assertIn('sort === "five-stars"', script)
        self.assertIn('api("/api/leaderboard")', script)
        self.assertIn('path == "/api/leaderboard"', server)
        self.assertIn(".leaderboard-row", styles)

    def test_gallery_reserves_exact_aspect_ratio_before_lazy_images_load(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        self.assertIn('style="aspect-ratio:${geometry.ratio}"', script)
        self.assertIn('width="${geometry.width}" height="${geometry.height}"', script)
        self.assertIn("storage.backfill_asset_dimensions(ROOT)", server)
        self.assertNotIn(".gallery-feed-image img { display: block; width: 100%; height: auto;", styles)
        self.assertNotIn(".image-wall-tile img { display: block; width: 100%; height: auto;", styles)

    def test_postprocessing_page_uses_deterministic_embedded_comfy_metadata(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        orchestrator = (root / "app" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn('data-tab="postprocess"', index)
        self.assertIn('data-panel="postprocess"', index)
        self.assertIn('id="postprocessBatchSelect"', index)
        self.assertIn('id="convertMetadataBatch"', index)
        self.assertIn("/api/postprocess/assets/", script)
        self.assertIn("/api/postprocess/batches/", script)
        self.assertIn(r'/api/postprocess/assets/([a-f0-9]+)', server)
        self.assertIn(r'/api/postprocess/batches/([a-f0-9]+)', server)
        self.assertIn("convert_png_to_civitai(target", orchestrator)


if __name__ == "__main__":
    unittest.main()
