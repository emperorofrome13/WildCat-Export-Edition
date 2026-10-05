from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import struct
import tempfile
import threading
import zlib
from pathlib import Path
from typing import Any

from .core import analyze_workflow, read_png_text_metadata


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TEXT_INPUTS = {"text", "prompt", "positive_prompt", "negative_prompt", "caption", "description"}
RESOURCE_HASH_LOCK = threading.RLock()
MODEL_FOLDERS = ("checkpoints", "diffusion_models", "unet")
LORA_FOLDERS = ("loras",)


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 2 and isinstance(value[0], (str, int))


def _node_key(node_id: str) -> tuple[int, int | str]:
    return (0, int(node_id)) if str(node_id).isdigit() else (1, str(node_id))


def _inputs(node: dict[str, Any]) -> dict[str, Any]:
    value = node.get("inputs")
    return value if isinstance(value, dict) else {}


def _walk_upstream(
    graph: dict[str, Any],
    starts: list[str] | tuple[str, ...],
    max_depth: int = 40,
) -> dict[str, int]:
    distances: dict[str, int] = {}
    frontier = [(str(node_id), 0) for node_id in starts]
    while frontier:
        node_id, distance = frontier.pop(0)
        if distance > max_depth or node_id in distances:
            continue
        distances[node_id] = distance
        node = graph.get(node_id)
        if not isinstance(node, dict):
            continue
        links = [
            str(value[0])
            for value in _inputs(node).values()
            if _is_link(value)
        ]
        frontier.extend((linked, distance + 1) for linked in sorted(set(links), key=_node_key))
    return distances


def _branch(graph: dict[str, Any], value: Any) -> dict[str, int]:
    return _walk_upstream(graph, [str(value[0])]) if _is_link(value) else {}


def _ordered_nodes(graph: dict[str, Any], distances: dict[str, int]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (node_id, graph[node_id])
        for node_id in sorted(distances, key=lambda item: (distances[item], _node_key(item)))
        if isinstance(graph.get(node_id), dict)
    ]


def _save_node(graph: dict[str, Any], source_filename: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    source = Path(source_filename).stem.casefold()
    for node_id, node in graph.items():
        class_name = str(node.get("class_type") or "").casefold()
        if "save" not in class_name or "image" not in class_name:
            continue
        prefix = str(_inputs(node).get("filename_prefix") or "")
        prefix_stem = Path(prefix.replace("\\", "/")).name.casefold()
        score = 1
        if prefix_stem and source:
            if source.startswith(prefix_stem):
                score = 5
            elif prefix_stem in source:
                score = 4
        candidates.append((score, str(node_id)))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (-item[0], _node_key(item[1])))[0][1]


def _sampler_node(graph: dict[str, Any], scope: dict[str, int]) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for node_id, node in _ordered_nodes(graph, scope):
        class_name = str(node.get("class_type") or "").casefold()
        inputs = _inputs(node)
        sampler_keys = {"seed", "noise_seed", "steps", "cfg", "cfg_scale", "sampler_name", "scheduler"}
        score = (4 if "sampler" in class_name else 0) + sum(key in inputs for key in sampler_keys)
        if score >= 3:
            candidates.append((score, scope[node_id], node_id))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (-item[0], item[1], _node_key(item[2])))[0][2]


def _scalar(
    graph: dict[str, Any],
    scope: dict[str, int],
    names: tuple[str, ...],
) -> Any:
    for _node_id, node in _ordered_nodes(graph, scope):
        inputs = _inputs(node)
        for name in names:
            value = inputs.get(name)
            if value is not None and not _is_link(value) and not isinstance(value, (dict, list)):
                return value
    return None


def _named_resource(
    graph: dict[str, Any],
    scope: dict[str, int],
    names: tuple[str, ...],
) -> str:
    value = _scalar(graph, scope, names)
    if not isinstance(value, str) or not value.strip():
        return ""
    return Path(value.replace("\\", "/")).stem


def _named_resource_reference(
    graph: dict[str, Any],
    scope: dict[str, int],
    names: tuple[str, ...],
) -> str:
    value = _scalar(graph, scope, names)
    return value.strip() if isinstance(value, str) else ""


def _natural_input_key(name: str) -> tuple[str, int, str]:
    match = re.match(r"^(.*?)(\d+)$", name)
    return (
        match.group(1).casefold() if match else name.casefold(),
        int(match.group(2)) if match else -1,
        name.casefold(),
    )


def _extract_loras(graph: dict[str, Any], scope: dict[str, int]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    # Visit the oldest upstream loaders first so the displayed order follows the model chain.
    ordered = sorted(
        _ordered_nodes(graph, scope),
        key=lambda item: (-scope[item[0]], _node_key(item[0])),
    )
    for node_id, node in ordered:
        inputs = _inputs(node)
        class_name = str(node.get("class_type") or "").casefold()
        candidates: list[tuple[str, Any]] = []
        lora_name = inputs.get("lora_name")
        if isinstance(lora_name, str) and lora_name.strip():
            strength = inputs.get("strength_model", inputs.get("strength", inputs.get("strength_clip", 1)))
            candidates.append((lora_name, strength))
        if "lora" in class_name:
            for input_name in sorted(inputs, key=_natural_input_key):
                value = inputs[input_name]
                if not isinstance(value, dict) or value.get("on") is not True:
                    continue
                nested_name = value.get("lora") or value.get("lora_name")
                if isinstance(nested_name, str) and nested_name.strip():
                    candidates.append((nested_name, value.get("strength", value.get("strength_model", 1))))
        for reference, strength in candidates:
            reference = reference.strip()
            key = (reference.casefold(), _format_number(strength))
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "name": Path(reference.replace("\\", "/")).stem,
                    "reference": reference,
                    "strength": strength,
                    "node_id": node_id,
                }
            )
    return found


def _text_from_branch(graph: dict[str, Any], branch: dict[str, int]) -> str:
    found: list[str] = []
    seen: set[str] = set()
    for _node_id, node in _ordered_nodes(graph, branch):
        for input_name, value in _inputs(node).items():
            if str(input_name).casefold() not in TEXT_INPUTS or not isinstance(value, str):
                continue
            value = value.strip()
            if value and value not in seen:
                found.append(value)
                seen.add(value)
    return "\n".join(found)


def _branch_has_class(graph: dict[str, Any], branch: dict[str, int], class_name: str) -> bool:
    expected = class_name.casefold()
    return any(
        str(graph.get(node_id, {}).get("class_type") or "").casefold() == expected
        for node_id in branch
    )


def _mapped_prompt_text(graph: dict[str, Any], role: str) -> str:
    analysis = analyze_workflow(graph)
    field_ids = analysis[f"recommended_{role}"]
    values: list[str] = []
    seen: set[str] = set()
    for field_id in field_ids:
        node_id, input_name = field_id.rsplit(":", 1)
        value = _inputs(graph.get(node_id, {})).get(input_name)
        if isinstance(value, str):
            value = value.strip()
            if value and value not in seen:
                values.append(value)
                seen.add(value)
    return "\n".join(values)


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        rounded = round(value, 8)
        if abs(value - rounded) < 1e-12:
            value = rounded
        return format(value, ".15g")
    return str(value)


def extract_generation_data(
    graph: dict[str, Any],
    width: int,
    height: int,
    source_filename: str = "",
) -> dict[str, Any]:
    """Deterministically translate values present in a ComfyUI API graph."""
    save_id = _save_node(graph, source_filename)
    scope = _walk_upstream(graph, [save_id]) if save_id else {str(node_id): 0 for node_id in graph}
    sampler_id = _sampler_node(graph, scope)
    sampler_scope = _walk_upstream(graph, [sampler_id]) if sampler_id else scope
    sampler_inputs = _inputs(graph.get(sampler_id, {})) if sampler_id else {}

    positive_branch = _branch(graph, sampler_inputs.get("positive"))
    negative_branch = _branch(graph, sampler_inputs.get("negative"))
    positive = _text_from_branch(graph, positive_branch)
    negative = (
        ""
        if _branch_has_class(graph, negative_branch, "ConditioningZeroOut")
        else _text_from_branch(graph, negative_branch)
    )
    if not positive:
        positive = _mapped_prompt_text(graph, "positive")
    if not negative and not _branch_has_class(graph, negative_branch, "ConditioningZeroOut"):
        negative = _mapped_prompt_text(graph, "negative")

    model_scope = _branch(graph, sampler_inputs.get("model")) or sampler_scope
    model_reference = _named_resource_reference(
        graph,
        model_scope,
        ("ckpt_name", "model_name", "unet_name"),
    )
    model = Path(model_reference.replace("\\", "/")).stem if model_reference else ""
    vae = _named_resource(graph, scope, ("vae_name",))

    clip_value = _scalar(graph, scope, ("clip_skip", "stop_at_clip_layer"))
    if isinstance(clip_value, (int, float)) and clip_value < 0:
        clip_value = abs(clip_value)

    data: dict[str, Any] = {
        "prompt": positive,
        "negative_prompt": negative,
        "steps": _scalar(graph, sampler_scope, ("steps",)),
        "sampler": _scalar(graph, sampler_scope, ("sampler_name",)),
        "scheduler": _scalar(graph, sampler_scope, ("scheduler",)),
        "cfg_scale": _scalar(graph, sampler_scope, ("cfg", "cfg_scale", "guidance")),
        "seed": _scalar(graph, sampler_scope, ("seed", "noise_seed", "random_seed")),
        "width": int(width),
        "height": int(height),
        "model": model,
        "model_reference": model_reference,
        "loras": _extract_loras(graph, model_scope),
        "vae": vae,
        "clip_skip": clip_value,
        "denoising_strength": _scalar(graph, sampler_scope, ("denoise", "denoising_strength")),
    }
    return data


def _models_root(comfy_path: str | Path) -> Path | None:
    root = Path(str(comfy_path)).expanduser()
    candidates = (root / "ComfyUI" / "models", root / "models")
    return next((candidate.resolve() for candidate in candidates if candidate.is_dir()), None)


def _safe_resource_parts(reference: str) -> tuple[str, ...]:
    cleaned = reference.strip().replace("\\", "/").lstrip("/")
    parts = tuple(part for part in cleaned.split("/") if part and part != ".")
    if not parts or any(part == ".." for part in parts) or Path(cleaned).is_absolute():
        return ()
    if parts[0].casefold() == "models":
        parts = parts[1:]
    return parts


def _resolve_resource(models_root: Path, reference: str, folders: tuple[str, ...]) -> Path | None:
    parts = _safe_resource_parts(reference)
    if not parts:
        return None
    folder_names = {folder.casefold() for folder in folders}
    relative = parts[1:] if parts[0].casefold() in folder_names else parts
    exact_candidates = [models_root / folder / Path(*relative) for folder in folders]
    exact_candidates.append(models_root / Path(*parts))
    for candidate in exact_candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(models_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved

    suffix = "/".join(relative).casefold()
    matches: list[Path] = []
    for folder in folders:
        base = models_root / folder
        if not base.is_dir():
            continue
        matches.extend(
            path.resolve()
            for path in base.rglob(Path(relative[-1]).name)
            if path.is_file()
            and path.resolve().relative_to(models_root).as_posix().casefold().endswith(suffix)
        )
    unique = sorted(set(matches), key=lambda path: path.as_posix().casefold())
    return unique[0] if len(unique) == 1 else None


def _read_hash_cache(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_hash_cache(path: Path | None, cache: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _sha256_file(path: Path, offset: int = 0) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        if offset:
            handle.seek(offset)
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safetensors_header(path: Path) -> tuple[dict[str, Any], int]:
    if path.suffix.casefold() != ".safetensors":
        return {}, 0
    try:
        with path.open("rb") as handle:
            length_bytes = handle.read(8)
            if len(length_bytes) != 8:
                return {}, 0
            header_length = struct.unpack("<Q", length_bytes)[0]
            if header_length <= 0 or header_length > 128 * 1024 * 1024:
                return {}, 0
            header = json.loads(handle.read(header_length))
    except (OSError, json.JSONDecodeError, struct.error):
        return {}, 0
    return (header if isinstance(header, dict) else {}), 8 + header_length


def _resource_hash(
    path: Path,
    kind: str,
    hash_cache_path: str | Path | None,
) -> tuple[str, str]:
    cache_path = Path(hash_cache_path) if hash_cache_path else None
    stat_result = path.stat()
    cache_key = str(path.resolve()).casefold()
    with RESOURCE_HASH_LOCK:
        cache = _read_hash_cache(cache_path)
        cached = cache.get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("size") == stat_result.st_size
            and cached.get("mtime_ns") == stat_result.st_mtime_ns
            and isinstance(cached.get("hash"), str)
        ):
            return cached["hash"], str(cached.get("method") or "")

        if kind == "lora":
            header, payload_offset = _safetensors_header(path)
            metadata = header.get("__metadata__") if isinstance(header, dict) else {}
            stored_hash = metadata.get("sshs_model_hash") if isinstance(metadata, dict) else ""
            if isinstance(stored_hash, str) and re.fullmatch(r"[0-9a-fA-F]{12,64}", stored_hash):
                resource_hash = stored_hash.casefold()[:12]
                method = "safetensors payload hash"
            elif payload_offset:
                resource_hash = _sha256_file(path, payload_offset)[:12]
                method = "safetensors payload hash"
            else:
                resource_hash = _sha256_file(path)[:12]
                method = "file SHA-256"
        else:
            resource_hash = _sha256_file(path)[:10]
            method = "file SHA-256"

        cache[cache_key] = {
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "kind": kind,
            "hash": resource_hash,
            "method": method,
        }
        _write_hash_cache(cache_path, cache)
        return resource_hash, method


def attach_resource_hashes(
    data: dict[str, Any],
    comfy_path: str | Path | None,
    hash_cache_path: str | Path | None = None,
) -> dict[str, Any]:
    if not comfy_path:
        return data
    models_root = _models_root(comfy_path)
    if models_root is None:
        return data

    model_reference = str(data.get("model_reference") or "")
    model_path = _resolve_resource(models_root, model_reference, MODEL_FOLDERS) if model_reference else None
    if model_reference:
        model_resource = {
            "name": data.get("model") or Path(model_reference.replace("\\", "/")).stem,
            "reference": model_reference,
            "resolved": bool(model_path),
        }
        if model_path:
            model_hash, method = _resource_hash(model_path, "model", hash_cache_path)
            model_resource.update({"hash": model_hash, "hash_method": method})
            data["model_hash"] = model_hash
        data["model_resource"] = model_resource

    enriched_loras: list[dict[str, Any]] = []
    for original in data.get("loras") or []:
        lora = dict(original)
        reference = str(lora.get("reference") or "")
        lora_path = _resolve_resource(models_root, reference, LORA_FOLDERS) if reference else None
        lora["resolved"] = bool(lora_path)
        if lora_path:
            lora_hash, method = _resource_hash(lora_path, "lora", hash_cache_path)
            lora.update({"hash": lora_hash, "hash_method": method})
        enriched_loras.append(lora)
    data["loras"] = enriched_loras
    return data


def _prompt_with_lora_tags(prompt: str, loras: list[dict[str, Any]]) -> str:
    tags: list[str] = []
    prompt_folded = prompt.casefold()
    for lora in loras:
        name = str(lora.get("name") or "").strip()
        if not name or not lora.get("resolved") or not lora.get("hash"):
            continue
        if f"<lora:{name.casefold()}:" in prompt_folded:
            continue
        tags.append(f"<lora:{name}:{_format_number(lora.get('strength', 1))}>")
    if not tags:
        return prompt
    return "\n".join(part for part in (prompt, " ".join(tags)) if part)


def build_civitai_parameters(data: dict[str, Any]) -> str:
    loras = data.get("loras") if isinstance(data.get("loras"), list) else []
    prompt = _prompt_with_lora_tags(str(data.get("prompt") or "").strip(), loras)
    negative = str(data.get("negative_prompt") or "").strip()
    lora_hashes = [
        f"{lora.get('name')}: {lora.get('hash')}"
        for lora in loras
        if lora.get("name") and lora.get("hash") and lora.get("resolved")
    ]
    settings: list[tuple[str, Any]] = [
        ("Steps", data.get("steps")),
        ("Sampler", data.get("sampler")),
        ("Schedule type", data.get("scheduler")),
        ("CFG scale", data.get("cfg_scale")),
        ("Seed", data.get("seed")),
    ]
    width, height = data.get("width"), data.get("height")
    if width and height:
        settings.append(("Size", f"{int(width)}x{int(height)}"))
    settings.extend(
        [
            ("Model hash", data.get("model_hash")),
            ("Model", data.get("model")),
            ("VAE", data.get("vae")),
            ("Clip skip", data.get("clip_skip")),
            ("Denoising strength", data.get("denoising_strength")),
            ("Lora hashes", f'"{", ".join(lora_hashes)}"' if lora_hashes else ""),
        ]
    )
    settings_line = ", ".join(
        f"{name}: {_format_number(value)}"
        for name, value in settings
        if value is not None and value != ""
    )
    lines = [prompt, f"Negative prompt: {negative}"]
    if settings_line:
        lines.append(settings_line)
    return "\n".join(lines)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or not header.startswith(PNG_SIGNATURE) or header[12:16] != b"IHDR":
        return 0, 0
    return struct.unpack(">II", header[16:24])


def _parameters_chunk_type(path: Path) -> str:
    with path.open("rb") as handle:
        if handle.read(8) != PNG_SIGNATURE:
            return ""
        while True:
            header = handle.read(8)
            if len(header) != 8:
                return ""
            length, chunk_type = struct.unpack(">I4s", header)
            if length > 64 * 1024 * 1024:
                return ""
            if chunk_type in {b"tEXt", b"zTXt", b"iTXt"}:
                data = handle.read(length)
                if data.partition(b"\0")[0] == b"parameters":
                    return chunk_type.decode("ascii")
            else:
                handle.seek(length, 1)
            if len(handle.read(4)) != 4 or chunk_type == b"IEND":
                return ""


def inspect_civitai_metadata(
    path: str | Path,
    source_filename: str = "",
    comfy_path: str | Path | None = None,
    hash_cache_path: str | Path | None = None,
) -> dict[str, Any]:
    target = Path(path)
    if target.suffix.casefold() != ".png":
        return {
            "status": "unsupported",
            "message": "Only PNG images can be converted without changing image pixels.",
            "parameters": "",
            "extracted": {},
        }
    metadata = read_png_text_metadata(target)
    raw_graph = metadata.get("prompt")
    if not raw_graph:
        return {
            "status": "unavailable",
            "message": "No embedded ComfyUI prompt graph was found.",
            "parameters": "",
            "extracted": {},
        }
    try:
        graph = json.loads(raw_graph)
    except json.JSONDecodeError:
        return {
            "status": "unavailable",
            "message": "The embedded ComfyUI prompt graph is invalid.",
            "parameters": "",
            "extracted": {},
        }
    if not isinstance(graph, dict):
        return {
            "status": "unavailable",
            "message": "The embedded ComfyUI prompt graph is invalid.",
            "parameters": "",
            "extracted": {},
        }
    width, height = _png_dimensions(target)
    extracted = extract_generation_data(graph, width, height, source_filename)
    attach_resource_hashes(extracted, comfy_path, hash_cache_path)
    parameters = build_civitai_parameters(extracted)
    current = metadata.get("parameters", "")
    parameters_chunk = _parameters_chunk_type(target)
    status = "compliant" if current == parameters and parameters_chunk == "tEXt" else "ready"
    message = (
        "Civitai parameters already match the embedded ComfyUI metadata and use a PNG tEXt chunk."
        if status == "compliant"
        else (
            "The parameter text matches, but its PNG container must be changed to tEXt for Civitai."
            if current == parameters
            else "Ready for deterministic in-place conversion."
        )
    )
    return {
        "status": status,
        "message": message,
        "parameters": parameters,
        "current_parameters": current,
        "parameters_chunk": parameters_chunk,
        "extracted": extracted,
        "preserved_keys": sorted(key for key in metadata if key != "parameters"),
    }


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _text_keyword(chunk_type: bytes, data: bytes) -> bytes:
    if chunk_type not in {b"tEXt", b"zTXt", b"iTXt"}:
        return b""
    return data.partition(b"\0")[0]


def _rewrite_parameters(path: Path, parameters: str) -> bool:
    raw = path.read_bytes()
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError("Image is not a valid PNG.")
    position = len(PNG_SIGNATURE)
    output = bytearray(PNG_SIGNATURE)
    inserted = False
    found_iend = False
    while position + 12 <= len(raw):
        length = struct.unpack(">I", raw[position : position + 4])[0]
        end = position + 12 + length
        if end > len(raw):
            raise ValueError("PNG metadata is truncated.")
        chunk_type = raw[position + 4 : position + 8]
        data = raw[position + 8 : position + 8 + length]
        original = raw[position:end]
        if _text_keyword(chunk_type, data) == b"parameters":
            position = end
            continue
        if chunk_type == b"IEND":
            payload = b"parameters\0" + parameters.encode("utf-8")
            output.extend(_chunk(b"tEXt", payload))
            inserted = True
            found_iend = True
        output.extend(original)
        position = end
        if chunk_type == b"IEND":
            break
    if not inserted or not found_iend:
        raise ValueError("PNG is missing its final IEND chunk.")
    rewritten = bytes(output)
    if rewritten == raw:
        return False
    original_mode = stat.S_IMODE(path.stat().st_mode)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(rewritten)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return True


def convert_png_to_civitai(
    path: str | Path,
    source_filename: str = "",
    comfy_path: str | Path | None = None,
    hash_cache_path: str | Path | None = None,
) -> dict[str, Any]:
    target = Path(path)
    result = inspect_civitai_metadata(target, source_filename, comfy_path, hash_cache_path)
    if result["status"] not in {"ready", "compliant"}:
        return result
    if result["status"] == "compliant":
        result["changed"] = False
        return result
    result["changed"] = _rewrite_parameters(target, result["parameters"])
    result["status"] = "compliant"
    result["message"] = (
        "Civitai parameters were written in a PNG tEXt chunk; "
        "ComfyUI metadata and image pixels were preserved."
    )
    return result
