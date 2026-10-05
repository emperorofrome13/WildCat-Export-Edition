from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_sha256(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _like_escape(value: str) -> str:
    """Escape SQL LIKE wildcards so filenames match literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def image_dimensions(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                return struct.unpack(">II", header[16:24])
            if header[:6] in {b"GIF87a", b"GIF89a"} and len(header) >= 10:
                return struct.unpack("<HH", header[6:10])
            if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
                if header[12:16] == b"VP8X" and len(header) >= 30:
                    return 1 + int.from_bytes(header[24:27], "little"), 1 + int.from_bytes(header[27:30], "little")
                if header[12:16] == b"VP8L" and len(header) >= 25 and header[20] == 0x2F:
                    bits = int.from_bytes(header[21:25], "little")
                    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
                if header[12:16] == b"VP8 " and len(header) >= 30 and header[23:26] == b"\x9d\x01\x2a":
                    return int.from_bytes(header[26:28], "little") & 0x3FFF, int.from_bytes(header[28:30], "little") & 0x3FFF
            if not header.startswith(b"\xff\xd8"):
                return 0, 0
            handle.seek(2)
            start_of_frame = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            while True:
                byte = handle.read(1)
                if not byte:
                    break
                if byte != b"\xff":
                    continue
                marker = handle.read(1)
                while marker == b"\xff":
                    marker = handle.read(1)
                if not marker or marker in {b"\x01", *[bytes([value]) for value in range(0xD0, 0xD9)]}:
                    continue
                length_bytes = handle.read(2)
                if len(length_bytes) != 2:
                    break
                length = int.from_bytes(length_bytes, "big")
                if marker[0] in start_of_frame:
                    frame = handle.read(5)
                    if len(frame) == 5:
                        return int.from_bytes(frame[3:5], "big"), int.from_bytes(frame[1:3], "big")
                    break
                handle.seek(max(0, length - 2), 1)
    except (OSError, ValueError, struct.error):
        return 0, 0
    return 0, 0


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    brief TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT '',
                    request_json TEXT NOT NULL,
                    total_prompts INTEGER NOT NULL DEFAULT 0,
                    completed_runs INTEGER NOT NULL DEFAULT 0,
                    total_runs INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    paused INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prompts (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    guide TEXT NOT NULL DEFAULT '',
                    reference_id TEXT NOT NULL DEFAULT '',
                    reference_name TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL,
                    original_prompt TEXT NOT NULL DEFAULT '',
                    regeneration_count INTEGER NOT NULL DEFAULT 0,
                    negative_prompt TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    completed_runs INTEGER NOT NULL DEFAULT 0,
                    target_runs INTEGER NOT NULL DEFAULT 1,
                    seed INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, position)
                );

                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL DEFAULT 'images',
                    filename TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    source_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    rating INTEGER NOT NULL DEFAULT 0,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS questions (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    prompt_id TEXT REFERENCES prompts(id) ON DELETE SET NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS comparisons (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    focus TEXT NOT NULL DEFAULT '',
                    samples_per_guide INTEGER NOT NULL DEFAULT 1,
                    guide_samples_json TEXT NOT NULL DEFAULT '{}',
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT REFERENCES batches(id) ON DELETE CASCADE,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_prompts_batch ON prompts(batch_id, position);
                CREATE INDEX IF NOT EXISTS idx_assets_prompt ON assets(prompt_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id, id);
                """
            )
            asset_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(assets)").fetchall()}
            if "rating" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN rating INTEGER NOT NULL DEFAULT 0")
            if "width" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN width INTEGER NOT NULL DEFAULT 0")
            if "height" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN height INTEGER NOT NULL DEFAULT 0")
            if "file_hash" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN file_hash TEXT NOT NULL DEFAULT ''")
            if "civitai_posted" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN civitai_posted INTEGER NOT NULL DEFAULT 0")
            if "civitai_post_id" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN civitai_post_id INTEGER")
            if "civitai_url" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN civitai_url TEXT NOT NULL DEFAULT ''")
            if "phash" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN phash TEXT NOT NULL DEFAULT ''")
            prompt_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(prompts)").fetchall()}
            if "reference_id" not in prompt_columns:
                db.execute("ALTER TABLE prompts ADD COLUMN reference_id TEXT NOT NULL DEFAULT ''")
            if "reference_name" not in prompt_columns:
                db.execute("ALTER TABLE prompts ADD COLUMN reference_name TEXT NOT NULL DEFAULT ''")
            if "original_prompt" not in prompt_columns:
                db.execute("ALTER TABLE prompts ADD COLUMN original_prompt TEXT NOT NULL DEFAULT ''")
            if "regeneration_count" not in prompt_columns:
                db.execute("ALTER TABLE prompts ADD COLUMN regeneration_count INTEGER NOT NULL DEFAULT 0")
            db.execute(
                "UPDATE batches SET status='interrupted', phase='Stopped when WildCat Harness closed', "
                "updated_at=? WHERE status IN ('running','pausing','paused')",
                (utc_now(),),
            )
            db.execute(
                "UPDATE prompts SET status='pending',error='Prompt regeneration was interrupted; ready to retry',updated_at=? "
                "WHERE status='regenerating'",
                (utc_now(),),
            )

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_batch(self, request: dict[str, Any]) -> str:
        batch_id = uuid.uuid4().hex
        now = utc_now()
        title = str(request.get("title") or request.get("brief") or "Untitled batch").strip()[:160]
        source = str(request.get("source") or "paste")
        with self._lock, self._db() as db:
            db.execute(
                """INSERT INTO batches
                (id,title,source,brief,status,phase,request_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    title,
                    source,
                    str(request.get("brief") or "").strip(),
                    "queued",
                    "Waiting to start",
                    json.dumps(request, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.event(batch_id, "info", "Batch created.")
        return batch_id

    def batch(self, batch_id: str, include_details: bool = True) -> dict[str, Any] | None:
        with self._lock, self._db() as db:
            row = db.execute("SELECT rowid AS _queue_sequence, * FROM batches WHERE id=?", (batch_id,)).fetchone()
            batch = self._dict(row)
            if not batch:
                return None
            queue_sequence = batch.pop("_queue_sequence")
            batch["request"] = json.loads(batch.pop("request_json"))
            if batch["status"] == "queued":
                batch["queue_position"] = db.execute(
                    """SELECT 1 + COUNT(*) FROM batches q
                    WHERE q.status='queued' AND (
                        q.created_at < ? OR (q.created_at = ? AND q.rowid < ?)
                    )""",
                    (batch["created_at"], batch["created_at"], queue_sequence),
                ).fetchone()[0]
            else:
                batch["queue_position"] = None
            if include_details:
                prompt_rows = db.execute(
                    "SELECT * FROM prompts WHERE batch_id=? ORDER BY position", (batch_id,)
                ).fetchall()
                prompts = [dict(item) for item in prompt_rows]
                for prompt in prompts:
                    assets = db.execute(
                        "SELECT * FROM assets WHERE prompt_id=? ORDER BY created_at", (prompt["id"],)
                    ).fetchall()
                    prompt["assets"] = [dict(asset) for asset in assets]
                    for asset in prompt["assets"]:
                        asset["source"] = json.loads(asset.pop("source_json"))
                batch["prompts"] = prompts
                questions = db.execute(
                    "SELECT * FROM questions WHERE batch_id=? ORDER BY created_at DESC", (batch_id,)
                ).fetchall()
                batch["questions"] = [dict(question) for question in questions]
                comparisons = db.execute(
                    "SELECT * FROM comparisons WHERE batch_id=? ORDER BY created_at DESC", (batch_id,)
                ).fetchall()
                batch["comparisons"] = [dict(comparison) for comparison in comparisons]
                for comparison in batch["comparisons"]:
                    comparison["guide_samples"] = json.loads(comparison.pop("guide_samples_json"))
                events = db.execute(
                    "SELECT level,message,created_at FROM events WHERE batch_id=? ORDER BY id DESC LIMIT 100",
                    (batch_id,),
                ).fetchall()
                batch["events"] = [dict(event) for event in reversed(events)]
            return batch

    def list_batches(self, search: str = "") -> list[dict[str, Any]]:
        like = f"%{search.strip()}%"
        with self._lock, self._db() as db:
            rows = db.execute(
                """
                SELECT b.*,
                       (SELECT COUNT(*) FROM assets a WHERE a.batch_id=b.id) AS asset_count,
                       (SELECT local_path FROM assets a WHERE a.batch_id=b.id ORDER BY a.created_at LIMIT 1) AS cover_path,
                       CASE WHEN b.status='queued' THEN 1 + (
                           SELECT COUNT(*) FROM batches q WHERE q.status='queued' AND (
                               q.created_at < b.created_at OR (q.created_at = b.created_at AND q.rowid < b.rowid)
                           )
                       ) ELSE NULL END AS queue_position
                FROM batches b
                WHERE (?='' OR b.title LIKE ? OR b.brief LIKE ? OR EXISTS (
                    SELECT 1 FROM prompts p WHERE p.batch_id=b.id AND (p.prompt LIKE ? OR p.guide LIKE ?)
                ))
                ORDER BY b.created_at DESC
                """,
                (search.strip(), like, like, like, like),
            ).fetchall()
            results = [dict(row) for row in rows]
            for item in results:
                item.pop("request_json", None)
            return results

    def guide_leaderboard(self) -> dict[str, Any]:
        """Aggregate image ratings by markdown guide across every saved batch."""
        with self._lock, self._db() as db:
            rows = db.execute(
                """
                SELECT p.guide,a.batch_id,a.rating,a.created_at,a.civitai_posted
                FROM assets a
                JOIN prompts p ON p.id=a.prompt_id
                WHERE a.kind='images' AND TRIM(p.guide)<>''
                ORDER BY a.created_at
                """
            ).fetchall()

        grouped: dict[str, dict[str, Any]] = {}
        all_batches: set[str] = set()
        for row in rows:
            guide = " ".join(str(row["guide"] or "").split())
            if not guide:
                continue
            key = guide.casefold()
            record = grouped.setdefault(
                key,
                {
                    "guide": guide,
                    "total_images": 0,
                    "rated_images": 0,
                    "posted_images": 0,
                    "points": 0,
                    "batches": set(),
                    "ratings": {str(value): 0 for value in range(1, 6)},
                    "last_generated": "",
                },
            )
            record["total_images"] += 1
            record["batches"].add(str(row["batch_id"]))
            all_batches.add(str(row["batch_id"]))
            record["last_generated"] = max(record["last_generated"], str(row["created_at"] or ""))
            if int(row["civitai_posted"] or 0):
                record["posted_images"] += 1
            rating = int(row["rating"] or 0)
            if 1 <= rating <= 5:
                record["rated_images"] += 1
                record["points"] += rating
                record["ratings"][str(rating)] += 1

        guides: list[dict[str, Any]] = []
        for record in grouped.values():
            rated = int(record["rated_images"])
            total = int(record["total_images"])
            guides.append(
                {
                    "guide": record["guide"],
                    "average_rating": round(record["points"] / rated, 3) if rated else None,
                    "rated_images": rated,
                    "total_images": total,
                    "posted_images": int(record["posted_images"]),
                    "rating_coverage": round((rated / total) * 100, 1) if total else 0,
                    "batch_count": len(record["batches"]),
                    "ratings": record["ratings"],
                    "last_generated": record["last_generated"],
                }
            )
        guides.sort(
            key=lambda item: (
                item["average_rating"] is None,
                -(item["average_rating"] or 0),
                -item["rated_images"],
                item["guide"].casefold(),
            )
        )
        rank = 0
        for item in guides:
            if item["average_rating"] is not None:
                rank += 1
                item["rank"] = rank
            else:
                item["rank"] = None

        rated_images = sum(item["rated_images"] for item in guides)
        total_points = sum(
            sum(int(stars) * count for stars, count in item["ratings"].items())
            for item in guides
        )
        return {
            "guides": guides,
            "summary": {
                "total_guides": len(guides),
                "ranked_guides": rank,
                "batch_count": len(all_batches),
                "total_images": sum(item["total_images"] for item in guides),
                "rated_images": rated_images,
                "posted_images": sum(item["posted_images"] for item in guides),
                "average_rating": round(total_points / rated_images, 3) if rated_images else None,
            },
        }

    def request_for(self, batch_id: str) -> dict[str, Any]:
        with self._lock, self._db() as db:
            row = db.execute("SELECT request_json FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise KeyError(batch_id)
            return json.loads(row[0])

    def update_batch(self, batch_id: str, **changes: Any) -> None:
        allowed = {
            "title", "status", "phase", "total_prompts", "completed_runs", "total_runs",
            "error", "paused", "cancel_requested", "brief"
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self._lock, self._db() as db:
            db.execute(f"UPDATE batches SET {assignments} WHERE id=?", (*values.values(), batch_id))

    def add_prompts(self, batch_id: str, records: Iterable[dict[str, Any]], target_runs: int) -> int:
        records = list(records)
        if not records:
            return 0
        now = utc_now()
        with self._lock, self._db() as db:
            position = db.execute(
                "SELECT COALESCE(MAX(position),0) FROM prompts WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
            for record in records:
                position += 1
                record_target_runs = max(1, int(record.get("target_runs", target_runs)))
                db.execute(
                    """INSERT INTO prompts
                    (id,batch_id,position,guide,reference_id,reference_name,prompt,negative_prompt,status,target_runs,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        batch_id,
                        position,
                        str(record.get("guide") or ""),
                        str(record.get("reference_id") or ""),
                        str(record.get("reference_name") or ""),
                        str(record.get("prompt") or "").strip(),
                        str(record.get("negative_prompt") or "").strip(),
                        "pending",
                        record_target_runs,
                        now,
                        now,
                    ),
                )
            total_prompts = db.execute(
                "SELECT COUNT(*) FROM prompts WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
            total_runs = db.execute(
                "SELECT COALESCE(SUM(target_runs),0) FROM prompts WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
            db.execute(
                "UPDATE batches SET total_prompts=?,total_runs=?,updated_at=? WHERE id=?",
(total_prompts, total_runs, now, batch_id),
            )
        return len(records)

    def clone_batch_for_rerun(
        self,
        batch_id: str,
        workflow_id: str = "",
        workflow_name: str = "",
        selection: str = "all",
    ) -> str:
        original = self.batch(batch_id)
        if not original:
            raise KeyError(batch_id)
        prompts = original.get("prompts") or []
        if not prompts:
            raise ValueError("This job has no prompts to run again.")
        if selection == "errors":
            prompts = [p for p in prompts if str(p.get("error") or "").strip()]
            if not prompts:
                raise ValueError("This job has no failed prompts to rerun.")
        elif selection == "unrated":
            prompts = [
                p for p in prompts
                if not any(int(asset.get("rating") or 0) > 0 for asset in (p.get("assets") or []))
            ]
            if not prompts:
                raise ValueError("Every image in this job is rated; there is nothing unrated to rerun.")
        request = dict(original["request"])
        base_title = str(original.get("title") or "Untitled batch")
        title_suffix = " · rerun"
        if workflow_id:
            request["workflow_id"] = str(workflow_id)
            if workflow_name:
                request["workflow_name"] = workflow_name
                title_suffix = f" · rerun via {workflow_name}"
        request["title"] = f"{base_title}{title_suffix}"[:160]
        request["rerun_of"] = batch_id
        new_batch_id = self.create_batch(request)
        self.add_prompts(
            new_batch_id,
            [
                {
                    "guide": prompt.get("guide", ""),
                    "reference_id": prompt.get("reference_id", ""),
                    "reference_name": prompt.get("reference_name", ""),
                    "prompt": prompt.get("prompt", ""),
                    "negative_prompt": prompt.get("negative_prompt", ""),
                    "target_runs": prompt.get("target_runs", 1),
                }
                for prompt in prompts
            ],
            1,
        )
        self.event(new_batch_id, "info", f"Copied {len(prompts)} prompts from job {batch_id[:8]} for a rerun (selection: {selection}).")
        return new_batch_id

    def guide_prompt_counts(self, batch_id: str) -> dict[str, int]:
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT guide,COUNT(*) AS count FROM prompts WHERE batch_id=? GROUP BY guide", (batch_id,)
            ).fetchall()
            return {str(row["guide"]): int(row["count"]) for row in rows}

    def guide_reference_prompt_counts(self, batch_id: str) -> dict[tuple[str, str], int]:
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT guide,reference_id,COUNT(*) AS count FROM prompts
                WHERE batch_id=? AND reference_id != '' GROUP BY guide,reference_id""",
                (batch_id,),
            ).fetchall()
            return {
                (str(row["guide"]), str(row["reference_id"])): int(row["count"])
                for row in rows
            }

    def pending_prompts(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT * FROM prompts WHERE batch_id=? AND completed_runs < target_runs
                AND status != 'cancelled' ORDER BY position""",
                (batch_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_prompt(self, prompt_id: str, **changes: Any) -> None:
        allowed = {"status", "completed_runs", "seed", "error"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self._lock, self._db() as db:
            db.execute(f"UPDATE prompts SET {assignments} WHERE id=?", (*values.values(), prompt_id))

    def replace_prompt_text(self, prompt_id: str, replacement: str) -> dict[str, Any]:
        replacement = str(replacement or "").strip()
        if not replacement:
            raise ValueError("The replacement prompt is empty.")
        now = utc_now()
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT prompt,original_prompt,regeneration_count FROM prompts WHERE id=?",
                (prompt_id,),
            ).fetchone()
            if not row:
                raise KeyError(prompt_id)
            original = str(row["original_prompt"] or row["prompt"])
            count = int(row["regeneration_count"] or 0) + 1
            db.execute(
                """UPDATE prompts SET prompt=?,original_prompt=?,regeneration_count=?,status='pending',error='',updated_at=?
                WHERE id=?""",
                (replacement, original, count, now, prompt_id),
            )
            return {"prompt": replacement, "original_prompt": original, "regeneration_count": count}

    def skip_prompt(self, batch_id: str, prompt_id: str) -> None:
        now = utc_now()
        with self._lock, self._db() as db:
            updated = db.execute(
                """UPDATE prompts SET target_runs=completed_runs,status='skipped',error='Skipped by user',updated_at=?
                WHERE id=? AND batch_id=?""",
                (now, prompt_id, batch_id),
            )
            if updated.rowcount != 1:
                raise KeyError(prompt_id)
            totals = db.execute(
                "SELECT COALESCE(SUM(completed_runs),0),COALESCE(SUM(target_runs),0) FROM prompts WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            db.execute(
                "UPDATE batches SET completed_runs=?,total_runs=?,updated_at=? WHERE id=?",
                (int(totals[0]), int(totals[1]), now, batch_id),
            )

    def prepare_prompt_rerun(self, batch_id: str, prompt_id: str) -> dict[str, int]:
        now = utc_now()
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT completed_runs,target_runs FROM prompts WHERE id=? AND batch_id=?",
                (prompt_id, batch_id),
            ).fetchone()
            if not row:
                raise KeyError(prompt_id)
            completed = int(row["completed_runs"])
            target = int(row["target_runs"])
            if completed >= target:
                target = completed + 1
            db.execute(
                "UPDATE prompts SET target_runs=?,status='pending',error='',updated_at=? WHERE id=?",
                (target, now, prompt_id),
            )
            totals = db.execute(
                "SELECT COALESCE(SUM(completed_runs),0),COALESCE(SUM(target_runs),0) FROM prompts WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            db.execute(
                "UPDATE batches SET completed_runs=?,total_runs=?,updated_at=? WHERE id=?",
                (int(totals[0]), int(totals[1]), now, batch_id),
            )
            return {"completed_runs": completed, "target_runs": target}

    def increment_run(self, batch_id: str, prompt_id: str, seed: int) -> tuple[int, int]:
        now = utc_now()
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT completed_runs,target_runs FROM prompts WHERE id=?", (prompt_id,)
            ).fetchone()
            completed = int(row[0]) + 1
            target = int(row[1])
            status = "complete" if completed >= target else "pending"
            db.execute(
                "UPDATE prompts SET completed_runs=?,seed=?,status=?,error='',updated_at=? WHERE id=?",
                (completed, seed, status, now, prompt_id),
            )
            batch_completed = db.execute(
                "SELECT COALESCE(SUM(completed_runs),0) FROM prompts WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
            db.execute(
                "UPDATE batches SET completed_runs=?,updated_at=? WHERE id=?",
                (batch_completed, now, batch_id),
            )
            return completed, target

    def add_asset(
        self,
        batch_id: str,
        prompt_id: str,
        kind: str,
        filename: str,
        local_path: str,
        source: dict[str, Any],
    ) -> str:
        asset_id = uuid.uuid4().hex
        archive_root = self.path.parent.parent.resolve()
        archive_path = (archive_root / local_path).resolve()
        try:
            archive_path.relative_to(archive_root)
        except ValueError:
            width, height = 0, 0
            file_hash = ""
            phash = ""
        else:
            from .core import dhash_file
            width, height = image_dimensions(archive_path)
            file_hash = _file_sha256(archive_path)
            phash = dhash_file(archive_path)
        with self._lock, self._db() as db:
            db.execute(
                """INSERT INTO assets
                (id,batch_id,prompt_id,kind,filename,local_path,source_json,created_at,rating,width,height,file_hash,phash)
                VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                (
                    asset_id,
                    batch_id,
                    prompt_id,
                    kind,
                    filename,
                    local_path,
                    json.dumps(source, ensure_ascii=False),
                    utc_now(),
                    width,
                    height,
                    file_hash,
                    phash,
                ),
            )
        return asset_id

    def backfill_asset_dimensions(self, root: Path) -> int:
        root = root.resolve()
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT id,local_path FROM assets WHERE width<=0 OR height<=0"
            ).fetchall()
        updates: list[tuple[int, int, str]] = []
        for row in rows:
            target = (root / str(row["local_path"])).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            width, height = image_dimensions(target)
            if width > 0 and height > 0:
                updates.append((width, height, str(row["id"])))
        if updates:
            with self._lock, self._db() as db:
                db.executemany("UPDATE assets SET width=?,height=? WHERE id=?", updates)
        return len(updates)

    def rate_asset(self, asset_id: str, rating: int) -> dict[str, Any]:
        rating = int(rating)
        if rating < 0 or rating > 5:
            raise ValueError("Image rating must be between 0 and 5 stars.")
        with self._lock, self._db() as db:
            updated = db.execute(
                "UPDATE assets SET rating=? WHERE id=?",
                (rating, asset_id),
            )
            if updated.rowcount != 1:
                raise KeyError(asset_id)
            row = db.execute(
                "SELECT id,batch_id,prompt_id,rating FROM assets WHERE id=?",
                (asset_id,),
            ).fetchone()
            return dict(row)

    def asset(self, asset_id: str) -> dict[str, Any] | None:
        with self._lock, self._db() as db:
            row = db.execute(
                """SELECT a.*,p.position,p.guide,b.title AS batch_title
                FROM assets a
                JOIN prompts p ON p.id=a.prompt_id
                JOIN batches b ON b.id=a.batch_id
                WHERE a.id=?""",
                (asset_id,),
            ).fetchone()
        item = self._dict(row)
        if item:
            try:
                item["source"] = json.loads(item.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item["source"] = {}
        return item

    def assets_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT a.*,p.position,p.guide
                FROM assets a JOIN prompts p ON p.id=a.prompt_id
                WHERE a.batch_id=? ORDER BY p.position,a.created_at,a.id""",
                (batch_id,),
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            try:
                item["source"] = json.loads(item.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item["source"] = {}
        return items

    def image_tool_results(self) -> list[dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT a.*,p.position,p.guide,p.prompt,p.negative_prompt,p.seed,
                p.completed_runs,p.target_runs,p.error,b.title AS batch_title
                FROM assets a
                JOIN prompts p ON p.id=a.prompt_id
                JOIN batches b ON b.id=a.batch_id
                WHERE a.source_json LIKE '%\"image_tool_kind\"%'
                ORDER BY a.created_at DESC,a.id DESC"""
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            try:
                item["source"] = json.loads(item.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item["source"] = {}
        return items

    def unfinished_batches_for_workflow(self, workflow_id: str) -> list[dict[str, str]]:
        unfinished_statuses = {"queued", "running", "pausing", "paused", "interrupted", "failed", "complete_with_errors"}
        matches: list[dict[str, str]] = []
        with self._lock, self._db() as db:
            rows = db.execute("SELECT id,title,status,request_json FROM batches").fetchall()
            for row in rows:
                if str(row["status"]) not in unfinished_statuses:
                    continue
                try:
                    request = json.loads(row["request_json"])
                except json.JSONDecodeError:
                    continue
                if str(request.get("workflow_id") or "") == workflow_id:
                    matches.append({"id": str(row["id"]), "title": str(row["title"]), "status": str(row["status"])})
        return matches

    def batches_with_unrecorded_assets(self) -> list[str]:
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT DISTINCT batch_id FROM assets
                WHERE source_json NOT LIKE '%"vram_relay"%' ORDER BY batch_id"""
            ).fetchall()
            return [str(row[0]) for row in rows]

    def unrecorded_assets(self, batch_id: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT id,local_path,source_json FROM assets
                WHERE batch_id=? AND source_json NOT LIKE '%"vram_relay"%'
                ORDER BY created_at LIMIT ?""",
                (batch_id, max(1, int(limit))),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_inferred_workflow_provenance(self, batch_id: str, provenance: dict[str, Any]) -> int:
        updated = 0
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT id,source_json FROM assets
                WHERE batch_id=? AND source_json NOT LIKE '%"vram_relay"%'""",
                (batch_id,),
            ).fetchall()
            for row in rows:
                try:
                    source = json.loads(row["source_json"] or "{}")
                except json.JSONDecodeError:
                    source = {}
                source["vram_relay"] = dict(provenance)
                db.execute(
                    "UPDATE assets SET source_json=? WHERE id=?",
                    (json.dumps(source, ensure_ascii=False), row["id"]),
                )
                updated += 1
        return updated

    def add_question(self, batch_id: str, prompt_id: str | None, question: str, answer: str) -> str:
        item_id = uuid.uuid4().hex
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO questions VALUES (?,?,?,?,?,?)",
                (item_id, batch_id, prompt_id, question, answer, utc_now()),
            )
        return item_id

    def event(self, batch_id: str | None, level: str, message: str) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO events(batch_id,level,message,created_at) VALUES (?,?,?,?)",
                (batch_id, level, message, utc_now()),
            )

    def controls(self, batch_id: str) -> dict[str, bool]:
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT paused,cancel_requested FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
            if not row:
                return {"paused": False, "cancel_requested": True}
            return {"paused": bool(row[0]), "cancel_requested": bool(row[1])}

    def next_queued(self) -> str | None:
        with self._lock, self._db() as db:
            row = db.execute(
                """SELECT id FROM batches
                WHERE status='queued' AND cancel_requested=0
                ORDER BY created_at,rowid LIMIT 1"""
            ).fetchone()
            return str(row[0]) if row else None

    def cancel_all_queued(self) -> int:
        now = utc_now()
        with self._lock, self._db() as db:
            updated = db.execute(
                """UPDATE batches SET status='cancelled',cancel_requested=1,paused=0,
                phase='Cancelled by queue stop',error='Queue stopped by user',updated_at=?
                WHERE status='queued' AND cancel_requested=0""",
                (now,),
            ).rowcount
        return updated

    def queued_count(self) -> int:
        with self._lock, self._db() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM batches WHERE status='queued' AND cancel_requested=0"
            ).fetchone()[0])

    def add_comparison(
        self,
        batch_id: str,
        model: str,
        focus: str,
        samples_per_guide: int,
        guide_samples: dict[str, list[str]],
        result: str,
    ) -> str:
        item_id = uuid.uuid4().hex
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO comparisons VALUES (?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    batch_id,
                    model,
                    focus,
                    samples_per_guide,
                    json.dumps(guide_samples, ensure_ascii=False),
                    result,
                    utc_now(),
                ),
            )
        return item_id

    def delete_batch(self, batch_id: str) -> list[str]:
        with self._lock, self._db() as db:
            paths = [row[0] for row in db.execute("SELECT local_path FROM assets WHERE batch_id=?", (batch_id,))]
            db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            return paths

    def delete_assets(self, batch_id: str) -> list[str]:
        with self._lock, self._db() as db:
            exists = db.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not exists:
                raise KeyError(batch_id)
            paths = [row[0] for row in db.execute(
                "SELECT local_path FROM assets WHERE batch_id=?", (batch_id,)
            )]
            db.execute("DELETE FROM assets WHERE batch_id=?", (batch_id,))
            return paths

    def delete_asset(self, asset_id: str) -> str:
        with self._lock, self._db() as db:
            row = db.execute("SELECT local_path FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not row:
                raise KeyError(asset_id)
            db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            return str(row[0])

    def find_asset_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        if not file_hash:
            return None
        with self._lock, self._db() as db:
            row = db.execute(
                """SELECT a.*,p.position,p.guide,b.title AS batch_title
                FROM assets a
                JOIN prompts p ON p.id=a.prompt_id
                JOIN batches b ON b.id=a.batch_id
                WHERE a.file_hash=? LIMIT 1""",
                (file_hash,),
            ).fetchone()
        item = self._dict(row)
        if item:
            try:
                item["source"] = json.loads(item.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item["source"] = {}
            item.pop("file_hash", None)
        return item

    def find_asset_by_filename(self, filename: str) -> dict[str, Any] | None:
        """Search assets by original ComfyUI filename or WildCat archived filename."""
        if not filename:
            return None
        bare = filename.strip().split("/")[-1].split("\\")[-1]
        if not bare:
            return None
        like_bare = f"%{_like_escape(bare)}%"
        source_like = f'%"filename": "{_like_escape(bare)}"%'
        with self._lock, self._db() as db:
            row = db.execute(
                r"""SELECT a.*,p.position,p.guide,b.title AS batch_title
                FROM assets a
                JOIN prompts p ON p.id=a.prompt_id
                JOIN batches b ON b.id=a.batch_id
                WHERE a.filename=? OR a.local_path LIKE ? ESCAPE '\'
                ORDER BY a.created_at DESC LIMIT 1""",
                (bare, like_bare),
            ).fetchone()
            if not row:
                row = db.execute(
                    r"""SELECT a.*,p.position,p.guide,b.title AS batch_title
                    FROM assets a
                    JOIN prompts p ON p.id=a.prompt_id
                    JOIN batches b ON b.id=a.batch_id
                    WHERE a.source_json LIKE ? ESCAPE '\'
                    ORDER BY a.created_at DESC LIMIT 1""",
                    (source_like,),
                ).fetchone()
        if not row:
            return None
        item = self._dict(row)
        if item:
            try:
                item["source"] = json.loads(item.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item["source"] = {}
            item.pop("file_hash", None)
        return item

    def backfill_asset_hashes(self, root: Path) -> int:
        root = root.resolve()
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT id,local_path FROM assets WHERE file_hash=''"
            ).fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            target = (root / str(row["local_path"])).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            file_hash = _file_sha256(target)
            if file_hash:
                updates.append((file_hash, str(row["id"])))
        if updates:
            with self._lock, self._db() as db:
                db.executemany("UPDATE assets SET file_hash=? WHERE id=?", updates)
        return len(updates)

    def backfill_asset_phashes(self, root: Path) -> int:
        from .core import dhash_file
        root = root.resolve()
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT id,local_path FROM assets WHERE phash=''"
            ).fetchall()
        updates: list[tuple[str, str]] = []
        total = 0
        for row in rows:
            target = (root / str(row["local_path"])).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            phash = dhash_file(target)
            if phash:
                updates.append((phash, str(row["id"])))
            if len(updates) >= 200:
                with self._lock, self._db() as db:
                    db.executemany("UPDATE assets SET phash=? WHERE id=?", updates)
                total += len(updates)
                updates = []
        if updates:
            with self._lock, self._db() as db:
                db.executemany("UPDATE assets SET phash=? WHERE id=?", updates)
            total += len(updates)
        return total

    def find_similar_assets(self, target_hash: str, max_distance: int = 12, limit: int = 12) -> list[dict[str, Any]]:
        from .core import hamming_distance_hex
        if not target_hash:
            return []
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT a.*,p.position,p.guide,b.title AS batch_title
                FROM assets a
                JOIN prompts p ON p.id=a.prompt_id
                JOIN batches b ON b.id=a.batch_id
                WHERE a.phash<>''"""
            ).fetchall()
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            distance = hamming_distance_hex(target_hash, str(row["phash"]))
            if distance is None or distance > max_distance:
                continue
            item = dict(row)
            item.pop("phash", None)
            try:
                item["source"] = json.loads(item.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item["source"] = {}
            item["hamming_distance"] = distance
            item.pop("file_hash", None)
            scored.append((distance, item))
        scored.sort(key=lambda pair: (pair[0], str(pair[1].get("created_at"))))
        return [item for _distance, item in scored[:max(1, int(limit))]]

    def civitai_match_candidates(self) -> list[dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                """SELECT a.id,a.width,a.height,a.civitai_posted,p.seed
                FROM assets a JOIN prompts p ON p.id=a.prompt_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_civitai(self, asset_id: str, posted: bool, post_id: int | None = None, url: str = "") -> dict[str, Any] | None:
        with self._lock, self._db() as db:
            exists = db.execute("SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not exists:
                raise KeyError(asset_id)
            db.execute(
                "UPDATE assets SET civitai_posted=?,civitai_post_id=?,civitai_url=? WHERE id=?",
                (1 if posted else 0, int(post_id) if posted and post_id else None, url if posted else "", asset_id),
            )
            row = db.execute(
                "SELECT id,civitai_posted,civitai_post_id,civitai_url FROM assets WHERE id=?", (asset_id,)
            ).fetchone()
            return self._dict(row)

    def civitai_counts(self) -> dict[str, int]:
        with self._lock, self._db() as db:
            total = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            posted = db.execute("SELECT COUNT(*) FROM assets WHERE civitai_posted=1").fetchone()[0]
            return {"total_images": int(total), "posted_images": int(posted)}

    def unposted_asset_ids(self, batch_id: str) -> list[str]:
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT id FROM assets WHERE batch_id=? AND civitai_posted=0 ORDER BY created_at",
                (batch_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def civitai_post_context(self, asset_id: str) -> dict[str, Any] | None:
        with self._lock, self._db() as db:
            row = db.execute(
                """SELECT a.*,p.prompt,p.negative_prompt,p.seed,b.title AS batch_title
                FROM assets a
                JOIN prompts p ON p.id=a.prompt_id
                JOIN batches b ON b.id=a.batch_id
                WHERE a.id=?""",
                (asset_id,),
            ).fetchone()
        item = self._dict(row)
        if item:
            try:
                item["source"] = json.loads(item.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item["source"] = {}
        return item

    def assets_by_ids(self, asset_ids: list[str]) -> list[dict[str, Any]]:
        if not asset_ids:
            return []
        placeholders = ",".join("?" for _ in asset_ids)
        with self._lock, self._db() as db:
            rows = db.execute(
                f"SELECT a.*,p.prompt,p.negative_prompt,p.seed,b.title AS batch_title "
                f"FROM assets a JOIN prompts p ON p.id=a.prompt_id JOIN batches b ON b.id=a.batch_id "
                f"WHERE a.id IN ({placeholders})",
                tuple(asset_ids),
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            try:
                item["source"] = json.loads(item.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item["source"] = {}
        return items

