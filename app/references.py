from __future__ import annotations

import base64
import json
import mimetypes
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from .config import ROOT
from .core import safe_filename


REFERENCE_DIR = ROOT / "data" / "references"
ORIGINAL_DIR = REFERENCE_DIR / "originals"
PREVIEW_DIR = REFERENCE_DIR / "processed"
CATALOG_PATH = REFERENCE_DIR / "catalog.json"
QUEUE_DIR = ROOT / "data" / "reference-queue"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReferenceLibrary:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        if not CATALOG_PATH.exists():
            self._write([])

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        return data if isinstance(data, list) else []

    def _write(self, entries: list[dict[str, Any]]) -> None:
        temporary = CATALOG_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(CATALOG_PATH)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = self._read()
        return sorted(entries, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def get(self, reference_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list() if item.get("id") == reference_id), None)

    def save_source(
        self,
        name: str,
        media_type: str,
        content_type: str,
        stream: BinaryIO,
        length: int,
    ) -> dict[str, Any]:
        reference_id = uuid.uuid4().hex
        clean_name = safe_filename(name, "reference")
        suffix = Path(clean_name).suffix.lower()
        if not suffix:
            suffix = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ".bin"
        target = ORIGINAL_DIR / f"{reference_id}-{Path(clean_name).stem}{suffix}"
        temporary = target.with_suffix(target.suffix + ".part")
        remaining = length
        try:
            with temporary.open("wb") as handle:
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("The reference upload ended before the complete file arrived.")
                    handle.write(chunk)
                    remaining -= len(chunk)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        entry = {
            "id": reference_id,
            "name": clean_name,
            "media_type": media_type if media_type in {"image", "video"} else "image",
            "content_type": content_type,
            "source_path": target.relative_to(ROOT).as_posix(),
            "source_size": length,
            "processed_path": "",
            "processed_content_type": "",
            "width": 0,
            "height": 0,
            "remove_ui": True,
            "crop": {},
            "created_at": _now(),
        }
        with self._lock:
            entries = self._read()
            entries.append(entry)
            self._write(entries)
        return entry

    def save_processed(
        self,
        reference_id: str,
        data_url: str,
        width: int,
        height: int,
        remove_ui: bool,
        crop: dict[str, Any],
    ) -> dict[str, Any]:
        if not data_url.startswith("data:image/") or "," not in data_url:
            raise ValueError("Processed reference must be an image data URL.")
        header, encoded = data_url.split(",", 1)
        mime = header[5:].split(";", 1)[0].lower()
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime)
        if not extension:
            raise ValueError("Processed references must be JPEG, PNG, or WebP images.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("Processed reference image data is invalid.") from error
        if len(content) > 15 * 1024 * 1024:
            raise ValueError("Processed reference image exceeds 15 MB.")
        target = PREVIEW_DIR / f"{reference_id}{extension}"
        target.write_bytes(content)
        with self._lock:
            entries = self._read()
            entry = next((item for item in entries if item.get("id") == reference_id), None)
            if not entry:
                target.unlink(missing_ok=True)
                raise KeyError(reference_id)
            entry.update(
                {
                    "processed_path": target.relative_to(ROOT).as_posix(),
                    "processed_content_type": mime,
                    "width": max(1, int(width)),
                    "height": max(1, int(height)),
                    "remove_ui": bool(remove_ui),
                    "crop": crop if isinstance(crop, dict) else {},
                }
            )
            self._write(entries)
            return dict(entry)

    def selected(self, reference_ids: list[str]) -> list[dict[str, Any]]:
        wanted = {str(item) for item in reference_ids}
        entries = [item for item in self.list() if item.get("id") in wanted]
        by_id = {str(item["id"]): item for item in entries}
        ordered = [by_id[item] for item in reference_ids if item in by_id]
        for entry in ordered:
            path = (ROOT / str(entry.get("processed_path") or "")).resolve()
            try:
                path.relative_to(PREVIEW_DIR.resolve())
            except ValueError as error:
                raise ValueError("A reference preview path is outside the WildCat archive.") from error
            if not path.is_file():
                raise ValueError(f"Reference {entry.get('name') or entry.get('id')} is not ready.")
        return ordered

    def prompt_images(self, entries: list[dict[str, Any]]) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []
        for entry in entries:
            path = (ROOT / str(entry["processed_path"])).resolve()
            mime = str(entry.get("processed_content_type") or mimetypes.guess_type(path.name)[0] or "image/jpeg")
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            images.append({
                "id": str(entry.get("id") or ""),
                "name": str(entry.get("name") or path.name),
                "dataUrl": f"data:{mime};base64,{encoded}",
            })
        return images

    def stage(self, reference_ids: list[str]) -> list[dict[str, Any]]:
        """Move selected tray uploads into an isolated temporary queue folder."""
        entries = self.selected(reference_ids)
        if not entries:
            return []
        queue_id = uuid.uuid4().hex
        queue_root = QUEUE_DIR / queue_id
        source_root = queue_root / "originals"
        processed_root = queue_root / "processed"
        source_root.mkdir(parents=True, exist_ok=False)
        processed_root.mkdir(parents=True, exist_ok=False)
        staged: list[dict[str, Any]] = []
        originals_to_remove: list[Path] = []
        try:
            for entry in entries:
                item = dict(entry)
                for key, allowed_root, destination_root in (
                    ("source_path", ORIGINAL_DIR, source_root),
                    ("processed_path", PREVIEW_DIR, processed_root),
                ):
                    source = (ROOT / str(entry.get(key) or "")).resolve()
                    try:
                        source.relative_to(allowed_root.resolve())
                    except ValueError as error:
                        raise ValueError("A reference path is outside the temporary upload tray.") from error
                    if not source.is_file():
                        raise ValueError(f"Reference {entry.get('name') or entry.get('id')} is not ready.")
                    destination = destination_root / source.name
                    shutil.copy2(source, destination)
                    item[key] = destination.relative_to(ROOT).as_posix()
                    originals_to_remove.append(source)
                item["temporary"] = True
                item["queue_id"] = queue_id
                staged.append(item)
            with self._lock:
                current = self._read()
                claimed = {str(entry["id"]) for entry in entries}
                self._write([item for item in current if str(item.get("id")) not in claimed])
            for source in originals_to_remove:
                source.unlink(missing_ok=True)
            return staged
        except Exception:
            shutil.rmtree(queue_root, ignore_errors=True)
            raise

    def cleanup_staged(self, entries: list[dict[str, Any]]) -> int:
        """Delete temporary queue folders referenced by a completed batch."""
        queue_ids = {
            str(entry.get("queue_id") or "")
            for entry in entries
            if isinstance(entry, dict) and entry.get("temporary")
        }
        removed = 0
        for queue_id in queue_ids:
            if not queue_id or any(character not in "0123456789abcdef" for character in queue_id.lower()):
                continue
            target = (QUEUE_DIR / queue_id).resolve()
            try:
                target.relative_to(QUEUE_DIR.resolve())
            except ValueError:
                continue
            if target.is_dir():
                shutil.rmtree(target)
                removed += 1
        return removed

    def media_path(self, reference_id: str) -> Path:
        entry = self.get(reference_id)
        if not entry:
            raise KeyError(reference_id)
        target = (ROOT / str(entry.get("processed_path") or "")).resolve()
        try:
            target.relative_to(PREVIEW_DIR.resolve())
        except ValueError as error:
            raise ValueError("Reference preview path is outside the WildCat archive.") from error
        if not target.is_file():
            raise ValueError("Reference preview is not ready.")
        return target

    def delete(self, reference_id: str) -> dict[str, Any]:
        with self._lock:
            entries = self._read()
            entry = next((item for item in entries if item.get("id") == reference_id), None)
            if not entry:
                raise KeyError(reference_id)
            remaining = [item for item in entries if item.get("id") != reference_id]
            self._write(remaining)
        for key, root in (("source_path", ORIGINAL_DIR), ("processed_path", PREVIEW_DIR)):
            value = str(entry.get(key) or "")
            if not value:
                continue
            target = (ROOT / value).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                continue
            target.unlink(missing_ok=True)
        return entry
