from __future__ import annotations

import json
import struct
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


class RecyclePixelBin:
    """Retain one RGBA pixel per deleted archive image in fixed-size PNG canvases."""

    COLLAGE_WIDTH = 512
    COLLAGE_HEIGHT = 512

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.index_path = self.folder / "recycled-pixels.json"
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self.COLLAGE_WIDTH * self.COLLAGE_HEIGHT

    def collage_path(self, number: int) -> Path:
        if number < 1:
            raise ValueError("Recycle collage number must be positive.")
        return self.folder / f"recycle-collage-{number:04d}.png"

    def _entries(self) -> list[dict[str, Any]]:
        if not self.index_path.is_file():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def validate_pixel(pixel: Any) -> list[int]:
        if not isinstance(pixel, list) or len(pixel) not in {3, 4}:
            raise ValueError("Could not sample a recycle pixel from this image. Reload the gallery and try again.")
        values = [int(value) for value in pixel]
        if len(values) == 3:
            values.append(255)
        if any(value < 0 or value > 255 for value in values):
            raise ValueError("The recycle pixel contains an invalid color value.")
        return values

    def add(self, asset: dict[str, Any], pixel: Any) -> dict[str, Any]:
        rgba = self.validate_pixel(pixel)
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            entries = self._entries()
            entries.append(
                {
                    "asset_id": str(asset.get("id") or ""),
                    "filename": str(asset.get("filename") or "deleted image"),
                    "batch_id": str(asset.get("batch_id") or ""),
                    "batch_title": str(asset.get("batch_title") or ""),
                    "rgba": rgba,
                    "deleted_at": _utc_now(),
                }
            )
            temp_index = self.index_path.with_suffix(".json.tmp")
            temp_index.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_index.replace(self.index_path)
            page_number = ((len(entries) - 1) // self.capacity) + 1
            self._write_collage_page(entries, page_number)
            return self._summary(entries)

    def add_external(self, filename: str, source_path: str, pixel: Any) -> dict[str, Any]:
        """Add a pixel from an external file that is not in the WildCat archive."""
        rgba = self.validate_pixel(pixel)
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            entries = self._entries()
            entries.append(
                {
                    "asset_id": "",
                    "filename": filename,
                    "batch_id": "",
                    "batch_title": f"External: {source_path}",
                    "rgba": rgba,
                    "deleted_at": _utc_now(),
                }
            )
            temp_index = self.index_path.with_suffix(".json.tmp")
            temp_index.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_index.replace(self.index_path)
            page_number = ((len(entries) - 1) // self.capacity) + 1
            self._write_collage_page(entries, page_number)
            return self._summary(entries)

    def _write_collage_page(self, entries: list[dict[str, Any]], number: int) -> None:
        start = (number - 1) * self.capacity
        page_entries = entries[start : start + self.capacity]
        pixels = bytearray(self.capacity * 4)
        for index, entry in enumerate(page_entries):
            offset = index * 4
            pixels[offset : offset + 4] = bytes(self.validate_pixel(entry.get("rgba")))

        raw = bytearray()
        row_bytes = self.COLLAGE_WIDTH * 4
        for row in range(self.COLLAGE_HEIGHT):
            start_offset = row * row_bytes
            raw.append(0)
            raw.extend(pixels[start_offset : start_offset + row_bytes])

        png = bytearray(b"\x89PNG\r\n\x1a\n")
        png.extend(
            _png_chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", self.COLLAGE_WIDTH, self.COLLAGE_HEIGHT, 8, 6, 0, 0, 0),
            )
        )
        png.extend(_png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9)))
        png.extend(_png_chunk(b"IEND", b""))
        target = self.collage_path(number)
        temporary = target.with_suffix(".png.tmp")
        temporary.write_bytes(png)
        temporary.replace(target)

    def _summary(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(entries)
        collage_count = ((count - 1) // self.capacity) + 1 if count else 0
        collages = []
        for number in range(1, collage_count + 1):
            pixel_count = min(self.capacity, count - ((number - 1) * self.capacity))
            collages.append(
                {
                    "number": number,
                    "pixel_count": pixel_count,
                    "capacity": self.capacity,
                    "complete": pixel_count == self.capacity,
                    "url": f"/recycle-bin/collage-{number:04d}.png",
                }
            )
        current_pixels = collages[-1]["pixel_count"] if collages else 0
        return {
            "count": count,
            "width": self.COLLAGE_WIDTH,
            "height": self.COLLAGE_HEIGHT,
            "capacity_per_collage": self.capacity,
            "collage_count": collage_count,
            "current_collage": collage_count,
            "current_pixels": current_pixels,
            "current_percent": round((current_pixels / self.capacity) * 100, 4) if count else 0,
            "collage_url": collages[-1]["url"] if collages else "",
            "collages": collages,
            "latest": list(reversed(entries[-12:])),
        }

    def summary(self) -> dict[str, Any]:
        with self._lock:
            entries = self._entries()
            collage_count = ((len(entries) - 1) // self.capacity) + 1 if entries else 0
            for number in range(1, collage_count + 1):
                if not self.collage_path(number).is_file():
                    self.folder.mkdir(parents=True, exist_ok=True)
                    self._write_collage_page(entries, number)
            return self._summary(entries)
