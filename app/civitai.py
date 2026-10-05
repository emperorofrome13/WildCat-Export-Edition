from __future__ import annotations

import hashlib
import json
import math
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://civitai.red"
POST_URL_TEMPLATE = "https://civitai.red/posts/{post_id}"


class CivitaiError(RuntimeError):
    """Raised for any Civitai API failure; surfaced to the UI as a 409."""


def _request(
    url: str,
    method: str = "GET",
    token: str | None = None,
    payload: Any = None,
    raw_data: bytes | None = None,
    content_type: str = "application/json",
    timeout: float = 60,
) -> Any:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise CivitaiError("Civitai requests require a secure HTTPS URL without embedded credentials.")
    if token and parsed.hostname not in {"civitai.com", "civitai.red"}:
        raise CivitaiError("Refusing to send a Civitai credential to another host.")
    if not token:
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise CivitaiError("Refusing a non-public Civitai upload destination.")
        except socket.gaierror as error:
            raise CivitaiError("Could not resolve the Civitai upload destination.") from error
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 WildCatHarness/1.0"
        ),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    elif raw_data is not None:
        headers["Content-Type"] = content_type
        data = raw_data
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        from .providers import NoRedirect
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            body = response.read(32 * 1024 * 1024 + 1)
            if len(body) > 32 * 1024 * 1024:
                raise CivitaiError("Civitai response exceeded 32 MB.")
    except urllib.error.HTTPError as error:
        raise CivitaiError(f"Civitai request failed (HTTP {error.code}). Check your key, account access and image settings.") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CivitaiError(f"Could not reach Civitai: {error}") from error
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return body


def get_me(token: str) -> dict[str, Any]:
    return _request(f"{BASE_URL}/api/v1/me", token=token)


def fetch_user_images(token: str, username: str, cursor: str | None = None, limit: int = 200) -> dict[str, Any]:
    # browsingLevel=15 = all levels (None+Soft+Mature+X); without it the listing is
    # capped at the public/SFW level and most NSFW posts are silently missing.
    query = f"username={urllib.parse.quote(username)}&limit={limit}&withMeta=true&sort=Newest&browsingLevel=15"
    if cursor:
        query += f"&cursor={urllib.parse.quote(str(cursor))}"
    return _request(f"{BASE_URL}/api/v1/images?{query}", token=token, timeout=60)


def request_upload_url(token: str, filename: str, size: int, mime: str) -> dict[str, Any]:
    payload = {"filename": filename, "size": int(size), "mimeType": mime}
    return _request(
        f"{BASE_URL}/api/v1/image-upload",
        method="POST",
        token=token,
        payload=payload,
        timeout=60,
    )


def upload_image_bytes(upload_url: str, data: bytes, mime: str) -> None:
    _request(upload_url, method="PUT", raw_data=data, content_type=mime, timeout=300)


def create_post_with_images(
    token: str,
    title: str,
    detail: str,
    images: list[dict[str, Any]],
    publish: bool = False,
    model_version_id: int | None = None,
) -> dict[str, Any]:
    input_data: dict[str, Any] = {"publish": bool(publish), "images": images}
    if title:
        input_data["title"] = title
    if detail:
        input_data["detail"] = detail
    if model_version_id:
        input_data["modelVersionId"] = int(model_version_id)
    response = _request(
        f"{BASE_URL}/api/trpc/post.createWithImages",
        method="POST",
        token=token,
        payload={"json": input_data},
        timeout=120,
    )
    if isinstance(response, dict) and response.get("error"):
        message = response["error"].get("json", {}).get("message", "unknown tRPC error")
        raise CivitaiError(f"Civitai post creation failed: {message}")
    result = (response.get("result") or {}).get("data") or {}
    return result.get("json") or result or {}


def update_image_nsfw_level(token: str, image_id: int, level: str) -> dict[str, Any]:
    """level: one of 'None' | 'Soft' | 'Mature' | 'X' (Civitai NsfwLevel enum)."""
    if level not in {"None", "Soft", "Mature", "X"}:
        raise CivitaiError(f"Unknown NSFW level: {level}")
    response = _request(
        f"{BASE_URL}/api/trpc/image.updateImageNsfwLevel",
        method="POST",
        token=token,
        payload={"json": {"id": int(image_id), "nsfwLevel": level}},
        timeout=60,
    )
    if isinstance(response, dict) and response.get("error"):
        message = response["error"].get("json", {}).get("message", "unknown tRPC error")
        raise CivitaiError(f"Civitai NSFW update failed: {message}")
    result = (response.get("result") or {}).get("data") or {}
    return result.get("json") or result or {}


def delete_post(token: str, post_id: int) -> dict[str, Any]:
    return _request(
        f"{BASE_URL}/api/trpc/post.delete",
        method="POST",
        token=token,
        payload={"json": {"id": int(post_id)}},
        timeout=60,
    )


def build_generation_meta(
    prompt: str,
    negative_prompt: str,
    seed: int | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"tool": "WildCat Harness"}
    if prompt:
        meta["prompt"] = prompt
    if negative_prompt:
        meta["negativePrompt"] = negative_prompt
    if seed is not None:
        meta["seed"] = int(seed)
    if extra:
        meta.update({key: value for key, value in extra.items() if value not in (None, "")})
    return meta


def match_civitai_items(
    assets: list[dict[str, Any]], items: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Match archived assets against a Civitai listing by seed + dimensions.

    Returns {asset_id: {"post_id": int, "url": str, "civitai_image_id": int}}.
    Assets already flagged as posted are never re-matched.
    """
    matches: dict[str, dict[str, Any]] = {}
    for item in items:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        seed = meta.get("seed")
        post_id = item.get("postId")
        if seed is None or not post_id:
            continue
        try:
            seed_value = int(seed)
            post_value = int(post_id)
        except (TypeError, ValueError):
            continue
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        for asset in assets:
            if asset.get("civitai_posted"):
                continue
            asset_seed = asset.get("seed")
            if asset_seed is None or int(asset_seed) != seed_value:
                continue
            asset_width = int(asset.get("width") or 0)
            asset_height = int(asset.get("height") or 0)
            if asset_width and width and asset_width != width:
                continue
            if asset_height and height and asset_height != height:
                continue
            matches.setdefault(
                str(asset["id"]),
                {
                    "post_id": post_value,
                    "url": POST_URL_TEMPLATE.format(post_id=post_value),
                    "civitai_image_id": int(item.get("id") or 0),
                },
            )
    return matches


def file_sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_model_version_by_hash(token: str, file_hash: str) -> dict[str, Any] | None:
    """Look up a Civitai model version by the file's SHA-256. None if unknown to Civitai."""
    if not file_hash:
        return None
    try:
        return _request(
            f"{BASE_URL}/api/v1/model-versions/by-hash/{file_hash}",
            token=token,
            timeout=30,
        )
    except CivitaiError:
        return None


_MODEL_FILE_SUFFIXES = (".safetensors", ".sft", ".pt", ".ckpt")
_MODEL_FOLDER_NAMES = {"checkpoints", "diffusion_models", "unet", "loras"}


def _comfy_models_root(comfy_root: str) -> Path | None:
    root = Path(comfy_root).expanduser()
    candidates = (root / "ComfyUI" / "models", root / "models")
    return next((candidate.resolve() for candidate in candidates if candidate.is_dir()), None)


def _resolve_model_file(models_root: Path, reference: str, folders: tuple[str, ...]) -> Path | None:
    """Resolve ComfyUI's relative model paths without allowing paths outside models/."""
    normalized = reference.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        return None
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    if parts[0].casefold() == "models":
        parts = parts[1:]
    explicit_folder = parts[0].casefold() if parts and parts[0].casefold() in _MODEL_FOLDER_NAMES else ""
    relative = parts[1:] if explicit_folder else parts
    if not relative:
        return None
    for folder in folders:
        if explicit_folder and explicit_folder != folder.casefold():
            continue
        candidate = models_root / folder / Path(*relative)
        try:
            resolved = candidate.resolve()
            resolved.relative_to(models_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _resource_specs(node: dict[str, Any]) -> list[tuple[str, str, float | None, tuple[str, ...]]]:
    """Return Civitai-relevant checkpoint/LoRA references from a ComfyUI loader node."""
    class_type = str(node.get("class_type") or "").casefold()
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    specs: list[tuple[str, str, float | None, tuple[str, ...]]] = []

    def usable_reference(value: Any) -> bool:
        return isinstance(value, str) and value.strip().casefold().endswith(_MODEL_FILE_SUFFIXES)

    def numeric_weight(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return float(value)
        return None

    shared_weight = None
    for key in ("strength_model", "strength", "strength_clip"):
        candidate_weight = numeric_weight(inputs.get(key))
        if candidate_weight is not None:
            shared_weight = candidate_weight
            break

    def append_lora(reference: Any, weight: Any = None) -> None:
        if usable_reference(reference):
            specs.append(
                (
                    "lora",
                    reference.strip(),
                    numeric_weight(weight) if weight is not None else shared_weight,
                    ("loras",),
                )
            )

    lora_keys = {"lora", "lora_name", "lora_file", "lora_path"}
    for key in ("lora_name", "lora_file", "lora_path"):
        append_lora(inputs.get(key), shared_weight)

    def collect_nested_loras(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("on") is False:
                return
            nested_reference = next(
                (
                    value.get(key)
                    for key in ("lora", "lora_name", "lora_file", "lora_path")
                    if usable_reference(value.get(key))
                ),
                None,
            )
            if nested_reference:
                nested_weight = next(
                    (
                        numeric_weight(value.get(key))
                        for key in ("strength", "strength_model", "strength_clip")
                        if numeric_weight(value.get(key)) is not None
                    ),
                    shared_weight,
                )
                append_lora(nested_reference, nested_weight)
                return
            for child in value.values():
                collect_nested_loras(child)
        elif isinstance(value, list):
            for child in value:
                collect_nested_loras(child)

    for key, value in inputs.items():
        if str(key).casefold() in lora_keys and usable_reference(value):
            append_lora(value, shared_weight)
        elif isinstance(value, (dict, list)):
            collect_nested_loras(value)

    if specs:
        return specs

    checkpoint_keys = ("ckpt_name", "checkpoint_name", "model_name")
    unet_keys = ("unet_name", "diffusion_model", "model_name")
    if "checkpoint" in class_type or "ckpt" in class_type or any(key in inputs for key in checkpoint_keys[:2]):
        folders = ("checkpoints",)
        names = checkpoint_keys
    elif "unet" in class_type or "diffusion" in class_type or any(key in inputs for key in unet_keys[:2]):
        folders = ("diffusion_models", "unet")
        names = unet_keys
    else:
        return []

    for key in names:
        reference = inputs.get(key)
        if usable_reference(reference):
            specs.append(("checkpoint", reference.strip(), None, folders))
            break
    return specs


def build_civitai_resources(
    token: str,
    graph: dict[str, Any] | None,
    comfy_root: str,
    cache_path: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve checkpoint/LoRA loader files in a ComfyUI graph to Civitai modelVersionIds.

    Returns entries shaped like Civitai's civitaiResources: [{type, modelVersionId, weight?}].
    Hash lookups are cached in cache_path so each file is hashed and resolved once.
    """
    if not graph:
        return []
    cache: dict[str, Any] = {}
    if cache_path:
        try:
            cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, json.JSONDecodeError):
            cache = {}
    cache_dirty = False
    resources: list[dict[str, Any]] = []
    seen_versions: set[int] = set()
    models_root = _comfy_models_root(comfy_root)
    if models_root is None:
        return []

    def lookup(file_hash: str) -> dict[str, Any] | None:
        nonlocal cache_dirty
        if file_hash in cache:
            value = cache[file_hash]
            if isinstance(value, dict):
                cached_id = value.get("id") or value.get("modelVersionId")
            else:
                cached_id = value
            try:
                cached_id = int(cached_id)
            except (TypeError, ValueError):
                return None
            return {"id": cached_id} if cached_id > 0 else None
        lookup_failed = False
        try:
            found = _request(
                f"{BASE_URL}/api/v1/model-versions/by-hash/{file_hash}",
                token=token,
                timeout=30,
            )
        except CivitaiError:
            found = None
            lookup_failed = True
        version_id = found.get("id") if isinstance(found, dict) else None
        if version_id:
            cache[file_hash] = int(version_id)
            cache_dirty = True
        elif not lookup_failed:
            cache[file_hash] = ""
            cache_dirty = True
        return found if isinstance(found, dict) else None

    for node in graph.values():
        if not isinstance(node, dict):
            continue
        for resource_type, reference, weight, folders in _resource_specs(node):
            model_file = _resolve_model_file(models_root, reference, folders)
            if model_file is None:
                continue
            try:
                file_hash = sha256_file(str(model_file))
            except OSError:
                continue
            found = lookup(file_hash)
            version_id = found.get("id") if isinstance(found, dict) else None
            if not version_id or int(version_id) in seen_versions:
                continue
            seen_versions.add(int(version_id))
            entry: dict[str, Any] = {
                "type": resource_type,
                "modelVersionId": int(version_id),
            }
            if resource_type == "lora" and weight is not None:
                entry["weight"] = weight
            resources.append(entry)
    if cache_path and cache_dirty:
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cache_path).write_text(json.dumps(cache, indent=1), encoding="utf-8")
        except OSError:
            pass
    return resources
