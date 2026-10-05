from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config, ROOT
from .core import (
    analyze_image_inputs,
    analyze_workflow,
    load_workflow_file,
    parse_numbered_prompts,
    prepare_image_workflow,
    prepare_workflow,
    read_comfy_prompt_graph,
    safe_filename,
    split_pasted_prompts,
    workflow_match_score,
)
from .integrations import IntegrationError, Services
from .metadata import convert_png_to_civitai
from .references import ReferenceLibrary
from .storage import Storage


DATA_DIR = ROOT / "data"
IMAGE_DIR = DATA_DIR / "images"
RESOURCE_HASH_CACHE = DATA_DIR / "resource-hashes.json"


class Cancelled(RuntimeError):
    pass


class PausedRun(RuntimeError):
    pass


class RetryRun(RuntimeError):
    pass


class SkipPrompt(RuntimeError):
    pass


class RegeneratePrompt(RuntimeError):
    def __init__(self, model_id: str = ""):
        super().__init__("Regenerate the current prompt with prompt backend.")
        self.model_id = model_id


class JobManager:
    def __init__(self, storage: Storage, config: Config, services: Services):
        self.storage = storage
        self.config = config
        self.services = services
        self.references = ReferenceLibrary()
        self._state_lock = threading.RLock()
        self._gpu_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._active_batch_id: str | None = None
        self._current_prompt: dict[str, Any] | None = None
        self._prompt_action: dict[str, Any] | None = None
        self._prompt_action_event = threading.Event()
        self._queue_paused = False

    @staticmethod
    def _workflow_name_from_saved_file(path: Path) -> str:
        stem = re.sub(r"-[0-9a-f]{8}-api$", "", path.stem, flags=re.IGNORECASE)
        stem = re.sub(r"-api$", "", stem, flags=re.IGNORECASE)
        return f"{stem}.json"

    def _workflow_metadata_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        known_paths: set[Path] = set()
        for entry in self.workflow_entries():
            path = Path(entry["file"]).resolve()
            known_paths.add(path)
            try:
                graph = load_workflow_file(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            analysis = analyze_workflow(graph)
            candidates.append({
                **entry,
                "path": path,
                "graph": graph,
                "variable_fields": list(entry.get("positive_fields") or [])
                + list(entry.get("negative_fields") or [])
                + list(analysis.get("seed_fields") or []),
                "registered": True,
            })
        workflow_dir = DATA_DIR / "workflows"
        for path in workflow_dir.glob("*.json"):
            resolved = path.resolve()
            if resolved in known_paths:
                continue
            try:
                graph = load_workflow_file(resolved)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            analysis = analyze_workflow(graph)
            candidates.append({
                "id": "",
                "name": self._workflow_name_from_saved_file(path),
                "file": str(resolved),
                "path": resolved,
                "graph": graph,
                "variable_fields": list(analysis.get("recommended_positive") or [])
                + list(analysis.get("recommended_negative") or [])
                + list(analysis.get("seed_fields") or []),
                "registered": False,
            })
        return candidates

    def backfill_workflow_provenance(self) -> int:
        candidates = self._workflow_metadata_candidates()
        if not candidates:
            return 0
        updated = 0
        for batch_id in self.storage.batches_with_unrecorded_assets():
            executed = None
            for asset in self.storage.unrecorded_assets(batch_id):
                path = (ROOT / str(asset["local_path"])).resolve()
                try:
                    path.relative_to(IMAGE_DIR.resolve())
                except ValueError:
                    continue
                if path.is_file():
                    executed = read_comfy_prompt_graph(path)
                if executed:
                    break
            if not executed:
                continue
            ranked = sorted(
                [
                    [
                        workflow_match_score(executed, item["graph"], item["variable_fields"]),
                        1 if item["registered"] else 0,
                        item,
                    ]
                    for item in candidates
                ],
                key=lambda match: (match[0], match[1]),
            )
            best_score = ranked[-1][0]
            if best_score < 0.995:
                continue
            best_matches = [item for score, _registered, item in ranked if abs(score - best_score) < 1e-9]
            names = {str(item["name"]).casefold() for item in best_matches}
            if len(names) != 1:
                continue
            best = sorted(best_matches, key=lambda item: bool(item["registered"]))[-1]
            provenance = {
                "workflow_id": best.get("id") or "",
                "workflow_name": best["name"],
                "workflow_file": best["path"].name,
                "inferred_from_metadata": True,
                "match_confidence": round(best_score, 4),
            }
            updated += self.storage.set_inferred_workflow_provenance(batch_id, provenance)
        return updated

    def active(self) -> dict[str, Any]:
        with self._state_lock:
            alive = bool(self._thread and self._thread.is_alive())
            batch_id = self._active_batch_id if alive else None
            result = {
                "running": alive,
                "batch_id": batch_id,
                "queued_count": self.storage.queued_count(),
            }
            if batch_id:
                batch = self.storage.batch(batch_id, include_details=False)
                if batch:
                    result.update(
                        {
                            "title": batch.get("title"),
                            "status": batch.get("status"),
                            "phase": batch.get("phase"),
                            "completed_runs": batch.get("completed_runs"),
                            "total_runs": batch.get("total_runs"),
                        }
                    )
                if self._current_prompt and self._current_prompt.get("batch_id") == batch_id:
                    current_prompt = dict(self._current_prompt)
                    if self._prompt_action and self._prompt_action.get("prompt_id") == current_prompt.get("id"):
                        current_prompt["action_pending"] = self._prompt_action.get("action")
                    result["current_prompt"] = current_prompt
            return result

    def _launch_locked(self, batch_id: str) -> None:
        self.storage.update_batch(
            batch_id,
            status="running",
            phase="Starting",
            error="",
            paused=0,
            cancel_requested=0,
        )
        self._active_batch_id = batch_id
        self._thread = threading.Thread(
            target=self._run_guarded,
            args=(batch_id,),
            name=f"wildcat-harness-{batch_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def pause_queue(self) -> None:
        with self._state_lock:
            self._queue_paused = True

    def unpause_queue(self) -> None:
        with self._state_lock:
            self._queue_paused = False

    def start(self, batch_id: str) -> dict[str, Any]:
        batch = self.storage.batch(batch_id, include_details=False)
        if not batch:
            raise KeyError(batch_id)
        sibling = self.services.sibling_relay_active() if hasattr(self.services, "sibling_relay_active") else {}
        if sibling.get("running"):
            title = sibling.get("title") or sibling.get("batch_id") or "another batch"
            raise RuntimeError(
                f"VRAM Relay is currently running {title}. Wait for it to finish before starting WildCat Export Edition."
            )
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                if self._active_batch_id == batch_id:
                    return {"queued": False, "active_batch_id": batch_id}
                already_queued = batch.get("status") == "queued"
                self.storage.update_batch(
                    batch_id,
                    status="queued",
                    phase=f"Queued behind {self._active_batch_id[:8] if self._active_batch_id else 'active batch'}",
                    error="",
                    paused=0,
                    cancel_requested=0,
                )
                if not already_queued:
                    self.storage.event(batch_id, "info", "Added to the automatic FIFO queue.")
                return {
                    "queued": True,
                    "active_batch_id": self._active_batch_id,
                    "queue_position": self.storage.batch(batch_id, include_details=False).get("queue_position"),
                }
            already_queued = batch.get("status") == "queued"
            if not already_queued:
                self.storage.update_batch(
                    batch_id,
                    status="queued",
                    phase="Waiting in automatic queue",
                    error="",
                    paused=0,
                    cancel_requested=0,
                )
                self.storage.event(batch_id, "info", "Added to the automatic FIFO queue.")
            oldest = self.storage.next_queued()
            if not oldest:
                raise RuntimeError("The batch could not be added to the queue.")
            if oldest != batch_id:
                self.storage.event(oldest, "info", "Automatically starting the oldest queued batch.")
            self._launch_locked(oldest)
            if oldest == batch_id:
                return {"queued": False, "active_batch_id": batch_id}
            return {
                "queued": True,
                "active_batch_id": oldest,
                "queue_position": self.storage.batch(batch_id, include_details=False).get("queue_position"),
            }

    def queue_batch(self, batch_id: str) -> dict[str, Any]:
        """Add a batch to the FIFO queue without starting it immediately."""
        batch = self.storage.batch(batch_id, include_details=False)
        if not batch:
            raise KeyError(batch_id)
        already_running = batch.get("status") in ("running", "pausing", "paused", "regenerating_prompt")
        if already_running:
            raise RuntimeError("This batch is already running.")
        already_queued = batch.get("status") == "queued"
        if not already_queued:
            self.storage.update_batch(
                batch_id,
                status="queued",
                phase="Waiting in queue",
                error="",
                paused=0,
                cancel_requested=0,
            )
            self.storage.event(batch_id, "info", "Added to the queue (will start when lane is free).")
        with self._state_lock:
            active = bool(self._thread and self._thread.is_alive())
            if not active and not self._queue_paused:
                oldest = self.storage.next_queued()
                if oldest:
                    self.storage.event(oldest, "info", "Automatically starting the oldest queued batch.")
                    self._launch_locked(oldest)
                    if oldest == batch_id:
                        return {"queued": False, "active_batch_id": batch_id}
        return {
            "queued": True,
            "queue_position": self.storage.batch(batch_id, include_details=False).get("queue_position"),
        }

    def start_next_queued(self) -> dict[str, Any] | None:
        with self._state_lock:
            if self._queue_paused:
                return None
            if self._thread and self._thread.is_alive():
                return None
            batch_id = self.storage.next_queued()
            if not batch_id:
                return None
            self.storage.event(batch_id, "info", "Automatically starting the next queued batch.")
            self._launch_locked(batch_id)
            return {"queued": False, "active_batch_id": batch_id}

    def autostart(self) -> None:
        # Run synchronously before the HTTP server accepts page requests. This prevents
        # startup catalog requests from racing a recovered queued worker.
        self.start_next_queued()

    def lm_models_for_ui(self) -> list[dict[str, Any]]:
        with self._gpu_lock:
            if self.active()["running"]:
                # Never launch prompt backend merely to populate a dropdown while ComfyUI may
                # own the GPU lane. If prompt backend is already online, this remains read-only.
                return self.services.lm_models()
            self.services.ensure_lm()
            return self.services.lm_models()

    def workflow_entries(self) -> list[dict[str, Any]]:
        entries = self.config.get("workflows", [])
        if not isinstance(entries, list):
            entries = []
        cleaned = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "ComfyUI API workflow"),
                "file": str(item.get("file") or ""),
                "positive_fields": list(item.get("positive_fields") or []),
                "negative_fields": list(item.get("negative_fields") or []),
            }
            for item in entries
            if isinstance(item, dict) and str(item.get("id") or "") and Path(str(item.get("file") or "")).is_file()
        ]
        if cleaned:
            return cleaned
        legacy_path = str(self.config.get("workflow_file", "")).strip()
        if not legacy_path or not Path(legacy_path).is_file():
            return []
        legacy = {
            "id": uuid.uuid5(uuid.NAMESPACE_URL, str(Path(legacy_path).resolve())).hex,
            "name": str(self.config.get("workflow_name", "") or Path(legacy_path).name),
            "file": str(Path(legacy_path).resolve()),
            "positive_fields": list(self.config.get("workflow_positive_fields", [])),
            "negative_fields": list(self.config.get("workflow_negative_fields", [])),
        }
        self.config.update({"workflows": [legacy], "active_workflow_id": legacy["id"]})
        return [legacy]

    def workflow_entry(self, workflow_id: str = "") -> dict[str, Any] | None:
        entries = self.workflow_entries()
        if not entries:
            return None
        selected_id = str(workflow_id or self.config.get("active_workflow_id", "")).strip()
        return next((item for item in entries if item["id"] == selected_id), entries[0])

    def workflow_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        entries = self.workflow_entries()
        active_id = str(self.config.get("active_workflow_id", "")).strip()
        for entry in entries:
            item = dict(entry)
            item["active"] = entry["id"] == active_id
            item["analysis"] = analyze_workflow(load_workflow_file(entry["file"]))
            catalog.append(item)
        return catalog

    def image_tool_entries(self) -> list[dict[str, Any]]:
        entries = self.config.get("image_tool_workflows", [])
        if not isinstance(entries, list):
            return []
        normalized = []
        for item in entries:
            if not (
                isinstance(item, dict)
                and str(item.get("id") or "")
                and str(item.get("kind") or "") in {"edit", "upscale"}
                and Path(str(item.get("file") or "")).is_file()
            ):
                continue
            entry = {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "Image API workflow"),
                "file": str(item.get("file") or ""),
                "kind": str(item.get("kind") or "edit"),
                "image_fields": list(item.get("image_fields") or []),
                "positive_fields": list(item.get("positive_fields") or []),
            }
            if entry["kind"] == "edit" and not entry["positive_fields"]:
                entry["positive_fields"] = analyze_workflow(load_workflow_file(entry["file"]))["recommended_positive"]
            normalized.append(entry)
        return normalized

    def image_tool_catalog(self) -> list[dict[str, Any]]:
        catalog = []
        for entry in self.image_tool_entries():
            item = dict(entry)
            graph = load_workflow_file(entry["file"])
            fields = analyze_image_inputs(graph)
            item["image_inputs"] = fields
            item["text_inputs"] = analyze_workflow(graph)["fields"]
            item["node_count"] = len(graph)
            catalog.append(item)
        return catalog

    def image_tool_entry(self, workflow_id: str) -> dict[str, Any] | None:
        return next((item for item in self.image_tool_entries() if item["id"] == workflow_id), None)

    def _wait_for_image_tool(self, prompt_id: str) -> dict[str, Any]:
        timeout_seconds = max(60, int(self.config.get("comfy_job_timeout_minutes", 180)) * 60)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            history = self.services.comfy_history(prompt_id)
            record = history.get(prompt_id) if isinstance(history, dict) else None
            if record:
                status = record.get("status", {}) if isinstance(record, dict) else {}
                if status.get("status_str") == "error":
                    messages = status.get("messages") or []
                    raise IntegrationError(f"ComfyUI execution failed: {json.dumps(messages)[-1600:]}")
                if record.get("outputs") is not None:
                    return record
            time.sleep(2)
        raise IntegrationError(f"ComfyUI job {prompt_id} exceeded the configured timeout.")

    def run_image_tool(
        self,
        asset: dict[str, Any],
        workflow_id: str,
        source_path: Path,
        instruction: str = "",
    ) -> list[dict[str, Any]]:
        entry = self.image_tool_entry(workflow_id)
        if not entry:
            raise ValueError("Choose a saved editing or upscaling ComfyUI API workflow.")
        instruction = str(instruction or "").strip()
        if entry["kind"] == "edit" and not instruction:
            raise ValueError("Enter a short edit instruction, such as ‘remove water’ or ‘change her hair color’.")
        if self.active()["running"]:
            raise RuntimeError("Wait for the active relay batch to finish before editing or upscaling an image.")
        with self._gpu_lock:
            if self.active()["running"]:
                raise RuntimeError("A relay batch took the GPU lane. Try the image action again when it finishes.")
            try:
                try:
                    self.services.unload_all_lm()
                except IntegrationError:
                    pass
                self.services.ensure_comfy()
                uploaded_name = self.services.upload_comfy_input(source_path)
                base = load_workflow_file(entry["file"])
                workflow = prepare_image_workflow(
                    base,
                    uploaded_name,
                    entry["image_fields"],
                    instruction,
                    entry["positive_fields"],
                )
                prompt_id = self.services.submit_comfy(
                    workflow,
                    f"wildcat-harness-image-tool-{uuid.uuid4().hex}",
                )
                record = self._wait_for_image_tool(prompt_id)
                outputs = self._outputs(record)
                if not outputs:
                    raise IntegrationError("The ComfyUI workflow completed without an image output.")
                destination = IMAGE_DIR / str(asset["batch_id"])
                destination.mkdir(parents=True, exist_ok=True)
                saved: list[dict[str, Any]] = []
                stamp = int(time.time())
                for index, (kind, output) in enumerate(outputs, start=1):
                    content, _content_type = self.services.download_comfy_output(output)
                    source_name = safe_filename(str(output.get("filename") or f"output-{index}.png"))
                    filename = (
                        f"{int(asset.get('position') or 0):05d}-{entry['kind']}-{stamp}-{index:02d}-{source_name}"
                    )
                    target = destination / filename
                    target.write_bytes(content)
                    source = dict(output)
                    source["vram_relay"] = {
                        "workflow_id": entry["id"],
                        "workflow_name": entry["name"],
                        "workflow_file": Path(entry["file"]).name,
                        "image_tool_kind": entry["kind"],
                        "source_asset_id": asset["id"],
                        "edit_instruction": instruction,
                    }
                    try:
                        metadata_result = convert_png_to_civitai(
                            target,
                            str(output.get("filename") or source_name),
                            self.config.get("comfy_path"),
                            RESOURCE_HASH_CACHE,
                        )
                        source["vram_relay"]["civitai_metadata"] = metadata_result["status"]
                    except Exception:
                        source["vram_relay"]["civitai_metadata"] = "error"
                    asset_id = self.storage.add_asset(
                        str(asset["batch_id"]),
                        str(asset["prompt_id"]),
                        kind,
                        filename,
                        target.relative_to(ROOT).as_posix(),
                        source,
                    )
                    created = self.storage.asset(asset_id)
                    if created:
                        saved.append(created)
                return saved
            finally:
                try:
                    self.services.free_comfy()
                except Exception:
                    pass

    def run_inpaint(
        self,
        asset: dict[str, Any],
        workflow_id: str,
        source_path: Path,
        instruction: str = "",
        mask_data_url: str = "",
        denoise: float = 0.75,
    ) -> list[dict[str, Any]]:
        entry = self.image_tool_entry(workflow_id)
        if not entry:
            raise ValueError("Choose a saved editing ComfyUI API workflow for inpainting.")
        instruction = str(instruction or "").strip()
        if not instruction:
            raise ValueError("Enter a short edit instruction for the masked area.")
        if not mask_data_url:
            raise ValueError("Paint a mask area on the image first.")
        if self.active()["running"]:
            raise RuntimeError("Wait for the active relay batch to finish before inpainting.")
        sibling = self.services.sibling_relay_active() if hasattr(self.services, "sibling_relay_active") else {}
        if sibling.get("running"):
            title = sibling.get("title") or sibling.get("batch_id") or "another batch"
            raise RuntimeError(
                f"VRAM Relay is currently running {title}. Wait for it to finish before inpainting."
            )
        import base64 as b64mod
        import importlib
        import io
        pil_image_mod = importlib.import_module("PIL.Image")
        mask_header, mask_b64 = mask_data_url.split(",", 1) if "," in mask_data_url else ("", mask_data_url)
        if mask_header and not str(mask_header).startswith("data:image/"):
            raise ValueError("The painted mask must be an image data URL. Repaint the mask and try again.")
        try:
            mask_bytes = b64mod.b64decode(mask_b64)
        except (ValueError, TypeError) as error:
            raise ValueError("The painted mask data is invalid. Repaint the mask and try again.") from error
        base = load_workflow_file(entry["file"])
        loader_ids = {str(field_id).rsplit(":", 1)[0] for field_id in (entry.get("image_fields") or [])}
        mask_mode = "none"
        for node in base.values():
            class_type = re.sub(r"[^a-z]", "", str(node.get("class_type") or "").lower())
            if "loadimagemask" in class_type:
                mask_mode = "alpha"
                break
        if mask_mode == "none":
            for node in base.values():
                for value in node.get("inputs", {}).values():
                    if isinstance(value, list) and len(value) >= 2 and str(value[0]) in loader_ids and value[1] == 1:
                        mask_mode = "inverted"
                        break
                if mask_mode != "none":
                    break
        if mask_mode == "none":
            raise ValueError(
                "This API workflow does not consume a mask. In ComfyUI, add a LoadImageMask node that reads "
                "the uploaded image and feed its mask into the sampler, then export Workflow (API) again."
            )
        denoise_targets = []
        for node_id, node in base.items():
            for input_name, value in node.get("inputs", {}).items():
                lowered = str(input_name).lower()
                if lowered == "denoise" and isinstance(value, (int, float)) and not isinstance(value, bool):
                    denoise_targets.append((str(node_id), str(input_name)))
        temp_dir = IMAGE_DIR / "_temp_inpaint"
        temp_path = temp_dir / f"inpaint-{uuid.uuid4().hex}.png"
        mask_img = None
        source_img = None
        rgba_img = None
        try:
            mask_img = pil_image_mod.open(io.BytesIO(mask_bytes)).convert("L")
            source_img = pil_image_mod.open(str(source_path)).convert("RGB")
            if mask_img.size != source_img.size:
                resized = mask_img.resize(source_img.size, pil_image_mod.LANCZOS)
                mask_img.close()
                mask_img = resized
            if mask_mode == "inverted":
                # ComfyUI's LoadImage mask output is (1 - alpha); invert so painted pixels regenerate.
                mask_img = mask_img.point(lambda value: 255 - value)
            rgba_img = pil_image_mod.new("RGBA", source_img.size)
            rgba_img.paste(source_img, (0, 0))
            rgba_img.putalpha(mask_img)
            temp_dir.mkdir(parents=True, exist_ok=True)
            rgba_img.save(str(temp_path), "PNG")
        except Exception as error:
            raise ValueError(f"Could not prepare the masked image: {error}") from error
        finally:
            for handle in (mask_img, source_img, rgba_img):
                try:
                    if handle is not None:
                        handle.close()
                except Exception:
                    pass
        with self._gpu_lock:
            if self.active()["running"]:
                raise RuntimeError("A relay batch took the GPU lane. Try inpainting when it finishes.")
            try:
                try:
                    self.services.unload_all_lm()
                except IntegrationError:
                    pass
                self.services.ensure_comfy()
                uploaded_name = self.services.upload_comfy_input(temp_path)
                workflow = prepare_image_workflow(
                    base,
                    uploaded_name,
                    entry["image_fields"],
                    instruction,
                    entry["positive_fields"],
                )
                for node_id, input_name in denoise_targets:
                    if node_id in workflow and input_name in workflow[node_id].get("inputs", {}):
                        workflow[node_id]["inputs"][input_name] = max(0.0, min(1.0, float(denoise)))
                prompt_id = self.services.submit_comfy(
                    workflow,
                    f"wildcat-harness-inpaint-{uuid.uuid4().hex}",
                )
                record = self._wait_for_image_tool(prompt_id)
                outputs = self._outputs(record)
                if not outputs:
                    raise IntegrationError("The ComfyUI inpainting workflow completed without an image output.")
                destination = IMAGE_DIR / str(asset["batch_id"])
                destination.mkdir(parents=True, exist_ok=True)
                saved: list[dict[str, Any]] = []
                stamp = int(time.time())
                for index, (kind, output) in enumerate(outputs, start=1):
                    content, _content_type = self.services.download_comfy_output(output)
                    source_name = safe_filename(str(output.get("filename") or f"output-{index}.png"))
                    filename = (
                        f"{int(asset.get('position') or 0):05d}-inpaint-{stamp}-{index:02d}-{source_name}"
                    )
                    target = destination / filename
                    target.write_bytes(content)
                    source = dict(output)
                    source["vram_relay"] = {
                        "workflow_id": entry["id"],
                        "workflow_name": entry["name"],
                        "workflow_file": Path(entry["file"]).name,
                        "image_tool_kind": "edit",
                        "source_asset_id": asset["id"],
                        "edit_instruction": f"[inpaint mask] {instruction}",
                    }
                    try:
                        metadata_result = convert_png_to_civitai(
                            target,
                            str(output.get("filename") or source_name),
                            self.config.get("comfy_path"),
                            RESOURCE_HASH_CACHE,
                        )
                        source["vram_relay"]["civitai_metadata"] = metadata_result["status"]
                    except Exception:
                        source["vram_relay"]["civitai_metadata"] = "error"
                    asset_id = self.storage.add_asset(
                        str(asset["batch_id"]),
                        str(asset["prompt_id"]),
                        kind,
                        filename,
                        target.relative_to(ROOT).as_posix(),
                        source,
                    )
                    created = self.storage.asset(asset_id)
                    if created:
                        saved.append(created)
                return saved
            finally:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    temp_dir.rmdir()
                except Exception:
                    pass
                try:
                    self.services.free_comfy()
                except Exception:
                    pass

    def _phase(self, batch_id: str, phase: str, message: str | None = None) -> None:
        self.storage.update_batch(batch_id, status="running", phase=phase)
        if message:
            self.storage.event(batch_id, "info", message)

    def _check_cancelled(self, batch_id: str) -> None:
        controls = self.storage.controls(batch_id)
        if controls["cancel_requested"]:
            raise Cancelled("Batch cancelled by user.")
        if controls["paused"]:
            self._wait_if_paused(batch_id)

    def _wait_if_paused(self, batch_id: str) -> None:
        announced = False
        while True:
            controls = self.storage.controls(batch_id)
            if controls["cancel_requested"]:
                raise Cancelled("Batch cancelled by user.")
            if not controls["paused"]:
                if announced:
                    self.storage.update_batch(batch_id, status="running", phase="Resuming image queue")
                    self.storage.event(batch_id, "info", "Image queue resumed.")
                return
            if not announced:
                self.storage.update_batch(batch_id, status="paused", phase="Paused between ComfyUI runs")
                self.storage.event(batch_id, "info", "Paused safely between ComfyUI runs.")
                announced = True
            time.sleep(1)

    def _run_guarded(self, batch_id: str) -> None:
        try:
            with self._gpu_lock:
                self._run(batch_id)
        except Cancelled as error:
            self.storage.update_batch(batch_id, status="cancelled", phase="Cancelled", error=str(error))
            self.storage.event(batch_id, "warning", str(error))
            try:
                self.services.interrupt_comfy()
            except IntegrationError:
                pass
        except Exception as error:  # noqa: BLE001 - persist all job failures for the UI
            self.storage.update_batch(batch_id, status="failed", phase="Stopped with an error", error=str(error))
            self.storage.event(batch_id, "error", str(error))
        finally:
            # A failed handoff must not leave either model family occupying VRAM.
            try:
                self.services.unload_all_lm()
            except Exception:
                pass
            try:
                self.services.free_comfy()
            except Exception:
                pass
            try:
                request = self.storage.request_for(batch_id)
                staged_references = request.get("references") or []
                if staged_references:
                    finished = self.storage.batch(batch_id, include_details=False)
                else:
                    finished = None
                if finished and finished.get("status") in {"complete", "complete_with_errors"}:
                    removed = self.references.cleanup_staged(staged_references)
                    if removed:
                        self.storage.event(
                            batch_id,
                            "info",
                            f"Deleted {removed} temporary reference queue after the batch completed.",
                        )
            except Exception as cleanup_error:  # noqa: BLE001 - cleanup must not change a finished result
                self.storage.event(batch_id, "warning", f"Temporary reference cleanup needs attention: {cleanup_error}")
            with self._state_lock:
                self._current_prompt = None
                self._prompt_action = None
                self._prompt_action_event.clear()
                if self._thread is threading.current_thread():
                    self._active_batch_id = None
                    self._thread = None
                self.start_next_queued()

    def _run(self, batch_id: str) -> None:
        request = self.storage.request_for(batch_id)
        source = request.get("source", "paste")
        target_runs = max(1, int(request.get("images_per_prompt", 1)))

        if source in {"guides", "pbi"} and not self.storage.pending_prompts(batch_id):
            self._generate_guided_prompts(batch_id, request, target_runs)
        elif source == "paste" and not self.storage.pending_prompts(batch_id):
            prompts = split_pasted_prompts(str(request.get("pasted_prompts") or ""))
            if not prompts:
                raise ValueError("Paste at least one image prompt.")
            self.storage.add_prompts(batch_id, prompts, target_runs)
            self.storage.event(batch_id, "info", f"Imported {len(prompts)} prompts.")

        batch = self.storage.batch(batch_id)
        if not batch or not batch.get("prompts"):
            raise ValueError("No prompts were generated or imported.")

        self._check_cancelled(batch_id)
        # This covers imported prompts and resumes: prompt backend must be out before ComfyUI loads.
        self._phase(batch_id, "Handoff: unloading prompt backend", "Unloading prompt backend before ComfyUI starts.")
        try:
            self.services.unload_all_lm()
        except IntegrationError:
            # An offline prompt backend server cannot be holding a loaded inference model.
            pass

        workflow_entry = self.workflow_entry(str(request.get("workflow_id") or ""))
        if not workflow_entry:
            raise ValueError("Upload and map a ComfyUI API-format workflow in Settings first.")
        request["workflow_id"] = workflow_entry["id"]
        request["workflow_name"] = workflow_entry["name"]
        base_workflow = load_workflow_file(workflow_entry["file"])
        positive_fields = list(workflow_entry.get("positive_fields") or [])
        negative_fields = list(workflow_entry.get("negative_fields") or [])

        self._phase(
            batch_id,
            f"Preparing ComfyUI · {workflow_entry['name']}",
            f"prompt backend is unloaded. Preparing ComfyUI with {workflow_entry['name']}.",
        )
        self.services.ensure_comfy()
        self._run_comfy_queue(
            batch_id,
            base_workflow,
            positive_fields,
            negative_fields,
            workflow_entry,
        )

    @staticmethod
    def _guide_groups(guides: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        if not guides:
            raise ValueError("Choose at least one WildCat Markdown guide.")
        groups: list[list[dict[str, Any]]] = []
        cursor = 0
        while cursor < len(guides):
            remaining = len(guides) - cursor
            if remaining <= 4:
                size = remaining
            elif remaining == 5:
                size = 3
            else:
                size = 4
            groups.append(guides[cursor : cursor + size])
            cursor += size
        return groups

    def _generate_guided_prompts(self, batch_id: str, request: dict[str, Any], target_runs: int) -> None:
        guides = request.get("guides") or []
        guide_groups = self._guide_groups(guides)
        base_target = max(1, int(request.get("prompts_per_guide", 1)))
        model_id = str(request.get("model") or self.config.get("prompt_model", "")).strip()
        brief = str(request.get("brief") or "").strip()
        reference_entries = request.get("references") if isinstance(request.get("references"), list) else []
        reference_images = self.references.prompt_images(reference_entries)
        reference_mode = str(request.get("reference_mode") or "inspiration")
        remove_ui_requested = any(entry.get("remove_ui") for entry in reference_entries)
        if reference_images:
            if reference_mode == "match":
                reference_direction = (
                    "Use the supplied cleaned reference media as the primary visual specification. "
                    "Reverse-engineer a generation prompt that matches the subjects, composition, camera angle, "
                    "lighting, colors, materials, spatial relationships, and overall appearance as closely as possible. "
                    "WildCat supplies one reference at a time so every uploaded image is used explicitly."
                )
            else:
                reference_direction = (
                    "Use the supplied cleaned reference media as a mood board for inspiration. Extract useful visual ideas "
                    "such as palette, atmosphere, styling, materials, lighting, and composition, while creating a clearly new "
                    "scene rather than copying a specific identity or exact layout."
                )
            cleanup_direction = (
                "Ignore and exclude all app chrome, browser bars, captions, subtitles, watermarks, logos, playback controls, "
                "progress bars, buttons, borders, notification badges, and other user-interface overlays that may remain."
            ) if remove_ui_requested else ""
            brief = "\n\n".join(part for part in (brief, reference_direction, cleanup_direction) if part)
        if not brief:
            raise ValueError("Describe the image batch or add reference media.")

        self._phase(batch_id, "Preparing prompt backend", "Freeing ComfyUI memory before prompt generation.")
        try:
            self.services.free_comfy()
        except IntegrationError:
            # Offline ComfyUI cannot be holding a loaded model, so do not launch it.
            pass
        self._check_cancelled(batch_id)

        self.services.ensure_lm()
        self._phase(batch_id, "Loading prompt backend model", f"Loading {model_id} in prompt backend.")
        runtime_model_id = self.services.load_lm(model_id)
        self.storage.event(
            batch_id,
            "info",
            "prompt backend prompt generation is using up to 4 parallel requests; responses are saved in their original guide order.",
        )
        if reference_images:
            self.storage.event(
                batch_id,
                "info",
                f"Using {len(reference_images)} cleaned reference image(s) in {reference_mode} mode.",
            )

        try:
            counts = self.storage.guide_prompt_counts(batch_id)
            reference_counts = self.storage.guide_reference_prompt_counts(batch_id)

            def fill_group_target(
                guide_group: list[dict[str, Any]],
                group_number: int,
                desired: int,
                progress: dict[str, int],
                images: list[dict[str, str]],
                reference_entry: dict[str, Any] | None = None,
                reference_number: int = 0,
            ) -> None:
                attempt = 0
                while any(progress.get(str(guide.get("name", "")), 0) < desired for guide in guide_group):
                    self._check_cancelled(batch_id)
                    attempt += 1
                    if attempt > max(10, (desired // 20 + 1) * 4):
                        raise IntegrationError("prompt backend repeatedly returned fewer prompts than requested.")
                    remaining = {
                        str(guide.get("name", "")): max(
                            0, desired - progress.get(str(guide.get("name", "")), 0)
                        )
                        for guide in guide_group
                    }
                    chunk = min(20, max(remaining.values()))
                    group_counts = [progress.get(str(guide.get("name", "")), 0) for guide in guide_group]
                    segment = (min(group_counts) // 20) + 1
                    if reference_entry:
                        reference_name = str(reference_entry.get("name") or f"Reference {reference_number}")
                        segment_brief = (
                            f"{brief}\n\nReference {reference_number} of {len(reference_images)}: {reference_name}. "
                            "Use only the single attached reference for this request. Create distinct prompt ideas for it "
                            f"and do not repeat earlier variations. Variation segment {segment}."
                        )
                        phase = (
                            f"Reference {reference_number}/{len(reference_images)} · "
                            f"guide group {group_number}/{len(guide_groups)}, segment {segment}"
                        )
                        message = (
                            f"Using temporary reference {reference_number}/{len(reference_images)} ({reference_name}) "
                            f"to generate up to {chunk} prompt(s) for {len(guide_group)} guides."
                        )
                    else:
                        segment_brief = (
                            f"{brief}\n\nVariation segment {segment}. Create new, distinct prompt ideas and do not repeat earlier variations."
                        )
                        phase = f"Generating guide group {group_number}/{len(guide_groups)}, segment {segment}"
                        message = f"WildCat is generating up to {chunk} prompts for {len(guide_group)} guides."
                    self._phase(
                        batch_id,
                        phase,
                        message,
                    )
                    response = self.services.generate_guided_prompts(
                        {
                            "model": runtime_model_id,
                            "prompt": segment_brief,
                            "guides": guide_group,
                            "images": images,
                            "imageMode": "i2i" if reference_mode == "match" else "t2i",
                            "removeUi": remove_ui_requested,
                            "negativeEnabled": bool(request.get("negative_enabled", False)),
                            "count": chunk,
                            "wordLimit": request.get("word_limit", ""),
                            "temperature": request.get("temperature", 0.7),
                        }
                    )
                    added = 0
                    for result in response.get("results", []):
                        guide_name = str(result.get("guide") or "Untitled guide")
                        needed = max(0, desired - progress.get(guide_name, 0))
                        if needed <= 0:
                            continue
                        if result.get("error"):
                            self.storage.event(batch_id, "warning", f"{guide_name}: {result['error']}")
                            continue
                        parsed = parse_numbered_prompts(str(result.get("output") or ""))[:needed]
                        for item in parsed:
                            item["guide"] = guide_name
                            if reference_entry:
                                item["reference_id"] = str(reference_entry.get("id") or "")
                                item["reference_name"] = str(reference_entry.get("name") or "")
                        added += self.storage.add_prompts(batch_id, parsed, target_runs)
                        progress[guide_name] = progress.get(guide_name, 0) + len(parsed)
                        if progress is not counts:
                            counts[guide_name] = counts.get(guide_name, 0) + len(parsed)
                    if not added:
                        errors = "; ".join(
                            f"{result.get('guide')}: {result['error']}"
                            for result in response.get("results", [])
                            if result.get("error")
                        )
                        raise IntegrationError(
                            "WildCat's guide engine returned no usable numbered prompts."
                            + (f" {errors}" if errors else "")
                        )

            if reference_images:
                reference_total = len(reference_images)
                extra_each, extra_remainder = divmod(base_target, reference_total)
                for group_number, guide_group in enumerate(guide_groups, start=1):
                    for reference_index, (reference_entry, reference_image) in enumerate(
                        zip(reference_entries, reference_images), start=1
                    ):
                        desired = 1 + extra_each + (1 if reference_index <= extra_remainder else 0)
                        progress = {
                            str(guide.get("name", "")): reference_counts.get(
                                (str(guide.get("name", "")), str(reference_entry.get("id") or "")), 0
                            )
                            for guide in guide_group
                        }
                        fill_group_target(
                            guide_group,
                            group_number,
                            desired,
                            progress,
                            [reference_image],
                            reference_entry,
                            reference_index,
                        )
                        for guide_name, generated in progress.items():
                            reference_counts[(guide_name, str(reference_entry.get("id") or ""))] = generated
                effective_target = base_target + reference_total
                self.storage.event(
                    batch_id,
                    "info",
                    f"Prompt phase complete: all {reference_total} references were used separately; "
                    f"{effective_target} prompts per guide, {sum(counts.values())} total.",
                )
            else:
                for group_number, guide_group in enumerate(guide_groups, start=1):
                    fill_group_target(guide_group, group_number, base_target, counts, [])
                self.storage.event(batch_id, "info", f"Prompt phase complete with {sum(counts.values())} prompts.")
        finally:
            self._phase(batch_id, "Handoff: unloading prompt backend", "Prompt phase complete. Unloading prompt backend from VRAM.")
            self.services.unload_all_lm()

    def _set_current_prompt(self, batch_id: str, prompt: dict[str, Any], run_number: int) -> None:
        with self._state_lock:
            self._current_prompt = {
                "batch_id": batch_id,
                "id": str(prompt["id"]),
                "position": int(prompt["position"]),
                "run_number": int(run_number),
                "target_runs": int(prompt["target_runs"]),
            }

    def _clear_current_prompt(self, batch_id: str, prompt_id: str) -> None:
        with self._state_lock:
            if (
                self._current_prompt
                and self._current_prompt.get("batch_id") == batch_id
                and self._current_prompt.get("id") == prompt_id
            ):
                self._current_prompt = None
            if self._prompt_action and self._prompt_action.get("prompt_id") == prompt_id:
                self._prompt_action = None
                self._prompt_action_event.clear()

    def _raise_prompt_action(self, batch_id: str, prompt_id: str) -> None:
        with self._state_lock:
            action = self._prompt_action
            if not action or action.get("batch_id") != batch_id or action.get("prompt_id") != prompt_id:
                return
            self._prompt_action = None
            self._prompt_action_event.clear()
        if action["action"] == "retry":
            raise RetryRun("Retry the current prompt with a new seed.")
        if action["action"] == "skip":
            raise SkipPrompt("Skip the current prompt.")
        raise RegeneratePrompt(str(action.get("model_id") or ""))

    def prompt_action(self, batch_id: str, action: str, model_id: str = "") -> dict[str, Any]:
        if action not in {"retry", "skip", "regenerate"}:
            raise ValueError("Choose retry, skip, or regenerate for the current prompt.")
        with self._state_lock:
            alive = bool(self._thread and self._thread.is_alive())
            current = dict(self._current_prompt) if self._current_prompt else None
            if not alive or self._active_batch_id != batch_id:
                raise ValueError("Only the currently running batch can change its current prompt.")
            if not current or current.get("batch_id") != batch_id:
                raise ValueError("WildCat is not currently inside a ComfyUI prompt. Wait until a prompt is running.")
            if self._prompt_action:
                raise ValueError("A prompt action is already being applied.")
            self._prompt_action = {
                "batch_id": batch_id,
                "prompt_id": current["id"],
                "action": action,
                "model_id": str(model_id or "").strip(),
            }
            self._prompt_action_event.set()
        labels = {
            "retry": "retry with a new seed",
            "skip": "skip and continue to the next prompt",
            "regenerate": "rewrite with prompt backend, then retry",
        }
        self.storage.update_batch(
            batch_id,
            phase=f"Prompt {current['position']}: preparing to {labels[action]}",
        )
        self.storage.event(
            batch_id,
            "warning" if action == "skip" else "info",
            f"Prompt {current['position']} requested to {labels[action]}. Interrupting its current ComfyUI run.",
        )
        try:
            self.services.interrupt_comfy()
        except IntegrationError:
            pass
        return current

    def _wait_for_comfy(self, batch_id: str, prompt_id: str, storage_prompt_id: str) -> dict[str, Any]:
        timeout_seconds = max(60, int(self.config.get("comfy_job_timeout_minutes", 180)) * 60)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            self._raise_prompt_action(batch_id, storage_prompt_id)
            controls = self.storage.controls(batch_id)
            if controls["cancel_requested"]:
                try:
                    self.services.interrupt_comfy()
                finally:
                    raise Cancelled("Batch cancelled while ComfyUI was generating.")
            if controls["paused"]:
                try:
                    self.services.interrupt_comfy()
                except IntegrationError:
                    pass
                raise PausedRun("Current ComfyUI run interrupted for pause.")
            history = self.services.comfy_history(prompt_id)
            record = history.get(prompt_id) if isinstance(history, dict) else None
            if record:
                status = record.get("status", {}) if isinstance(record, dict) else {}
                if status.get("status_str") == "error":
                    messages = status.get("messages") or []
                    raise IntegrationError(f"ComfyUI execution failed: {json.dumps(messages)[-1600:]}")
                if record.get("outputs") is not None:
                    return record
            self._prompt_action_event.wait(2)
        raise IntegrationError(f"ComfyUI job {prompt_id} exceeded the configured timeout.")

    @staticmethod
    def _outputs(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        found: list[tuple[str, dict[str, Any]]] = []
        for node_output in (record.get("outputs") or {}).values():
            if not isinstance(node_output, dict):
                continue
            for kind in ("images", "gifs", "videos", "audio"):
                items = node_output.get(kind) or []
                if isinstance(items, list):
                    found.extend((kind, item) for item in items if isinstance(item, dict) and item.get("filename"))
        return found

    def _save_outputs(
        self,
        batch_id: str,
        prompt: dict[str, Any],
        run_number: int,
        outputs: list[tuple[str, dict[str, Any]]],
        workflow_entry: dict[str, Any],
    ) -> None:
        destination = IMAGE_DIR / batch_id
        destination.mkdir(parents=True, exist_ok=True)
        for index, (kind, output) in enumerate(outputs, start=1):
            content, _content_type = self.services.download_comfy_output(output)
            source_name = safe_filename(str(output.get("filename") or f"output-{index}.png"))
            filename = f"{int(prompt['position']):05d}-run{run_number:03d}-{index:02d}-{source_name}"
            target = destination / filename
            target.write_bytes(content)
            relative = target.relative_to(ROOT).as_posix()
            source = dict(output)
            source["vram_relay"] = {
                "workflow_id": workflow_entry["id"],
                "workflow_name": workflow_entry["name"],
                "workflow_file": Path(workflow_entry["file"]).name,
            }
            try:
                metadata_result = convert_png_to_civitai(target,
                    str(output.get("filename") or source_name),
                    self.config.get("comfy_path"),
                    RESOURCE_HASH_CACHE,
                )
                source["vram_relay"]["civitai_metadata"] = metadata_result["status"]
            except Exception as error:  # noqa: BLE001 - metadata must not discard a completed image
                source["vram_relay"]["civitai_metadata"] = "error"
                self.storage.event(
                    batch_id,
                    "warning",
                    f"Saved {filename}, but Civitai metadata conversion failed: {error}",
                )
            self.storage.add_asset(
                batch_id,
                prompt["id"],
                kind,
                filename,
                relative,
                source,
            )

    def _regenerate_prompt(
        self,
        batch_id: str,
        prompt: dict[str, Any],
        requested_model_id: str,
        standalone: bool = False,
    ) -> str:
        request = self.storage.request_for(batch_id)
        model_id = str(
            requested_model_id
            or request.get("model")
            or self.config.get("prompt_model", "")
            or self.config.get("qa_model", "")
        ).strip()
        if not model_id:
            raise ValueError("Choose an prompt backend prompt model before regenerating this prompt.")
        original = str(prompt.get("prompt") or "").strip()
        if not original:
            raise ValueError("The current prompt is empty and cannot be regenerated.")
        context_length = max(4096, int(self.config.get("lm_context_length", 16384)))
        max_chars = max(6000, (context_length - 2048) * 3)
        if len(original) > max_chars:
            head = int(max_chars * 0.75)
            original_for_lm = original[:head] + "\n[repeated middle removed]\n" + original[-(max_chars - head):]
        else:
            original_for_lm = original
        self.storage.update_prompt(prompt["id"], status="regenerating", error="")
        phase = f"Prompt {prompt['position']}: unloading ComfyUI for prompt backend rewrite"
        message = f"Prompt {prompt['position']} is being regenerated. Freeing ComfyUI VRAM before prompt backend loads."
        if standalone:
            self.storage.update_batch(batch_id, status="regenerating_prompt", phase=phase, error="")
            self.storage.event(batch_id, "info", message)
        else:
            self._phase(batch_id, phase, message)
        try:
            self.services.free_comfy()
        except IntegrationError as error:
            exited = self.services.exit_comfy()
            offline_markers = ("actively refused", "connection refused", "winerror 10061")
            already_offline = any(marker in str(error).lower() for marker in offline_markers)
            if not exited and not already_offline:
                raise IntegrationError(
                    f"ComfyUI did not release VRAM and its verified process could not be exited: {error}"
                ) from error
            if exited:
                self.storage.event(
                    batch_id,
                    "warning",
                    "ComfyUI was unresponsive during the VRAM handoff, so WildCat safely exited its verified process.",
                )
            else:
                self.storage.event(batch_id, "info", "ComfyUI was already offline; its VRAM was already available.")
        if not standalone:
            self._check_cancelled(batch_id)
        self.services.ensure_lm()
        if standalone:
            self.storage.update_batch(batch_id, phase=f"Prompt {prompt['position']}: regenerating with prompt backend")
        else:
            self._phase(batch_id, f"Prompt {prompt['position']}: regenerating with prompt backend")
        try:
            runtime_model_id = self.services.load_lm(model_id)
            replacement = self.services.chat(
                runtime_model_id,
                (
                    "You repair image-generation prompts. Preserve the intended subject, scene, style, composition, "
                    "lighting, camera, and important constraints, but remove contradictions, broken syntax, excessive "
                    "repetition, and wording likely to make generation fail or stall. Return one complete replacement "
                    "positive prompt only. Do not number it, quote it, explain it, or use Markdown fences."
                ),
                "Rewrite this bad or unworkable image prompt into a reliable replacement:\n\n" + original_for_lm,
                temperature=0.45,
            ).strip()
        finally:
            self.services.unload_all_lm()
        replacement = re.sub(r"^```[^\n]*\n?|\n?```$", "", replacement).strip()
        replacement = re.sub(r"^\s*(?:replacement\s+prompt\s*:|prompt\s*:|\d+[.)]\s*)", "", replacement, flags=re.IGNORECASE).strip()
        if len(replacement) < 20:
            raise IntegrationError("prompt backend returned a replacement prompt that was too short to use.")
        self.storage.replace_prompt_text(prompt["id"], replacement)
        prompt["prompt"] = replacement
        prompt["status"] = "pending"
        self.storage.event(
            batch_id,
            "info",
            f"Prompt {prompt['position']} was regenerated and saved. The original remains in its prompt history.",
        )
        if standalone:
            self.storage.prepare_prompt_rerun(batch_id, prompt["id"])
            self.storage.update_batch(
                batch_id,
                status="interrupted",
                phase=f"Prompt {prompt['position']} regenerated — ready to resume",
                error="",
                paused=0,
                cancel_requested=0,
            )
            return replacement
        self._check_cancelled(batch_id)
        self._phase(
            batch_id,
            f"Prompt {prompt['position']}: prompt backend unloaded; restarting ComfyUI",
            "Prompt rewrite complete. prompt backend is unloaded and ComfyUI is resuming.",
        )
        self.services.ensure_comfy()
        return replacement

    def regenerate_saved_prompt(
        self,
        batch_id: str,
        prompt_id: str,
        model_id: str = "",
    ) -> dict[str, Any]:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("Wait for the active batch to finish or stop it before regenerating a library prompt.")
        batch = self.storage.batch(batch_id)
        if not batch:
            raise KeyError(batch_id)
        prompt = next((item for item in batch.get("prompts", []) if item.get("id") == prompt_id), None)
        if not prompt:
            raise KeyError(prompt_id)
        previous_status = str(batch.get("status") or "interrupted")
        previous_prompt_status = str(prompt.get("status") or "pending")
        with self._gpu_lock:
            try:
                replacement = self._regenerate_prompt(batch_id, prompt, model_id, standalone=True)
            except Exception as error:
                self.storage.update_prompt(
                    prompt_id,
                    status=previous_prompt_status if previous_prompt_status != "regenerating" else "pending",
                    error=f"Prompt regeneration failed: {error}",
                )
                self.storage.update_batch(
                    batch_id,
                    status=previous_status,
                    phase=f"Prompt {prompt['position']} regeneration failed",
                    error=str(error),
                )
                self.storage.event(batch_id, "error", f"Prompt {prompt['position']} regeneration failed: {error}")
                raise
        queue_state = self.resume(batch_id)
        return {"prompt_id": prompt_id, "position": prompt["position"], "replacement": replacement, **queue_state}

    def _run_comfy_queue(
        self,
        batch_id: str,
        base_workflow: dict[str, Any],
        positive_fields: list[str],
        negative_fields: list[str],
        workflow_entry: dict[str, Any],
    ) -> None:
        client_id = f"wildcat-harness-{uuid.uuid4().hex}"
        consecutive_errors = 0
        had_errors = False
        try:
            prompts = self.storage.pending_prompts(batch_id)
            for queue_index, prompt in enumerate(prompts, start=1):
                while int(prompt["completed_runs"]) < int(prompt["target_runs"]):
                    self._wait_if_paused(batch_id)
                    self._check_cancelled(batch_id)
                    run_number = int(prompt["completed_runs"]) + 1
                    self._phase(
                        batch_id,
                        f"ComfyUI queue {queue_index}/{len(prompts)} · prompt #{prompt['position']} · run {run_number}/{prompt['target_runs']}",
                    )
                    workflow, seed = prepare_workflow(
                        base_workflow,
                        prompt["prompt"],
                        prompt.get("negative_prompt", ""),
                        positive_fields,
                        negative_fields,
                    )
                    self.storage.update_prompt(prompt["id"], status="running", seed=seed, error="")
                    try:
                        self._set_current_prompt(batch_id, prompt, run_number)
                        try:
                            comfy_prompt_id = self.services.submit_comfy(workflow, client_id)
                            self._raise_prompt_action(batch_id, prompt["id"])
                            record = self._wait_for_comfy(batch_id, comfy_prompt_id, prompt["id"])
                            self._raise_prompt_action(batch_id, prompt["id"])
                        except Exception:
                            self._raise_prompt_action(batch_id, prompt["id"])
                            raise
                        self._clear_current_prompt(batch_id, prompt["id"])
                        outputs = self._outputs(record)
                        if not outputs:
                            raise IntegrationError("ComfyUI completed, but the workflow produced no downloadable outputs.")
                        self._save_outputs(batch_id, prompt, run_number, outputs, workflow_entry)
                        completed, _target = self.storage.increment_run(batch_id, prompt["id"], seed)
                        prompt["completed_runs"] = completed
                        consecutive_errors = 0
                    except Cancelled:
                        raise
                    except PausedRun:
                        self._clear_current_prompt(batch_id, prompt["id"])
                        self.storage.update_prompt(prompt["id"], status="pending", error="")
                        self.storage.event(
                            batch_id,
                            "info",
                            f"Prompt {prompt['position']} was interrupted for pause and will retry on resume.",
                        )
                        self._wait_if_paused(batch_id)
                        consecutive_errors = 0
                        continue
                    except RetryRun:
                        self._clear_current_prompt(batch_id, prompt["id"])
                        self.storage.update_prompt(prompt["id"], status="pending", error="")
                        self.storage.event(
                            batch_id,
                            "info",
                            f"Prompt {prompt['position']} was interrupted and will retry now with a new seed.",
                        )
                        consecutive_errors = 0
                        continue
                    except SkipPrompt:
                        self._clear_current_prompt(batch_id, prompt["id"])
                        self.storage.skip_prompt(batch_id, prompt["id"])
                        prompt["target_runs"] = prompt["completed_runs"]
                        self.storage.event(
                            batch_id,
                            "warning",
                            f"Prompt {prompt['position']} was skipped. WildCat is continuing with the next prompt.",
                        )
                        consecutive_errors = 0
                        break
                    except RegeneratePrompt as requested:
                        self._clear_current_prompt(batch_id, prompt["id"])
                        try:
                            self._regenerate_prompt(batch_id, prompt, requested.model_id)
                        except Cancelled:
                            raise
                        except Exception as error:  # Keep the rest of the batch usable if rewriting fails.
                            had_errors = True
                            consecutive_errors += 1
                            self.storage.update_prompt(prompt["id"], status="error", error=f"Prompt regeneration failed: {error}")
                            self.storage.event(batch_id, "error", f"Prompt {prompt['position']} regeneration failed: {error}")
                            try:
                                self.services.unload_all_lm()
                            except Exception:
                                pass
                            self.services.ensure_comfy()
                            break
                        consecutive_errors = 0
                        continue
                    except Exception as error:  # noqa: BLE001 - record prompt failure and continue safely
                        had_errors = True
                        consecutive_errors += 1
                        self.storage.update_prompt(prompt["id"], status="error", error=str(error))
                        self.storage.event(batch_id, "error", f"Prompt {prompt['position']} failed: {error}")
                        break
                    finally:
                        self._clear_current_prompt(batch_id, prompt["id"])
                if consecutive_errors >= 3:
                    raise IntegrationError("Three consecutive ComfyUI prompts failed; the queue stopped to avoid repeating the same error.")
        finally:
            self.storage.update_batch(batch_id, phase="Unloading ComfyUI models")
            self.storage.event(batch_id, "info", "Image phase ended. Freeing ComfyUI models and CUDA memory.")
            self.services.free_comfy()

        if had_errors:
            self.storage.update_batch(
                batch_id,
                status="complete_with_errors",
                phase="Finished with some prompt errors — ComfyUI unloaded",
            )
        else:
            self.storage.update_batch(
                batch_id,
                status="complete",
                phase="Finished — ComfyUI unloaded and VRAM released",
                error="",
            )
        self.storage.event(batch_id, "info", "Batch complete. GPU models are unloaded.")

    def pause(self, batch_id: str) -> None:
        if self.active().get("batch_id") != batch_id:
            raise ValueError("Only the currently running batch can be paused.")
        self.storage.update_batch(
            batch_id,
            paused=1,
            status="pausing",
            phase="Pausing now — interrupting the current ComfyUI run",
        )
        self.storage.event(
            batch_id,
            "warning",
            "Pause requested. The current ComfyUI run is being interrupted and will retry on resume.",
        )
        try:
            self.services.interrupt_comfy()
        except IntegrationError:
            pass

    def resume(self, batch_id: str) -> dict[str, Any]:
        active = self.active()
        if active["batch_id"] == batch_id:
            batch = self.storage.batch(batch_id, include_details=False)
            if batch and batch.get("status") == "pausing":
                raise ValueError("Pause is still being applied. Resume when the job shows paused.")
            self.storage.update_batch(batch_id, paused=0)
            return {"queued": False, "active_batch_id": batch_id}
        self.storage.update_batch(
            batch_id,
            status="queued",
            phase="Waiting in automatic queue",
            error="",
            paused=0,
            cancel_requested=0,
        )
        return self.start(batch_id)

    def cancel(self, batch_id: str) -> None:
        batch = self.storage.batch(batch_id, include_details=False)
        if not batch:
            raise KeyError(batch_id)
        if batch.get("status") in {"complete", "complete_with_errors", "cancelled"}:
            status = str(batch.get("status") or "finished").replace("_", " ")
            raise ValueError(f"This batch is already {status}; it cannot be cancelled.")
        active = self.active()
        if active["batch_id"] == batch_id:
            self.storage.update_batch(batch_id, cancel_requested=1, paused=0, phase="Cancelling")
            try:
                self.services.interrupt_comfy()
            except IntegrationError:
                pass
        elif batch.get("status") in {"queued", "interrupted", "failed", "paused"}:
            self.storage.update_batch(
                batch_id,
                status="cancelled",
                cancel_requested=1,
                paused=0,
                phase="Cancelled",
                error="Batch cancelled by user.",
            )
            self.storage.event(batch_id, "warning", "Batch cancelled by user.")
        else:
            status = str(batch.get("status") or "finished").replace("_", " ")
            raise ValueError(f"This batch is already {status}; it cannot be cancelled.")

    @staticmethod
    def _even_sample(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        if len(items) <= count:
            return items
        if count <= 1:
            return [items[len(items) // 2]]
        indexes = [round(index * (len(items) - 1) / (count - 1)) for index in range(count)]
        return [items[index] for index in indexes]

    def compare_guides(
        self,
        batch_id: str,
        model_id: str,
        focus: str,
        samples_per_guide: int,
    ) -> str:
        if self.active()["running"]:
            raise RuntimeError("Image comparison will be available when the active batch finishes.")
        batch = self.storage.batch(batch_id)
        if not batch:
            raise KeyError(batch_id)
        if batch.get("source") not in {"guides", "pbi"}:
            raise ValueError("Guide comparison is available only for batches generated from Markdown guides.")
        samples_per_guide = max(1, min(4, int(samples_per_guide)))

        grouped: dict[str, list[dict[str, Any]]] = {}
        for prompt in batch.get("prompts", []):
            guide = str(prompt.get("guide") or "").strip()
            if not guide:
                continue
            image_assets = [asset for asset in prompt.get("assets", []) if asset.get("kind") == "images"]
            grouped.setdefault(guide, []).extend(image_assets)
        grouped = {guide: assets for guide, assets in grouped.items() if assets}
        if len(grouped) < 2:
            raise ValueError("This batch needs saved image outputs from at least two markdown guides.")

        models = self.services.lm_models()
        selected_model = next((model for model in models if model.get("id") == model_id), None)
        if not selected_model:
            raise ValueError("Choose an installed prompt backend model for comparison.")
        if not selected_model.get("vision"):
            raise ValueError("The selected prompt backend model does not report vision support.")

        labeled_images: list[tuple[str, int, Path]] = []
        guide_samples: dict[str, list[str]] = {}
        for guide, assets in grouped.items():
            selected = self._even_sample(assets, samples_per_guide)
            guide_samples[guide] = []
            for index, asset in enumerate(selected, start=1):
                image_path = ROOT / asset["local_path"]
                if image_path.is_file():
                    guide_samples[guide].append(asset["filename"])
                    labeled_images.append((guide, index, image_path))
        guide_samples = {guide: files for guide, files in guide_samples.items() if files}
        if len(guide_samples) < 2:
            raise ValueError("Saved image files from at least two guides must still be available on disk.")

        with self._gpu_lock:
            if self.active()["running"]:
                raise RuntimeError("A queued image batch started first. Compare after it finishes.")
            try:
                self.services.free_comfy()
            except IntegrationError:
                pass
            self.services.ensure_lm()
            self.services.load_lm(model_id)
            try:
                result = self.services.compare_guide_images(
                    model_id,
                    batch.get("brief") or batch.get("title") or "Compare overall output quality.",
                    focus.strip(),
                    labeled_images,
                )
            finally:
                self.services.unload_all_lm()
        self.storage.add_comparison(
            batch_id,
            model_id,
            focus.strip(),
            samples_per_guide,
            guide_samples,
            result,
        )
        return result

    def ask(
        self,
        batch_id: str,
        prompt_id: str | None,
        question: str,
        model_id: str,
        include_image: bool,
    ) -> str:
        if self.active()["running"]:
            raise RuntimeError("Wait for the image batch to finish or cancel it before asking prompt backend a question.")
        batch = self.storage.batch(batch_id)
        if not batch:
            raise KeyError(batch_id)
        selected = None
        if prompt_id:
            selected = next((item for item in batch.get("prompts", []) if item["id"] == prompt_id), None)
        context = [f"Batch: {batch['title']}", f"Original brief: {batch['brief']}"]
        image_path: Path | None = None
        if selected:
            context.extend(
                [
                    f"Guide: {selected.get('guide') or 'Imported prompt'}",
                    f"Image-generation prompt: {selected['prompt']}",
                    f"Negative prompt: {selected.get('negative_prompt') or '(none)'}",
                    f"Last seed: {selected.get('seed') or '(not recorded)'}",
                ]
            )
            assets = selected.get("assets", [])
            if include_image and assets:
                image_path = ROOT / assets[0]["local_path"]
        elif batch.get("prompts"):
            context.append("Prompts in this batch:\n" + "\n".join(
                f"{item['position']}. [{item.get('guide') or 'imported'}] {item['prompt']}"
                for item in batch["prompts"][:100]
            ))

        with self._gpu_lock:
            try:
                self.services.free_comfy()
            except IntegrationError:
                # If ComfyUI is offline there is no Comfy model process to evict.
                pass
            self.services.ensure_lm()
            self.services.load_lm(model_id)
            try:
                answer = self.services.chat(
                    model_id,
                    "You help a creator remember, compare, and understand archived AI image batches. Use the supplied batch context. Be specific and concise. If an image is supplied, inspect it directly.",
                    "\n\n".join(context) + f"\n\nQuestion: {question}",
                    image_path=image_path,
                )
            finally:
                self.services.unload_all_lm()
        self.storage.add_question(batch_id, prompt_id, question, answer)
        return answer

    def workflow_analysis(self, workflow_id: str = "") -> dict[str, Any] | None:
        entry = self.workflow_entry(workflow_id)
        if not entry:
            return None
        return analyze_workflow(load_workflow_file(entry["file"]))
