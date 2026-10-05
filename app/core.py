from __future__ import annotations

import copy
import json
import random
import re
import struct
import zlib
from pathlib import Path
from typing import Any


NUMBERED_PROMPT = re.compile(
    r"(?ms)(?:^|\n)\s*(?:\*\*)?(\d+)[.)](?:\*\*)?\s+(.*?)"
    r"(?=(?:\n\s*(?:\*\*)?\d+[.)](?:\*\*)?\s+)|\Z)"
)
NEGATIVE_LINE = re.compile(
    r"(?im)^\s*(?:\*\*)?negative\s+prompt(?:\*\*)?\s*:\s*"
)


def read_png_text_metadata(path: str | Path) -> dict[str, str]:
    """Read PNG textual chunks without decoding the image pixels."""
    metadata: dict[str, str] = {}
    with Path(path).open("rb") as handle:
        if handle.read(8) != b"\x89PNG\r\n\x1a\n":
            return metadata
        while True:
            header = handle.read(8)
            if len(header) != 8:
                break
            length, chunk_type = struct.unpack(">I4s", header)
            if length > 64 * 1024 * 1024:
                break
            if chunk_type in {b"tEXt", b"zTXt", b"iTXt"}:
                data = handle.read(length)
            else:
                handle.seek(length, 1)
                data = b""
            if len(handle.read(4)) != 4:
                break
            try:
                if chunk_type == b"tEXt":
                    keyword, separator, value = data.partition(b"\0")
                    if separator:
                        metadata[keyword.decode("latin-1")] = value.decode("utf-8", "replace")
                elif chunk_type == b"zTXt":
                    keyword, separator, remainder = data.partition(b"\0")
                    if separator and len(remainder) > 1 and remainder[0] == 0:
                        metadata[keyword.decode("latin-1")] = zlib.decompress(remainder[1:]).decode(
                            "utf-8", "replace"
                        )
                elif chunk_type == b"iTXt":
                    keyword, separator, remainder = data.partition(b"\0")
                    if not separator or len(remainder) < 2:
                        continue
                    compressed, compression_method = remainder[0], remainder[1]
                    remainder = remainder[2:]
                    _language, separator, remainder = remainder.partition(b"\0")
                    if not separator:
                        continue
                    _translated, separator, value = remainder.partition(b"\0")
                    if not separator:
                        continue
                    if compressed == 1 and compression_method == 0:
                        value = zlib.decompress(value)
                    metadata[keyword.decode("latin-1")] = value.decode("utf-8", "replace")
            except (UnicodeDecodeError, zlib.error):
                continue
            if chunk_type == b"IEND":
                break
    return metadata


def read_comfy_prompt_graph(path: str | Path) -> dict[str, Any] | None:
    """Return the executed API prompt graph embedded by ComfyUI in a PNG."""
    raw = read_png_text_metadata(path).get("prompt")
    if not raw:
        return None
    try:
        graph = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return graph if isinstance(graph, dict) else None


def _workflow_values_equal(left: Any, right: Any) -> bool:
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return float(left) == float(right)
    return left == right


def workflow_match_score(
    executed: dict[str, Any],
    candidate: dict[str, Any],
    variable_fields: list[str] | set[str] | tuple[str, ...] = (),
) -> float:
    """Score an embedded executed graph against an API workflow from 0 to 1."""
    variable = set(variable_fields)
    score = 0
    possible = 0
    for node_id in set(executed) | set(candidate):
        left = executed.get(node_id)
        right = candidate.get(node_id)
        possible += 20
        if not isinstance(left, dict) or not isinstance(right, dict):
            continue
        if left.get("class_type") != right.get("class_type"):
            continue
        score += 20
        class_type = str(left.get("class_type") or "")
        left_inputs = left.get("inputs") if isinstance(left.get("inputs"), dict) else {}
        right_inputs = right.get("inputs") if isinstance(right.get("inputs"), dict) else {}
        for input_name in set(left_inputs) | set(right_inputs):
            possible += 2
            if input_name not in left_inputs or input_name not in right_inputs:
                continue
            field_id = f"{node_id}:{input_name}"
            normalized_name = str(input_name).lower()
            changes_per_run = (
                field_id in variable
                or normalized_name in {"seed", "noise_seed"}
                or (class_type == "SaveImage" and normalized_name == "filename_prefix")
            )
            if changes_per_run or _workflow_values_equal(left_inputs[input_name], right_inputs[input_name]):
                score += 2
    return score / possible if possible else 0.0


def clean_prompt_text(value: str) -> str:
    value = value.strip().strip("`").strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_numbered_prompts(text: str) -> list[dict[str, str]]:
    """Parse common LM Studio numbered-list output into prompt records."""
    text = clean_prompt_text(text)
    matches = list(NUMBERED_PROMPT.finditer(text))
    blocks = [match.group(2).strip() for match in matches]

    if not blocks:
        blocks = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    if not blocks and text:
        blocks = [text]

    prompts: list[dict[str, str]] = []
    for block in blocks:
        split = NEGATIVE_LINE.split(block, maxsplit=1)
        positive = clean_prompt_text(split[0])
        negative = clean_prompt_text(split[1]) if len(split) > 1 else ""
        positive = re.sub(r"(?i)^positive\s+prompt\s*:\s*", "", positive).strip()
        if positive:
            prompts.append({"prompt": positive, "negative_prompt": negative})
    return prompts


def split_pasted_prompts(text: str) -> list[dict[str, str]]:
    parsed = parse_numbered_prompts(text)
    if len(parsed) > 1:
        return parsed

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1 and not any(line.lower().startswith("negative prompt") for line in lines):
        return [{"prompt": re.sub(r"^[-*]\s+", "", line), "negative_prompt": ""} for line in lines]
    return parsed


def _unwrap_workflow(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
        data = data["prompt"]
    if not isinstance(data, dict):
        raise ValueError("Workflow must be a JSON object.")
    if "nodes" in data and isinstance(data.get("nodes"), list):
        raise ValueError(
            "This is a normal ComfyUI workflow. In ComfyUI, enable Dev Mode and use "
            "Export Workflow (API), then upload that API-format JSON here."
        )
    nodes = {str(key): value for key, value in data.items() if isinstance(value, dict)}
    if not nodes or not all("class_type" in node and isinstance(node.get("inputs"), dict) for node in nodes.values()):
        raise ValueError("This file is not a ComfyUI API-format workflow.")
    return nodes


def load_workflow_file(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return _unwrap_workflow(json.load(handle))


def validate_workflow_json(content: str) -> dict[str, Any]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"Workflow JSON is invalid: {error.msg}.") from error
    return _unwrap_workflow(data)


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 2 and isinstance(value[0], (str, int))


def _text_fields(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    allowed = {"text", "prompt", "positive_prompt", "negative_prompt", "caption", "description"}
    for node_id, node in workflow.items():
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        title = str(meta.get("title") or node.get("class_type") or f"Node {node_id}")
        for input_name, value in node.get("inputs", {}).items():
            lowered = str(input_name).lower()
            if isinstance(value, str) and (lowered in allowed or "prompt" in lowered):
                fields.append(
                    {
                        "field_id": f"{node_id}:{input_name}",
                        "node_id": str(node_id),
                        "input": str(input_name),
                        "title": title,
                        "class_type": str(node.get("class_type", "")),
                        "sample": value[:180],
                    }
                )
    return fields


def _upstream_nodes(workflow: dict[str, Any], start_id: str, depth: int = 7) -> set[str]:
    found: set[str] = set()
    frontier = [str(start_id)]
    for _ in range(depth):
        next_frontier: list[str] = []
        for node_id in frontier:
            if node_id in found:
                continue
            found.add(node_id)
            node = workflow.get(node_id, {})
            for value in node.get("inputs", {}).values():
                if _is_link(value):
                    next_frontier.append(str(value[0]))
        frontier = next_frontier
        if not frontier:
            break
    return found


def analyze_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    fields = _text_fields(workflow)
    positive_nodes: set[str] = set()
    negative_nodes: set[str] = set()

    for node in workflow.values():
        inputs = node.get("inputs", {})
        for role, destination in (("positive", positive_nodes), ("negative", negative_nodes)):
            value = inputs.get(role)
            if _is_link(value):
                destination.update(_upstream_nodes(workflow, str(value[0])))

    positive: list[str] = []
    negative: list[str] = []
    for field in fields:
        searchable = f"{field['title']} {field['input']}".lower()
        field_id = field["field_id"]
        if field["node_id"] in negative_nodes or "negative" in searchable:
            negative.append(field_id)
        elif field["node_id"] in positive_nodes or "positive" in searchable:
            positive.append(field_id)

    if not positive:
        positive = [field["field_id"] for field in fields if "negative" not in f"{field['title']} {field['input']}".lower()]
    if not positive and fields:
        positive = [fields[0]["field_id"]]

    for field in fields:
        if field["field_id"] in positive:
            field["recommended"] = "positive"
        elif field["field_id"] in negative:
            field["recommended"] = "negative"
        else:
            field["recommended"] = ""

    seed_fields = []
    for node_id, node in workflow.items():
        for input_name, value in node.get("inputs", {}).items():
            if str(input_name).lower() in {"seed", "noise_seed", "random_seed"} and isinstance(value, int):
                seed_fields.append(f"{node_id}:{input_name}")

    return {
        "node_count": len(workflow),
        "fields": fields,
        "recommended_positive": positive,
        "recommended_negative": negative,
        "seed_fields": seed_fields,
    }


def analyze_image_inputs(workflow: dict[str, Any]) -> list[dict[str, str]]:
    """Find file-backed image inputs that can receive a gallery image."""
    fields: list[dict[str, str]] = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type") or "")
        normalized_class = re.sub(r"[^a-z]", "", class_type.lower())
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        title = str(meta.get("title") or class_type or f"Node {node_id}")
        for input_name, value in node.get("inputs", {}).items():
            normalized_input = str(input_name).lower()
            is_image_loader = "loadimage" in normalized_class
            if (
                isinstance(value, str)
                and not _is_link(value)
                and normalized_input in {"image", "image_path", "filename"}
                and is_image_loader
            ):
                fields.append(
                    {
                        "field_id": f"{node_id}:{input_name}",
                        "node_id": str(node_id),
                        "input": str(input_name),
                        "title": title,
                        "class_type": class_type,
                        "sample": value,
                    }
                )
    return fields


def _set_field(workflow: dict[str, Any], field_id: str, value: str) -> None:
    if ":" not in field_id:
        raise ValueError(f"Invalid workflow field: {field_id}")
    node_id, input_name = field_id.rsplit(":", 1)
    node = workflow.get(str(node_id))
    if not node or input_name not in node.get("inputs", {}):
        raise ValueError(f"Workflow field no longer exists: {field_id}")
    node["inputs"][input_name] = value


def prepare_image_workflow(
    base_workflow: dict[str, Any],
    uploaded_image_name: str,
    image_fields: list[str] | None = None,
    instruction: str = "",
    positive_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Copy an image-tool graph and map its image plus an optional edit instruction."""
    workflow = copy.deepcopy(base_workflow)
    detected = analyze_image_inputs(workflow)
    selected = image_fields or [field["field_id"] for field in detected]
    if not selected:
        raise ValueError("This API workflow has no supported LoadImage input.")
    known = {field["field_id"] for field in detected}
    unknown = [field_id for field_id in selected if field_id not in known]
    if unknown:
        raise ValueError(f"Image input no longer exists: {unknown[0]}")
    for field_id in selected:
        _set_field(workflow, field_id, uploaded_image_name)
    instruction = str(instruction or "").strip()
    if instruction:
        analysis = analyze_workflow(workflow)
        selected_text = positive_fields or analysis["recommended_positive"]
        if not selected_text:
            raise ValueError("This editing API workflow has no supported positive text input.")
        known_text = {field["field_id"] for field in analysis["fields"]}
        unknown_text = [field_id for field_id in selected_text if field_id not in known_text]
        if unknown_text:
            raise ValueError(f"Edit instruction input no longer exists: {unknown_text[0]}")
        for field_id in selected_text:
            _set_field(workflow, field_id, instruction)
    return workflow


def prepare_workflow(
    base_workflow: dict[str, Any],
    prompt: str,
    negative_prompt: str,
    positive_fields: list[str] | None = None,
    negative_fields: list[str] | None = None,
    seed: int | None = None,
) -> tuple[dict[str, Any], int]:
    workflow = copy.deepcopy(base_workflow)
    analysis = analyze_workflow(workflow)
    positive_fields = positive_fields or analysis["recommended_positive"]
    negative_fields = negative_fields if negative_fields is not None else analysis["recommended_negative"]

    if not positive_fields:
        raise ValueError("No positive prompt text field is selected in the workflow.")
    for field_id in positive_fields:
        _set_field(workflow, field_id, prompt)
    for field_id in negative_fields:
        _set_field(workflow, field_id, negative_prompt)

    chosen_seed = seed if seed is not None else random.SystemRandom().randint(1, 9_007_199_254_740_991)
    for field_id in analysis["seed_fields"]:
        node_id, input_name = field_id.rsplit(":", 1)
        workflow[node_id]["inputs"][input_name] = chosen_seed
    return workflow, chosen_seed


def safe_filename(value: str, fallback: str = "file") -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" ._")
    value = re.sub(r"\s+", " ", value)
    return (value[:120] or fallback).strip()


def dhash_bytes(data: bytes) -> str:
    """64-bit difference hash (dHash) of image bytes, as 16 hex chars. Empty on failure."""
    try:
        import importlib
        io_mod = importlib.import_module("io")
        pil_image_mod = importlib.import_module("PIL.Image")
        img = pil_image_mod.open(io_mod.BytesIO(data)).convert("L").resize((9, 8), pil_image_mod.LANCZOS)
        pixels = list(img.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                left = pixels[row * 9 + col]
                right = pixels[row * 9 + col + 1]
                bits = (bits << 1) | (1 if left > right else 0)
        img.close()
        return f"{bits:016x}"
    except Exception:
        return ""


def dhash_file(path: str | Path) -> str:
    try:
        return dhash_bytes(Path(path).read_bytes())
    except (OSError, ValueError):
        return ""


def hamming_distance_hex(left: str, right: str) -> int | None:
    """Hamming distance between two 64-bit hex hashes; None if either is empty."""
    try:
        if not left or not right:
            return None
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError:
        return None
