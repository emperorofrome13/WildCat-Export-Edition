from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT
from .core import safe_filename


GUIDE_DIR = ROOT / "data" / "guides"
MAX_GUIDE_BYTES = 5 * 1024 * 1024


class GuideLibrary:
    def __init__(self) -> None:
        GUIDE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _id(path: Path) -> str:
        return hashlib.sha256(path.name.casefold().encode("utf-8")).hexdigest()[:32]

    def _entry(self, path: Path, include_content: bool = True) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8-sig") if include_content else ""
        stat = path.stat()
        return {
            "id": self._id(path),
            "name": path.name,
            "content": content,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

    def list(self, include_content: bool = True) -> list[dict[str, Any]]:
        entries = [
            self._entry(path, include_content)
            for path in GUIDE_DIR.iterdir()
            if path.is_file() and path.suffix.casefold() == ".md"
        ]
        return sorted(entries, key=lambda item: str(item["name"]).casefold())

    def save(self, name: str, content: str) -> dict[str, Any]:
        filename = safe_filename(Path(str(name or "guide.md")).name, "guide.md")
        if Path(filename).suffix.casefold() != ".md":
            raise ValueError("WildCat guide files must use the .md extension.")
        encoded = str(content).encode("utf-8")
        if not encoded.strip():
            raise ValueError("The Markdown guide is empty.")
        if len(encoded) > MAX_GUIDE_BYTES:
            raise ValueError("A Markdown guide cannot exceed 5 MB.")
        target = (GUIDE_DIR / filename).resolve()
        try:
            target.relative_to(GUIDE_DIR.resolve())
        except ValueError as error:
            raise ValueError("The guide filename is outside WildCat's guide library.") from error
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(encoded)
        temporary.replace(target)
        return self._entry(target)

    def delete(self, guide_id: str) -> dict[str, Any]:
        entry = next((item for item in self.list(False) if item["id"] == guide_id), None)
        if not entry:
            raise KeyError(guide_id)
        target = (GUIDE_DIR / str(entry["name"])).resolve()
        try:
            target.relative_to(GUIDE_DIR.resolve())
        except ValueError as error:
            raise ValueError("The guide path is outside WildCat's guide library.") from error
        target.unlink()
        return entry
