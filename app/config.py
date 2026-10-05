from __future__ import annotations

import ipaddress
import json
import socket
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
VERSION = "1.02"
PROVIDERS = {"lmstudio", "ollama", "llamacpp", "claude", "codex"}
DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1", "port": 5194, "app_name": "WildCat Export Edition",
    "sibling_relay_url": "", "provider": "lmstudio", "setup_complete": False,
    "lm_url": "http://127.0.0.1:1234", "lm_api_token": "", "lm_studio_exe": "",
    "ollama_url": "http://127.0.0.1:11434", "llamacpp_url": "http://127.0.0.1:8080",
    "llamacpp_managed": False, "llamacpp_exe": "", "llamacpp_model": "",
    "llamacpp_mmproj": "", "llamacpp_gpu_layers": 99,
    "claude_exe": "", "codex_exe": "", "cli_model": "", "cli_cloud_consent": False,
    "comfy_url": "http://127.0.0.1:8188", "comfy_path": "",
    "workflow_file": "", "workflow_name": "", "workflow_positive_fields": [],
    "workflow_negative_fields": [], "workflows": [], "active_workflow_id": "",
    "image_tool_workflows": [], "prompt_model": "", "qa_model": "", "comparison_model": "",
    "civitai_api_token": "", "civitai_username": "", "civitai_last_sync": "",
    "civitai_last_model_version": "", "lm_context_length": 16384,
    "comfy_job_timeout_minutes": 180, "auto_start_apps": False,
}


def validate_local_url(value: Any) -> str:
    url = str(value).strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Use a local API address such as http://127.0.0.1:8188.")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError("API addresses must not include passwords, query strings, or a path. Leave off /v1.")
    if parts.hostname.lower() != "localhost":
        try:
            literal = ipaddress.ip_address(parts.hostname)
        except ValueError as error:
            raise ValueError("Use localhost or a loopback IP address, not a domain name.") from error
        if not literal.is_loopback:
            raise ValueError("This edition only connects to services on this computer (localhost or 127.0.0.1).")
    try:
        addresses = socket.getaddrinfo(parts.hostname, parts.port or 80, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_loopback for item in addresses):
            raise ValueError("This edition only connects to services on this computer (localhost or 127.0.0.1).")
    except socket.gaierror as error:
        raise ValueError("That local API address could not be resolved.") from error
    return url


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._initial_defaults())

    def _initial_defaults(self) -> dict[str, Any]:
        return dict(DEFAULTS)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        merged = dict(DEFAULTS)
        if isinstance(data, dict):
            merged.update({k: v for k, v in data.items() if k in DEFAULTS})
        return merged

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def all(self, include_token: bool = True) -> dict[str, Any]:
        with self._lock:
            data = self._read()
        if not include_token:
            for prefix in ("lm", "civitai"):
                data[f"{prefix}_has_api_token"] = bool(data.get(f"{prefix}_api_token"))
                data[f"{prefix}_api_token"] = ""
        return data

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self._read()
            for key, value in changes.items():
                if key not in DEFAULTS:
                    continue
                if key.endswith("api_token") and value == "__KEEP__":
                    continue
                if key.endswith("_url") and value:
                    value = validate_local_url(value)
                if key == "provider" and value not in PROVIDERS:
                    raise ValueError("Choose one of the five supported prompt backends.")
                if key == "host" and value != "127.0.0.1":
                    raise ValueError("The control panel must stay bound to 127.0.0.1.")
                if key in {"port", "lm_context_length", "comfy_job_timeout_minutes", "llamacpp_gpu_layers"}:
                    value = int(value)
                    limits = {"port": (1024, 65535), "lm_context_length": (1024, 1048576), "comfy_job_timeout_minutes": (1, 10080), "llamacpp_gpu_layers": (0, 999)}
                    lo, hi = limits[key]
                    if not lo <= value <= hi:
                        raise ValueError(f"{key.replace('_', ' ').capitalize()} must be between {lo} and {hi}.")
                if not isinstance(value, type(DEFAULTS[key])):
                    raise ValueError(f"Invalid value for {key.replace('_', ' ')}.")
                current[key] = value
            self._write(current)
            return dict(current)

    def get(self, key: str, default: Any = None) -> Any:
        return self.all().get(key, default)
