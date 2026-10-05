import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.references import ReferenceLibrary


class ReferenceLibraryTests(unittest.TestCase):
    def test_original_and_cleaned_reference_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference_root = root / "data" / "references"
            with (
                patch("app.references.ROOT", root),
                patch("app.references.REFERENCE_DIR", reference_root),
                patch("app.references.ORIGINAL_DIR", reference_root / "originals"),
                patch("app.references.PREVIEW_DIR", reference_root / "processed"),
                patch("app.references.CATALOG_PATH", reference_root / "catalog.json"),
                patch("app.references.QUEUE_DIR", root / "data" / "reference-queue"),
            ):
                library = ReferenceLibrary()
                original = b"original-photo-bytes"
                entry = library.save_source(
                    "screen.png", "image", "image/png", io.BytesIO(original), len(original)
                )
                preview = base64.b64encode(b"cleaned-jpeg-bytes").decode("ascii")
                ready = library.save_processed(
                    entry["id"],
                    f"data:image/jpeg;base64,{preview}",
                    1200,
                    800,
                    True,
                    {"top": 40, "right": 0, "bottom": 80, "left": 0},
                )
                self.assertEqual(ready["width"], 1200)
                self.assertTrue((root / ready["source_path"]).is_file())
                self.assertTrue((root / ready["processed_path"]).is_file())
                prompt_image = library.prompt_images(library.selected([entry["id"]]))[0]
                self.assertTrue(prompt_image["dataUrl"].startswith("data:image/jpeg;base64,"))
                library.delete(entry["id"])
                self.assertFalse((root / ready["source_path"]).exists())
                self.assertFalse((root / ready["processed_path"]).exists())

    def test_selected_references_move_to_temporary_queue_and_are_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference_root = root / "data" / "references"
            queue_root = root / "data" / "reference-queue"
            with (
                patch("app.references.ROOT", root),
                patch("app.references.REFERENCE_DIR", reference_root),
                patch("app.references.ORIGINAL_DIR", reference_root / "originals"),
                patch("app.references.PREVIEW_DIR", reference_root / "processed"),
                patch("app.references.CATALOG_PATH", reference_root / "catalog.json"),
                patch("app.references.QUEUE_DIR", queue_root),
            ):
                library = ReferenceLibrary()
                entry = library.save_source("one.png", "image", "image/png", io.BytesIO(b"source"), 6)
                ready = library.save_processed(
                    entry["id"],
                    f"data:image/png;base64,{base64.b64encode(b'processed').decode('ascii')}",
                    640,
                    480,
                    True,
                    {},
                )
                staged = library.stage([entry["id"]])

                self.assertEqual([], library.list())
                self.assertTrue(staged[0]["temporary"])
                self.assertTrue((root / staged[0]["source_path"]).is_file())
                self.assertTrue((root / staged[0]["processed_path"]).is_file())
                self.assertFalse((root / ready["source_path"]).exists())
                self.assertFalse((root / ready["processed_path"]).exists())
                self.assertEqual(1, library.cleanup_staged(staged))
                self.assertFalse((queue_root / staged[0]["queue_id"]).exists())


class WildCatStaticTests(unittest.TestCase):
    def test_reference_modes_video_stills_and_default_ui_cleanup_are_exposed(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app" / "server.py").read_text(encoding="utf-8")
        orchestrator = (root / "app" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("WildCat Harness", index)
        self.assertIn('name="referenceMode" value="match"', index)
        self.assertIn('name="referenceMode" value="inspiration" checked', index)
        self.assertIn('id="removeReferenceUi" type="checkbox" checked', index)
        self.assertIn('accept="image/*,video/*" multiple', index)
        self.assertIn('<details class="reference-composer" id="referenceComposer">', index)
        self.assertNotIn('<details class="reference-composer" id="referenceComposer" open', index)
        self.assertGreater(index.index('id="referenceComposer"'), index.index('id="negativeEnabled"'))
        self.assertIn('id="referenceSummaryCount"', index)
        self.assertIn('id="effectivePromptCount"', index)
        self.assertIn("Every selected image or still is sent separately", script)
        self.assertIn("video.duration * .5", script)
        self.assertNotIn("detectUiEdgeCrop", script)
        self.assertIn("remove_ui_requested = any(entry.get(\"remove_ui\")", orchestrator)
        self.assertIn("user-interface overlays", orchestrator)
        self.assertIn('path == "/api/references/source"', server)
        self.assertIn('"imageMode": "i2i" if reference_mode == "match" else "t2i"', orchestrator)


if __name__ == "__main__":
    unittest.main()
