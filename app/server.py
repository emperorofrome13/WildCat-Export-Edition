from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config, ROOT
from . import civitai as civitai_api
from .civitai import CivitaiError
from .core import analyze_image_inputs, analyze_workflow, read_comfy_prompt_graph, safe_filename, validate_workflow_json
from .guides import GuideLibrary
from .integrations import IntegrationError, Services
from .metadata import convert_png_to_civitai, inspect_civitai_metadata
from .orchestrator import DATA_DIR, IMAGE_DIR, JobManager
from .references import ReferenceLibrary
from .recycle import RecyclePixelBin
from .storage import Storage, utc_now


STATIC_DIR = Path(__file__).resolve().parent / "static"
WORKFLOW_DIR = DATA_DIR / "workflows"
IMAGE_TOOL_WORKFLOW_DIR = DATA_DIR / "image-tool-workflows"
DATABASE_PATH = DATA_DIR / "library.sqlite3"
MAX_BODY = 50 * 1024 * 1024
MAX_REFERENCE_UPLOAD = 2 * 1024 * 1024 * 1024
RESOURCE_HASH_CACHE = DATA_DIR / "resource-hashes.json"
RECYCLE_DIR = DATA_DIR / "recycle-bin"


config = Config()
storage = Storage(DATABASE_PATH)
storage.backfill_asset_dimensions(ROOT)
threading.Thread(target=storage.backfill_asset_hashes, args=(ROOT,), daemon=True).start()
threading.Thread(target=storage.backfill_asset_phashes, args=(ROOT,), daemon=True).start()
services = Services(config)
jobs = JobManager(storage, config, services)
references = ReferenceLibrary()
guides = GuideLibrary()
recycle_bin = RecyclePixelBin(RECYCLE_DIR)


def _parse_multipart_file(raw: bytes, content_type: str) -> tuple[str, bytes]:
    """Extract (filename, file_bytes) from a multipart/form-data body."""
    boundary_match = re.search(r"boundary=(.+?)(?:;|$)", content_type)
    if not boundary_match:
        return "", raw
    boundary = boundary_match.group(1).strip().strip('"').encode()
    for part in raw.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        header_block, body = part.split(b"\r\n\r\n", 1)
        header_text = header_block.decode("utf-8", errors="replace")
        if body.endswith(b"\r\n"):
            body = body[:-2]
        fn_match = re.search(r'filename="([^"]+)"', header_text)
        if fn_match:
            return fn_match.group(1), body
    return "", raw


def _asset_path(asset: dict[str, Any]) -> Path:
    target = (ROOT / str(asset.get("local_path") or "")).resolve()
    try:
        target.relative_to(IMAGE_DIR.resolve())
    except ValueError as error:
        raise ValueError("Image path is outside the WildCat Export Edition archive.") from error
    if not target.is_file():
        raise ValueError("Archived image file was not found.")
    return target


def _asset_source_filename(asset: dict[str, Any]) -> str:
    source = asset.get("source")
    if isinstance(source, dict) and source.get("filename"):
        return str(source["filename"])
    return str(asset.get("filename") or "")


def _postprocess_asset(asset: dict[str, Any], include_parameters: bool = True) -> dict[str, Any]:
    result = inspect_civitai_metadata(
        _asset_path(asset),
        _asset_source_filename(asset),
        config.get("comfy_path"),
        RESOURCE_HASH_CACHE,
    )
    extracted = dict(result.get("extracted") or {})
    if not include_parameters:
        extracted.pop("prompt", None)
        extracted.pop("negative_prompt", None)
        result.pop("parameters", None)
        result.pop("current_parameters", None)
    return {
        "id": asset["id"],
        "batch_id": asset["batch_id"],
        "prompt_id": asset["prompt_id"],
        "position": asset.get("position"),
        "guide": asset.get("guide") or "",
        "filename": asset["filename"],
        "local_path": asset["local_path"],
        **result,
        "extracted": extracted,
    }


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "WildCatHarness/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body must be valid JSON.") from error
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def _static(self, relative: str) -> None:
        relative = relative or "index.html"
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _media(self, relative: str) -> None:
        relative = urllib.parse.unquote(relative).replace("\\", "/").lstrip("/")
        if relative.startswith("data/images/"):
            relative = relative[len("data/images/") :]
        target = (IMAGE_DIR / relative).resolve()
        try:
            target.relative_to(IMAGE_DIR.resolve())
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Image not found")
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(content)

    def _reference_media(self, reference_id: str) -> None:
        try:
            target = references.media_path(reference_id)
        except KeyError:
            self._error(HTTPStatus.NOT_FOUND, "Reference not found")
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _recycle_collage(self, number: int) -> None:
        target = recycle_bin.collage_path(number).resolve()
        try:
            target.relative_to(RECYCLE_DIR.resolve())
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Recycle collage is empty")
            return
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _reference_source_upload(self, parsed: urllib.parse.ParseResult) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            raise ValueError("Choose a photo or video to upload.")
        if length > MAX_REFERENCE_UPLOAD:
            raise ValueError("A single reference file cannot exceed 2 GB.")
        query = urllib.parse.parse_qs(parsed.query)
        name = urllib.parse.unquote(query.get("name", ["reference"])[0])
        media_type = query.get("media_type", ["image"])[0]
        if media_type not in {"image", "video"}:
            raise ValueError("Reference media must be a photo or video.")
        entry = references.save_source(
            name,
            media_type,
            str(self.headers.get("Content-Type") or "application/octet-stream"),
            self.rfile,
            length,
        )
        self._json(201, {"reference": entry})

    def _recycle_find_and_delete(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            raise ValueError("No file was uploaded.")
        if length > MAX_BODY:
            raise ValueError("File is too large.")
        raw = self.rfile.read(length)
        if not raw:
            raise ValueError("Empty upload.")
        uploaded_name = ""
        file_data = raw
        content_type = str(self.headers.get("Content-Type") or "")
        if "form-data" in content_type:
            uploaded_name, file_data = _parse_multipart_file(raw, content_type)
        asset = storage.find_asset_by_filename(uploaded_name) if uploaded_name else None
        if not asset and file_data:
            file_hash = hashlib.sha256(file_data).hexdigest()
            asset = storage.find_asset_by_hash(file_hash)
        pixel_rgba = [128, 128, 128, 255]
        source_label = uploaded_name or "external image"
        deleted_from_disk = False
        if asset:
            if jobs.active().get("batch_id") == asset.get("batch_id"):
                self._error(409, "Wait for or cancel the active batch before recycling one of its images.")
                return
            target = _asset_path(asset)
            try:
                import importlib
                pil_image_mod = importlib.import_module("PIL.Image")
                img = pil_image_mod.open(str(target))
                pixel_rgba = list(img.resize((1, 1)).getpixel((0, 0)))
                img.close()
            except Exception:
                pass
            recycle = recycle_bin.add(asset, pixel_rgba)
            target.unlink(missing_ok=True)
            storage.delete_asset(asset["id"])
            deleted_from_disk = True
            self._json(200, {
                "found": True,
                "deleted_asset_id": asset["id"],
                "filename": asset["filename"],
                "batch_title": asset.get("batch_title") or "",
                "recycle_bin": recycle,
                "deleted_from_disk": deleted_from_disk,
            })
        else:
            try:
                import importlib
                io_mod = importlib.import_module("io")
                pil_image_mod = importlib.import_module("PIL.Image")
                img = pil_image_mod.open(io_mod.BytesIO(file_data))
                pixel_rgba = list(img.resize((1, 1)).getpixel((0, 0)))
                img.close()
            except Exception:
                pass
            recycle = recycle_bin.add_external(uploaded_name or source_label, source_label, pixel_rgba)
            self._json(200, {
                "found": True,
                "external": True,
                "filename": uploaded_name or source_label,
                "message": f"Recycled \"{uploaded_name or source_label}\".",
                "recycle_bin": recycle,
                "deleted_from_disk": False,
            })

    def _recycle_delete_file(self) -> None:
        """Delete a file from disk by filename in known directories."""
        body = self._body()
        filename = str(body.get("filename") or "").strip()
        if not filename:
            self._error(400, "Missing filename.")
            return
        search_dirs: list[Path] = []
        try:
            comfy_path = str(config.get("comfy_path", ""))
            if comfy_path:
                for sub in ("ComfyUI/output", "output"):
                    candidate = Path(comfy_path) / sub
                    if candidate.is_dir():
                        search_dirs.append(candidate)
        except Exception:
            pass
        if IMAGE_DIR.is_dir():
            search_dirs.append(IMAGE_DIR)
        deleted_paths: list[str] = []
        for search_dir in search_dirs:
            try:
                children = list(search_dir.iterdir())
            except OSError:
                continue
            for child in children:
                if child.name != filename or not child.is_file():
                    continue
                try:
                    actual = hashlib.sha256(child.read_bytes()).hexdigest()
                except OSError:
                    actual = None
                if actual:
                    # If file_hash was sent, verify it matches
                    file_hash = str(body.get("file_hash") or "").strip()
                    if file_hash and actual != file_hash:
                        continue
                try:
                    child.unlink()
                    deleted_paths.append(str(child))
                except OSError as e:
                    import traceback
                    print(f"delete error: {e} {child}", file=__import__('sys').stderr)
                    pass
        self._json(200, {"deleted": deleted_paths, "count": len(deleted_paths), "deleted_from_disk": len(deleted_paths) > 0})

    def _image_finder(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            raise ValueError("Drop or choose an image to search with.")
        if length > MAX_BODY:
            raise ValueError("File is too large.")
        raw = self.rfile.read(length)
        if not raw:
            raise ValueError("Empty upload.")
        uploaded_name = ""
        file_data = raw
        content_type = str(self.headers.get("Content-Type") or "")
        if "form-data" in content_type:
            uploaded_name, file_data = _parse_multipart_file(raw, content_type)
        from .core import dhash_bytes
        target_hash = dhash_bytes(file_data)
        if not target_hash:
            raise ValueError("Could not read that image. Try a PNG or JPEG.")
        matches = storage.find_similar_assets(target_hash)
        self._json(200, {
            "query_filename": uploaded_name,
            "match_count": len(matches),
            "matches": [
                {
                    "id": match["id"],
                    "filename": match["filename"],
                    "local_path": match["local_path"],
                    "batch_id": match["batch_id"],
                    "batch_title": match.get("batch_title") or "",
                    "width": match.get("width"),
                    "height": match.get("height"),
                    "hamming_distance": match.get("hamming_distance"),
                }
                for match in matches
            ],
        })

    @staticmethod
    def _remove_archived_files(paths: list[str], batch_id: str) -> None:
        for relative in paths:
            target = (ROOT / relative).resolve()
            try:
                target.relative_to(IMAGE_DIR.resolve())
            except ValueError:
                continue
            if target.is_file():
                target.unlink()
        folder = (IMAGE_DIR / batch_id).resolve()
        try:
            folder.relative_to(IMAGE_DIR.resolve())
        except ValueError:
            return
        if folder != IMAGE_DIR and folder.is_dir():
            shutil.rmtree(folder)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self._json(200, {"ok": True, "app": "WildCat Export Edition", "active": jobs.active()})
                return
            if path == "/api/bootstrap":
                jobs.backfill_workflow_provenance()
                self._json(
                    200,
                    {
                        "config": config.all(include_token=False),
                        "active": jobs.active(),
                        "workflow": jobs.workflow_analysis(),
                        "workflows": jobs.workflow_catalog(),
                        "image_tool_workflows": jobs.image_tool_catalog(),
                        "guides": guides.list(),
                        "references": references.list(),
                        "batches": storage.list_batches(),
                    },
                )
                return
            if path == "/api/status":
                service_status = services.status()
                service_status["guides"] = {"online": True, "count": len(guides.list(False))}
                self._json(200, {"services": service_status, "active": jobs.active(), "queued": storage.queued_count()})
                return
            if path == "/api/models":
                self._json(200, {"models": jobs.lm_models_for_ui()})
                return
            if path == "/api/guides":
                self._json(200, {"guides": guides.list()})
                return
            if path == "/api/batches":
                query = urllib.parse.parse_qs(parsed.query)
                self._json(200, {"batches": storage.list_batches(query.get("q", [""])[0])})
                return
            if path == "/api/leaderboard":
                self._json(200, storage.guide_leaderboard())
                return
            if path == "/api/image-tool-results":
                self._json(200, {"images": storage.image_tool_results()})
                return
            if path == "/api/recycle-bin":
                self._json(200, recycle_bin.summary())
                return
            recycle_collage_match = re.fullmatch(r"/recycle-bin/collage-(\d{4})\.png", path)
            if recycle_collage_match:
                self._recycle_collage(int(recycle_collage_match.group(1)))
                return
            if path == "/api/references":
                self._json(200, {"references": references.list()})
                return
            postprocess_batch_match = re.fullmatch(r"/api/postprocess/batches/([a-f0-9]+)", path)
            if postprocess_batch_match:
                batch_id = postprocess_batch_match.group(1)
                batch = storage.batch(batch_id, include_details=False)
                if not batch:
                    self._error(404, "Batch not found.")
                    return
                images = [
                    _postprocess_asset(asset, include_parameters=False)
                    for asset in storage.assets_for_batch(batch_id)
                ]
                counts = {
                    status: sum(item["status"] == status for item in images)
                    for status in ("compliant", "ready", "unavailable", "unsupported")
                }
                self._json(
                    200,
                    {
                        "batch": {"id": batch_id, "title": batch["title"]},
                        "images": images,
                        "counts": counts,
                    },
                )
                return
            postprocess_asset_match = re.fullmatch(r"/api/postprocess/assets/([a-f0-9]+)", path)
            if postprocess_asset_match:
                asset = storage.asset(postprocess_asset_match.group(1))
                if not asset:
                    self._error(404, "Image not found.")
                else:
                    self._json(200, {"image": _postprocess_asset(asset)})
                return
            batch_match = re.fullmatch(r"/api/batches/([a-f0-9]+)", path)
            if batch_match:
                batch = storage.batch(batch_match.group(1))
                if not batch:
                    self._error(404, "Batch not found.")
                else:
                    self._json(200, {"batch": batch, "active": jobs.active()})
                return
            if path.startswith("/media/"):
                self._media(path[len("/media/") :])
                return
            reference_media_match = re.fullmatch(r"/reference-media/([a-f0-9]+)", path)
            if reference_media_match:
                self._reference_media(reference_media_match.group(1))
                return
            if path == "/":
                self._static("index.html")
                return
            self._static(path.lstrip("/"))
        except (IntegrationError, ValueError, RuntimeError) as error:
            self._error(503, str(error))
        except Exception as error:  # noqa: BLE001 - return safe API failure
            self._error(500, str(error))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/references/source":
                self._reference_source_upload(parsed)
                return
            if path == "/api/recycle-bin/find-and-delete":
                self._recycle_find_and_delete()
                return
            if path == "/api/recycle-bin/delete-file":
                self._recycle_delete_file()
                return
            if path == "/api/image-finder":
                self._image_finder()
                return
            body = self._body()
            open_folder_match = re.fullmatch(r"/api/batches/([a-f0-9]+)/open-folder", path)
            if open_folder_match:
                batch_id = open_folder_match.group(1)
                batch = storage.batch(batch_id, include_details=False)
                if not batch:
                    raise ValueError("Batch not found.")
                folder = (IMAGE_DIR / batch_id).resolve()
                try:
                    folder.relative_to(IMAGE_DIR.resolve())
                except ValueError as error:
                    raise ValueError("Batch image folder is outside the WildCat Export Edition archive.") from error
                if not folder.is_dir():
                    raise ValueError("This job does not have an archived image folder yet.")
                if os.name != "nt":
                    raise ValueError("Open folder is available when WildCat Export Edition runs on Windows.")
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen(
                    ["explorer.exe", str(folder)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                self._json(200, {"opened": True, "folder": str(folder)})
                return
            if path == "/api/references/processed":
                reference_id = str(body.get("reference_id") or "")
                entry = references.save_processed(
                    reference_id,
                    str(body.get("data_url") or ""),
                    int(body.get("width") or 0),
                    int(body.get("height") or 0),
                    bool(body.get("remove_ui", True)),
                    body.get("crop") if isinstance(body.get("crop"), dict) else {},
                )
                self._json(200, {"reference": entry, "references": references.list()})
                return
            if path == "/api/settings":
                updates = dict(body)
                updates.pop("lm_has_api_token", None)
                updates.pop("civitai_api_token", None)
                current = config.update(updates)
                public = dict(current)
                public["lm_has_api_token"] = bool(public.get("lm_api_token"))
                public["lm_api_token"] = ""
                self._json(200, {"config": public})
                return
            if path == "/api/civitai/settings":
                token = str(body.get("api_token") or "").strip()
                username = str(body.get("username") or "").strip()
                if token and token != "__KEEP__":
                    me = civitai_api.get_me(token)
                    verified_name = str(me.get("username") or "").strip()
                    if not verified_name:
                        raise ValueError("Civitai accepted the key but did not return a username.")
                    config.update({"civitai_api_token": token, "civitai_username": verified_name})
                    self._json(200, {"ok": True, "username": verified_name, "civitai_has_api_token": True})
                    return
                if username:
                    config.update({"civitai_username": username})
                self._json(200, {
                    "ok": True,
                    "username": username or str(config.get("civitai_username") or ""),
                    "civitai_has_api_token": bool(config.get("civitai_api_token")),
                })
                return
            if path == "/api/civitai/sync":
                token = str(config.get("civitai_api_token") or "").strip()
                if not token:
                    raise ValueError("Add your Civitai API key in Settings first.")
                username = str(config.get("civitai_username") or "").strip()
                if not username:
                    me = civitai_api.get_me(token)
                    username = str(me.get("username") or "").strip()
                    if not username:
                        raise ValueError("Civitai did not return a username for this API key.")
                    config.update({"civitai_username": username})
                assets = storage.civitai_match_candidates()
                items: list[dict[str, Any]] = []
                cursor: str | None = None
                pages = 0
                while pages < 60:
                    payload = civitai_api.fetch_user_images(token, username, cursor)
                    items.extend(payload.get("items") or [])
                    cursor = (payload.get("metadata") or {}).get("nextCursor")
                    pages += 1
                    if not cursor:
                        break
                matches = civitai_api.match_civitai_items(assets, items)
                for asset_id, info in matches.items():
                    storage.mark_civitai(asset_id, True, info["post_id"], info["url"])
                config.update({"civitai_last_sync": utc_now()})
                counts = storage.civitai_counts()
                self._json(200, {
                    "ok": True,
                    "username": username,
                    "pages": pages,
                    "civitai_images_seen": len(items),
                    "newly_matched": len(matches),
                    **counts,
                })
                return
            if path == "/api/civitai/post":
                token = str(config.get("civitai_api_token") or "").strip()
                if not token:
                    raise ValueError("Add your Civitai API key in Settings first.")
                raw_ids = body.get("asset_ids") if isinstance(body.get("asset_ids"), list) else [body.get("asset_id")]
                asset_ids: list[str] = []
                for item in raw_ids:
                    value = str(item or "").strip()
                    if value and value not in asset_ids:
                        asset_ids.append(value)
                if not asset_ids:
                    raise ValueError("Choose at least one image to post.")
                publish = bool(body.get("publish", False))
                mode = str(body.get("mode") or "per_image").strip().lower()
                if mode not in {"per_image", "single_post"}:
                    raise ValueError("Civitai posting mode must be per_image or single_post.")
                title_override = str(body.get("title") or "").strip()
                detail = str(body.get("detail") or "").strip()
                nsfw_level = str(body.get("nsfw_level") or "").strip()
                if nsfw_level and nsfw_level not in {"None", "Soft", "Mature", "X"}:
                    raise ValueError("NSFW level must be None, Soft, Mature, or X.")
                model_version_id: int | None = None
                raw_mvid = body.get("model_version_id")
                if raw_mvid not in (None, "", 0, "0"):
                    try:
                        model_version_id = int(raw_mvid)
                    except (TypeError, ValueError) as error:
                        raise ValueError("Model version ID must be a number.") from error
                loaded: list[tuple[str, dict[str, Any], Path, bytes]] = []
                for asset_id in asset_ids:
                    asset = storage.civitai_post_context(asset_id)
                    if not asset:
                        raise ValueError("Image not found.")
                    if asset.get("civitai_posted"):
                        continue
                    target = _asset_path(asset)
                    data = target.read_bytes()
                    loaded.append((asset_id, asset, target, data))
                if not loaded:
                    self._json(200, {"ok": True, "posts": [], "skipped_all": True, "counts": storage.civitai_counts()})
                    return
                results: list[dict[str, Any]] = []

                def upload_one(asset: dict[str, Any], target: Path, data: bytes, index: int) -> dict[str, Any]:
                    upload = civitai_api.request_upload_url(token, target.name, len(data), "image/png")
                    key = str(upload.get("id") or "").strip()
                    upload_url = str(upload.get("uploadURL") or "").strip()
                    if not key or not upload_url:
                        raise CivitaiError("Civitai did not return an upload URL.")
                    civitai_api.upload_image_bytes(upload_url, data, "image/png")
                    meta = civitai_api.build_generation_meta(
                        str(asset.get("prompt") or ""),
                        str(asset.get("negative_prompt") or ""),
                        asset.get("seed"),
                    )
                    graph = read_comfy_prompt_graph(target)
                    resources = civitai_api.build_civitai_resources(
                        token,
                        graph,
                        str(config.get("comfy_path") or ""),
                        RESOURCE_HASH_CACHE,
                    )
                    if resources:
                        meta["civitaiResources"] = resources
                    width = int(asset.get("width") or 0)
                    height = int(asset.get("height") or 0)
                    entry: dict[str, Any] = {
                        "url": key,
                        "hash": civitai_api.file_sha256_hex(data),
                        "index": index,
                        "name": target.name,
                        "type": "image",
                        "meta": meta,
                    }
                    if width:
                        entry["width"] = width
                    if height:
                        entry["height"] = height
                    if model_version_id:
                        entry["modelVersionId"] = model_version_id
                    return entry

                if mode == "single_post":
                    entries = [upload_one(asset, target, data, i) for i, (asset_id, asset, target, data) in enumerate(loaded)]
                    title = title_override
                    result = civitai_api.create_post_with_images(token, title, detail, entries, publish=publish, model_version_id=model_version_id)
                    post_id = result.get("id")
                    url = civitai_api.POST_URL_TEMPLATE.format(post_id=post_id)
                    for asset_id, _asset, _target, _data in loaded:
                        storage.mark_civitai(asset_id, True, post_id, url)
                    warnings: list[str] = []
                    image_ids = result.get("imageIds") or []
                    if nsfw_level:
                        for image_id in image_ids:
                            try:
                                civitai_api.update_image_nsfw_level(token, int(image_id), nsfw_level)
                            except CivitaiError as error:
                                warnings.append(str(error))
                    results.append({"post_id": post_id, "url": url, "images": len(entries), "published": bool(result.get("publishedAt")), "asset_ids": [entry[0] for entry in loaded]})
                else:
                    warnings = []
                    for asset_id, asset, target, data in loaded:
                        entry = upload_one(asset, target, data, 0)
                        result = civitai_api.create_post_with_images(token, title_override, detail, [entry], publish=publish, model_version_id=model_version_id)
                        post_id = result.get("id")
                        url = civitai_api.POST_URL_TEMPLATE.format(post_id=post_id)
                        storage.mark_civitai(asset_id, True, post_id, url)
                        image_ids = result.get("imageIds") or []
                        if nsfw_level and image_ids:
                            try:
                                civitai_api.update_image_nsfw_level(token, int(image_ids[0]), nsfw_level)
                            except CivitaiError as error:
                                warnings.append(str(error))
                        results.append({"asset_id": asset_id, "post_id": post_id, "url": url, "published": bool(result.get("publishedAt"))})
                if model_version_id:
                    config.update({"civitai_last_model_version": str(model_version_id)})
                self._json(200, {"ok": True, "mode": mode, "posts": results, "warnings": warnings, "counts": storage.civitai_counts()})
                return
            civitai_mark_match = re.fullmatch(r"/api/assets/([a-f0-9]+)/civitai-mark", path)
            if civitai_mark_match:
                posted = bool(body.get("posted"))
                raw_post_id = body.get("post_id")
                post_id = int(raw_post_id) if raw_post_id else None
                asset = storage.mark_civitai(civitai_mark_match.group(1), posted, post_id, str(body.get("url") or ""))
                self._json(200, {"ok": True, "asset": asset, "counts": storage.civitai_counts()})
                return
            if path == "/api/guides":
                entry = guides.save(str(body.get("name") or "guide.md"), str(body.get("content") or ""))
                self._json(201, {"guide": entry, "guides": guides.list()})
                return
            if path == "/api/workflow":
                content = str(body.get("content") or "")
                filename = safe_filename(str(body.get("filename") or "workflow-api.json"))
                workflow = validate_workflow_json(content)
                analysis = analyze_workflow(workflow)
                WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
                workflow_id = uuid.uuid4().hex
                target = WORKFLOW_DIR / f"{safe_filename(Path(filename).stem)}-{workflow_id[:8]}-api.json"
                target.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")
                entry = {
                    "id": workflow_id,
                    "name": filename,
                    "file": str(target.resolve()),
                    "positive_fields": analysis["recommended_positive"],
                    "negative_fields": analysis["recommended_negative"],
                }
                workflows = jobs.workflow_entries()
                workflows.append(entry)
                config.update(
                    {
                        "workflows": workflows,
                        "active_workflow_id": workflow_id,
                        "workflow_file": str(target.resolve()),
                        "workflow_name": filename,
                        "workflow_positive_fields": analysis["recommended_positive"],
                        "workflow_negative_fields": analysis["recommended_negative"],
                    }
                )
                self._json(
                    200,
                    {
                        "workflow": analysis,
                        "workflow_entry": entry,
                        "workflows": jobs.workflow_catalog(),
                        "active_workflow_id": workflow_id,
                    },
                )
                return
            if path == "/api/image-tool-workflows":
                content = str(body.get("content") or "")
                filename = safe_filename(str(body.get("filename") or "image-tool-api.json"))
                kind = str(body.get("kind") or "").strip().lower()
                if kind not in {"edit", "upscale"}:
                    raise ValueError("Choose Editing or Upscaling for this API workflow.")
                workflow = validate_workflow_json(content)
                image_inputs = analyze_image_inputs(workflow)
                analysis = analyze_workflow(workflow)
                if not image_inputs:
                    raise ValueError(
                        "This API workflow has no LoadImage node. Add a Load Image node in ComfyUI, "
                        "connect it to the edit or upscale workflow, then export Workflow (API) again."
                    )
                if kind == "edit" and not analysis["recommended_positive"]:
                    raise ValueError(
                        "This editing API workflow has no text prompt input. Add a positive text node, "
                        "connect it to the workflow, then export Workflow (API) again."
                    )
                IMAGE_TOOL_WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
                workflow_id = uuid.uuid4().hex
                target = IMAGE_TOOL_WORKFLOW_DIR / (
                    f"{safe_filename(Path(filename).stem)}-{workflow_id[:8]}-{kind}-api.json"
                )
                target.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")
                entry = {
                    "id": workflow_id,
                    "name": filename,
                    "file": str(target.resolve()),
                    "kind": kind,
                    "image_fields": [field["field_id"] for field in image_inputs],
                    "positive_fields": analysis["recommended_positive"] if kind == "edit" else [],
                }
                entries = jobs.image_tool_entries()
                entries.append(entry)
                config.update({"image_tool_workflows": entries})
                self._json(
                    200,
                    {
                        "workflow_entry": entry,
                        "image_tool_workflows": jobs.image_tool_catalog(),
                    },
                )
                return
            if path == "/api/workflow/select":
                workflow_id = str(body.get("workflow_id") or "").strip()
                entry = jobs.workflow_entry(workflow_id)
                if not entry or entry["id"] != workflow_id:
                    raise ValueError("Choose a saved ComfyUI API workflow.")
                config.update(
                    {
                        "active_workflow_id": entry["id"],
                        "workflow_file": entry["file"],
                        "workflow_name": entry["name"],
                        "workflow_positive_fields": entry["positive_fields"],
                        "workflow_negative_fields": entry["negative_fields"],
                    }
                )
                self._json(
                    200,
                    {
                        "workflow": jobs.workflow_analysis(entry["id"]),
                        "workflow_entry": entry,
                        "workflows": jobs.workflow_catalog(),
                        "active_workflow_id": entry["id"],
                    },
                )
                return
            if path == "/api/workflow/mapping":
                positive = body.get("positive_fields") or []
                negative = body.get("negative_fields") or []
                if not isinstance(positive, list) or not isinstance(negative, list):
                    raise ValueError("Workflow mappings must be lists.")
                if not positive:
                    raise ValueError("Select at least one positive prompt field.")
                workflow_id = str(body.get("workflow_id") or config.get("active_workflow_id") or "").strip()
                entry = jobs.workflow_entry(workflow_id)
                if not entry or entry["id"] != workflow_id:
                    raise ValueError("Choose a saved ComfyUI API workflow.")
                workflows = jobs.workflow_entries()
                for item in workflows:
                    if item["id"] == workflow_id:
                        item["positive_fields"] = list(positive)
                        item["negative_fields"] = list(negative)
                config.update(
                    {
                        "workflows": workflows,
                        "active_workflow_id": workflow_id,
                        "workflow_file": entry["file"],
                        "workflow_name": entry["name"],
                        "workflow_positive_fields": positive,
                        "workflow_negative_fields": negative,
                    }
                )
                self._json(200, {"ok": True})
                return
            if path == "/api/batches":
                requested_workflow_id = str(body.get("workflow_id") or "").strip()
                workflow_entry = jobs.workflow_entry(requested_workflow_id)
                if not workflow_entry:
                    raise ValueError("Upload and map a ComfyUI API workflow before starting a batch.")
                if requested_workflow_id and workflow_entry["id"] != requested_workflow_id:
                    raise ValueError("The selected ComfyUI API workflow no longer exists.")
                body["workflow_id"] = workflow_entry["id"]
                body["workflow_name"] = workflow_entry["name"]
                source = body.get("source")
                selected_references: list[dict[str, Any]] = []
                if source not in {"guides", "pbi", "paste"}:
                    raise ValueError("Choose WildCat guides or pasted prompts as the source.")
                if source in {"guides", "pbi"}:
                    body["source"] = "guides"
                    if not str(body.get("model") or "").strip():
                        raise ValueError("Choose an prompt backend prompt model.")
                    reference_ids = body.get("reference_ids") or []
                    if not isinstance(reference_ids, list):
                        raise ValueError("Reference IDs must be a list.")
                    selected_references = references.selected([str(item) for item in reference_ids])
                    if not str(body.get("brief") or "").strip() and not selected_references:
                        raise ValueError("Describe the image batch or add reference media.")
                    reference_mode = str(body.get("reference_mode") or "inspiration")
                    if reference_mode not in {"match", "inspiration"}:
                        raise ValueError("Choose Close match or Inspiration for reference media.")
                    body["reference_mode"] = reference_mode
                    selected_guides = body.get("guides") or []
                    if not selected_guides:
                        raise ValueError("Choose at least one WildCat Markdown guide.")
                else:
                    if not str(body.get("pasted_prompts") or "").strip():
                        raise ValueError("Paste at least one prompt.")
                staged_references: list[dict[str, Any]] = []
                try:
                    if selected_references:
                        staged_references = references.stage([str(item["id"]) for item in selected_references])
                    body["references"] = staged_references
                    body["reference_ids"] = []
                    body["reference_count"] = len(staged_references)
                    base_prompt_count = max(1, int(body.get("prompts_per_guide") or 1)) if source in {"guides", "pbi"} else 0
                    body["effective_prompts_per_guide"] = base_prompt_count + len(staged_references)
                    batch_id = storage.create_batch(body)
                except Exception:
                    references.cleanup_staged(staged_references)
                    raise
                queue_only = bool(body.get("queue_only"))
                if queue_only:
                    queue_state = {
                        "queued": True,
                        "active_batch_id": jobs.active().get("batch_id"),
                        "queue_position": storage.batch(batch_id, include_details=False).get("queue_position"),
                    }
                else:
                    jobs.unpause_queue()
                    queue_state = jobs.start(batch_id)
                self._json(
                    201,
                    {
                        "batch_id": batch_id,
                        "reference_count": len(staged_references),
                        "effective_prompts_per_guide": body["effective_prompts_per_guide"],
                        "references": references.list(),
                        "queued": queue_only,
                        **queue_state,
                    },
                )
                return
            rerun_match = re.fullmatch(r"/api/batches/([a-f0-9]+)/rerun", path)
            if rerun_match:
                workflow_id = str(body.get("workflow_id") or "").strip()
                workflow_name = ""
                if workflow_id:
                    entry = jobs.workflow_entry(workflow_id)
                    if not entry or entry["id"] != workflow_id:
                        raise ValueError("The selected ComfyUI API workflow no longer exists.")
                    workflow_id = str(entry["id"])
                    workflow_name = str(entry["name"])
                selection = str(body.get("selection") or "all").strip().lower()
                if selection not in {"all", "errors", "unrated"}:
                    raise ValueError("Prompt selection must be all, errors, or unrated.")
                batch_id = storage.clone_batch_for_rerun(
                    rerun_match.group(1),
                    workflow_id=workflow_id,
                    workflow_name=workflow_name,
                    selection=selection,
                )
                jobs.unpause_queue()
                queue_state = jobs.start(batch_id)
                self._json(201, {"batch_id": batch_id, **queue_state, "active": jobs.active()})
                return
            prompt_action_match = re.fullmatch(
                r"/api/batches/([a-f0-9]+)/current-prompt/(retry|skip|regenerate)", path
            )
            if prompt_action_match:
                batch_id, action = prompt_action_match.groups()
                current = jobs.prompt_action(batch_id, action, str(body.get("model") or ""))
                self._json(200, {"ok": True, "current_prompt": current, "active": jobs.active()})
                return
            saved_prompt_regenerate_match = re.fullmatch(
                r"/api/batches/([a-f0-9]+)/prompts/([a-f0-9]+)/regenerate", path
            )
            if saved_prompt_regenerate_match:
                batch_id, prompt_id = saved_prompt_regenerate_match.groups()
                result = jobs.regenerate_saved_prompt(batch_id, prompt_id, str(body.get("model") or ""))
                self._json(200, {"ok": True, **result, "active": jobs.active()})
                return
            action_match = re.fullmatch(r"/api/batches/([a-f0-9]+)/(pause|resume|cancel|run|queue)", path)
            if action_match:
                batch_id, action = action_match.groups()
                queue_state: dict[str, Any] = {}
                if action == "pause":
                    jobs.pause(batch_id)
                elif action == "resume":
                    queue_state = jobs.resume(batch_id)
                elif action == "cancel":
                    jobs.cancel(batch_id)
                elif action == "queue":
                    queue_state = jobs.queue_batch(batch_id)
                else:
                    jobs.unpause_queue()
                    queue_state = jobs.start(batch_id)
                self._json(200, {"ok": True, **queue_state, "active": jobs.active()})
                return
            if path == "/api/ask":
                question = str(body.get("question") or "").strip()
                if not question:
                    raise ValueError("Enter a question.")
                model = str(body.get("model") or config.get("qa_model") or "").strip()
                if not model:
                    raise ValueError("Choose an prompt backend Q&A model.")
                answer = jobs.ask(
                    str(body.get("batch_id") or ""),
                    str(body.get("prompt_id") or "") or None,
                    question,
                    model,
                    bool(body.get("include_image", False)),
                )
                self._json(200, {"answer": answer})
                return
            if path == "/api/compare":
                model = str(body.get("model") or config.get("comparison_model") or "").strip()
                if not model:
                    raise ValueError("Choose a vision-capable prompt backend model.")
                result = jobs.compare_guides(
                    str(body.get("batch_id") or ""),
                    model,
                    str(body.get("focus") or ""),
                    int(body.get("samples_per_guide") or 2),
                )
                config.update({"comparison_model": model})
                self._json(200, {"result": result})
                return
            rating_match = re.fullmatch(r"/api/assets/([a-f0-9]+)/rating", path)
            if rating_match:
                asset = storage.rate_asset(rating_match.group(1), int(body.get("rating", 0)))
                self._json(200, {"asset": asset})
                return
            postprocess_asset_match = re.fullmatch(r"/api/postprocess/assets/([a-f0-9]+)", path)
            if postprocess_asset_match:
                asset = storage.asset(postprocess_asset_match.group(1))
                if not asset:
                    self._error(404, "Image not found.")
                    return
                result = convert_png_to_civitai(
                    _asset_path(asset),
                    _asset_source_filename(asset),
                    config.get("comfy_path"),
                    RESOURCE_HASH_CACHE,
                )
                self._json(200, {"image": _postprocess_asset(asset), "changed": bool(result.get("changed"))})
                return
            postprocess_batch_match = re.fullmatch(r"/api/postprocess/batches/([a-f0-9]+)", path)
            if postprocess_batch_match:
                batch_id = postprocess_batch_match.group(1)
                batch = storage.batch(batch_id, include_details=False)
                if not batch:
                    self._error(404, "Batch not found.")
                    return
                counts = {"converted": 0, "already_compliant": 0, "unavailable": 0, "unsupported": 0, "errors": 0}
                failures: list[dict[str, str]] = []
                for asset in storage.assets_for_batch(batch_id):
                    try:
                        result = convert_png_to_civitai(
                            _asset_path(asset),
                            _asset_source_filename(asset),
                            config.get("comfy_path"),
                            RESOURCE_HASH_CACHE,
                        )
                        if result["status"] == "compliant":
                            key = "converted" if result.get("changed") else "already_compliant"
                        else:
                            key = str(result["status"])
                        counts[key] = counts.get(key, 0) + 1
                    except Exception as error:  # noqa: BLE001 - continue the remaining images
                        counts["errors"] += 1
                        if len(failures) < 20:
                            failures.append({"filename": str(asset["filename"]), "error": str(error)})
                storage.event(
                    batch_id,
                    "info" if not counts["errors"] else "warning",
                    f"Civitai metadata post-processing: {counts['converted']} converted, "
                    f"{counts['already_compliant']} already compliant, {counts['errors']} errors.",
                )
                self._json(200, {"counts": counts, "failures": failures})
                return
            if path == "/api/actions/unload-lm":
                try:
                    unloaded = services.unload_all_lm()
                except IntegrationError:
                    unloaded = []
                self._json(200, {"ok": True, "unloaded": unloaded})
                return
            if path == "/api/actions/free-comfy":
                try:
                    services.free_comfy()
                except IntegrationError:
                    pass
                self._json(200, {"ok": True})
                return
            if path == "/api/actions/stop-comfy":
                active = jobs.active()
                batch_id = active.get("batch_id")
                if batch_id:
                    jobs.cancel(str(batch_id))
                errors = []
                try:
                    services.interrupt_comfy()
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
                try:
                    services.free_comfy()
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
                self._json(
                    200,
                    {
                        "ok": not errors,
                        "errors": errors,
                        "cancelled_batch_id": batch_id,
                        "active": jobs.active(),
                    },
                )
                return
            if path == "/api/actions/exit-comfy":
                active = jobs.active()
                batch_id = active.get("batch_id")
                if batch_id:
                    jobs.cancel(str(batch_id))
                    deadline = time.time() + 15
                    while jobs.active().get("batch_id") == batch_id and time.time() < deadline:
                        time.sleep(0.2)
                try:
                    services.interrupt_comfy()
                except IntegrationError:
                    pass
                try:
                    services.free_comfy()
                except IntegrationError:
                    pass
                exited = services.exit_comfy()
                self._json(
                    200,
                    {
                        "ok": True,
                        "exited_processes": exited,
                        "already_stopped": not exited,
                        "cancelled_batch_id": batch_id,
                        "active": jobs.active(),
                    },
                )
                return
            if path == "/api/actions/emergency-stop":
                active = jobs.active()
                if active.get("batch_id"):
                    jobs.cancel(str(active["batch_id"]))
                cancelled_queued = storage.cancel_all_queued()
                jobs.pause_queue()
                errors = []
                try:
                    services.interrupt_comfy()
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
                try:
                    services.unload_all_lm()
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
                try:
                    services.free_comfy()
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
                self._json(200, {"ok": not errors, "errors": errors, "cancelled_queued": cancelled_queued})
                return
            image_tool_match = re.fullmatch(r"/api/assets/([a-f0-9]+)/image-tool", path)
            if image_tool_match:
                asset = storage.asset(image_tool_match.group(1))
                if not asset:
                    self._error(404, "Image not found.")
                    return
                workflow_id = str(body.get("workflow_id") or "").strip()
                instruction = str(body.get("instruction") or "").strip()
                saved = jobs.run_image_tool(asset, workflow_id, _asset_path(asset), instruction)
                self._json(
                    200,
                    {
                        "ok": True,
                        "source_asset_id": asset["id"],
                        "images": saved,
                        "image_tool_workflows": jobs.image_tool_catalog(),
                    },
                )
                return
            rename_match = re.fullmatch(r"/api/batches/([a-f0-9]+)/rename", path)
            if rename_match:
                batch_id = rename_match.group(1)
                new_title = str(body.get("title") or "").strip()
                if not new_title:
                    raise ValueError("Enter a new batch name.")
                batch = storage.batch(batch_id, include_details=False)
                if not batch:
                    raise ValueError("Batch not found.")
                storage.update_batch(batch_id, title=new_title[:160])
                self._json(200, {"ok": True, "batch_id": batch_id, "title": new_title[:160]})
                return
            inpaint_match = re.fullmatch(r"/api/assets/([a-f0-9]+)/inpaint", path)
            if inpaint_match:
                asset = storage.asset(inpaint_match.group(1))
                if not asset:
                    self._error(404, "Image not found.")
                    return
                workflow_id = str(body.get("workflow_id") or "").strip()
                instruction = str(body.get("instruction") or "").strip()
                mask_data_url = str(body.get("mask_data_url") or "").strip()
                denoise = float(body.get("denoise") or 0.75)
                if not instruction:
                    raise ValueError("Enter an edit instruction for the masked area.")
                if not mask_data_url:
                    raise ValueError("Paint a mask area on the image first.")
                saved = jobs.run_inpaint(asset, workflow_id, _asset_path(asset), instruction, mask_data_url, denoise)
                self._json(
                    200,
                    {
                        "ok": True,
                        "source_asset_id": asset["id"],
                        "images": saved,
                        "image_tool_workflows": jobs.image_tool_catalog(),
                    },
                )
                return
            if path == "/api/actions/queue-stop":
                active = jobs.active()
                batch_id = active.get("batch_id")
                if batch_id:
                    jobs.cancel(str(batch_id))
                cancelled_queued = storage.cancel_all_queued()
                jobs.pause_queue()
                errors = []
                try:
                    services.interrupt_comfy()
                except Exception as error:
                    errors.append(str(error))
                try:
                    services.unload_all_lm()
                except Exception as error:
                    errors.append(str(error))
                try:
                    services.free_comfy()
                except Exception as error:
                    errors.append(str(error))
                self._json(200, {"ok": not errors, "errors": errors, "cancelled_queued": cancelled_queued, "active": jobs.active()})
                return
            self._error(404, "Not found")
        except KeyError:
            self._error(404, "Batch not found.")
        except (IntegrationError, ValueError, RuntimeError) as error:
            self._error(409, str(error))
        except Exception as error:  # noqa: BLE001 - return safe API failure
            self._error(500, str(error))

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.urlparse(self.path).path
        asset_match = re.fullmatch(r"/api/assets/([a-f0-9]+)", path)
        if asset_match:
            asset = storage.asset(asset_match.group(1))
            if not asset:
                self._error(404, "Image not found.")
                return
            if jobs.active().get("batch_id") == asset.get("batch_id"):
                self._error(409, "Wait for or cancel the active batch before deleting one of its images.")
                return
            try:
                body = self._body()
                target = _asset_path(asset)
                recycle = recycle_bin.add(asset, body.get("pixel"))
                target.unlink()
                storage.delete_asset(asset["id"])
            except (OSError, ValueError) as error:
                self._error(409, str(error))
                return
            self._json(200, {"deleted_asset_id": asset["id"], "recycle_bin": recycle})
            return
        reference_match = re.fullmatch(r"/api/references/([a-f0-9]+)", path)
        if reference_match:
            try:
                entry = references.delete(reference_match.group(1))
            except KeyError:
                self._error(404, "Reference not found.")
                return
            self._json(200, {"deleted_reference_id": entry["id"], "references": references.list()})
            return
        guide_match = re.fullmatch(r"/api/guides/([a-f0-9]+)", path)
        if guide_match:
            try:
                entry = guides.delete(guide_match.group(1))
            except KeyError:
                self._error(404, "Markdown guide not found.")
                return
            self._json(200, {"deleted_guide_id": entry["id"], "guides": guides.list()})
            return
        image_tool_workflow_match = re.fullmatch(r"/api/image-tool-workflows/([a-f0-9]+)", path)
        if image_tool_workflow_match:
            workflow_id = image_tool_workflow_match.group(1)
            entry = jobs.image_tool_entry(workflow_id)
            if not entry:
                self._error(404, "Image editing/upscaling API workflow not found.")
                return
            entries = [item for item in jobs.image_tool_entries() if item["id"] != workflow_id]
            config.update({"image_tool_workflows": entries})
            workflow_path = Path(entry["file"]).resolve()
            try:
                workflow_path.relative_to(IMAGE_TOOL_WORKFLOW_DIR.resolve())
            except ValueError:
                pass
            else:
                if workflow_path.is_file():
                    workflow_path.unlink()
            self._json(
                200,
                {
                    "deleted_workflow_id": workflow_id,
                    "image_tool_workflows": jobs.image_tool_catalog(),
                },
            )
            return

        workflow_match = re.fullmatch(r"/api/workflows/([a-f0-9]+)", path)
        if workflow_match:
            workflow_id = workflow_match.group(1)
            entry = jobs.workflow_entry(workflow_id)
            if not entry or entry["id"] != workflow_id:
                self._error(404, "ComfyUI API workflow not found.")
                return
            dependent = storage.unfinished_batches_for_workflow(workflow_id)
            if dependent:
                names = ", ".join(f"{item['title']} ({item['status']})" for item in dependent[:3])
                self._error(409, f"Cannot delete this workflow while unfinished jobs depend on it: {names}")
                return
            workflows = [item for item in jobs.workflow_entries() if item["id"] != workflow_id]
            next_entry = workflows[0] if workflows else None
            config.update(
                {
                    "workflows": workflows,
                    "active_workflow_id": next_entry["id"] if next_entry else "",
                    "workflow_file": next_entry["file"] if next_entry else "",
                    "workflow_name": next_entry["name"] if next_entry else "",
                    "workflow_positive_fields": next_entry["positive_fields"] if next_entry else [],
                    "workflow_negative_fields": next_entry["negative_fields"] if next_entry else [],
                }
            )
            workflow_path = Path(entry["file"]).resolve()
            try:
                workflow_path.relative_to(WORKFLOW_DIR.resolve())
            except ValueError:
                pass
            else:
                if workflow_path.is_file():
                    workflow_path.unlink()
            self._json(
                200,
                {
                    "deleted_workflow_id": workflow_id,
                    "active_workflow_id": next_entry["id"] if next_entry else "",
                    "workflow_entry": next_entry,
                    "workflow": jobs.workflow_analysis(next_entry["id"]) if next_entry else None,
                    "workflows": jobs.workflow_catalog(),
                },
            )
            return

        image_match = re.fullmatch(r"/api/batches/([a-f0-9]+)/images", path)
        if image_match:
            batch_id = image_match.group(1)
            if jobs.active().get("batch_id") == batch_id:
                self._error(409, "Cancel the active batch before deleting its images.")
                return
            try:
                paths = storage.delete_assets(batch_id)
            except KeyError:
                self._error(404, "Batch not found.")
                return
            self._remove_archived_files(paths, batch_id)
            storage.event(batch_id, "warning", f"Deleted {len(paths)} archived image files from this job.")
            self._json(200, {"batch_id": batch_id, "deleted_images": len(paths)})
            return

        match = re.fullmatch(r"/api/batches/([a-f0-9]+)", path)
        if not match:
            self._error(404, "Not found")
            return
        batch_id = match.group(1)
        if jobs.active().get("batch_id") == batch_id:
            self._error(409, "Cancel the active batch before deleting it.")
            return
        try:
            references.cleanup_staged(storage.request_for(batch_id).get("references") or [])
        except KeyError:
            self._error(404, "Batch not found.")
            return
        paths = storage.delete_batch(batch_id)
        self._remove_archived_files(paths, batch_id)
        self._json(200, {"deleted": batch_id})


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the WildCat Export Edition local app.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically.")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    host = str(config.get("host", "127.0.0.1"))
    port = int(args.port or config.get("port", 5192))
    server = AppServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"WildCat Export Edition is running at {url}")
    print("Keep this window open. Press Ctrl+C to stop the app.")
    jobs.autostart()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping WildCat Export Edition...")
    finally:
        server.server_close()


if __name__ == "__main__":
    from .export_server import main as export_main
    export_main()
