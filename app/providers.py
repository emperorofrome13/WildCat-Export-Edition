"""Portable prompt backends. No shell command interpolation or tool-enabled agents."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Config, validate_local_url
from .integrations import IntegrationError, Services, _is_context_error


class VRAMSafetyError(ValueError):
    """Must not be swallowed as an offline-service error during a GPU handoff."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IntegrationError("Local AI service redirected the request. Check its API address.")


def request_local(base: str, path: str, payload: Any = None, token: str = "", timeout: int = 15) -> Any:
    base = validate_local_url(base)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise IntegrationError("The AI service response exceeded 32 MB.")
        return json.loads(raw)
    except urllib.error.HTTPError as error:
        detail = error.read(16384).decode("utf-8", errors="replace")
        guidance = " The request exceeds the model context window; use fewer references or increase the context length." if _is_context_error(detail) else " Check the model and service logs."
        raise IntegrationError(f"Local AI request failed (HTTP {error.code}).{guidance}") from error
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        # Do not echo provider bodies, tokens or submitted private prompts.
        code = getattr(error, "code", None)
        suffix = f" (HTTP {code})" if code else ""
        raise IntegrationError(f"Could not complete the local AI request{suffix}. Check the service address, model and server logs.") from error


def cli_command(name: str, configured: str = "") -> list[str]:
    found = configured or shutil.which(name) or ""
    path = Path(found)
    if not path.is_file():
        raise IntegrationError(f"{name.capitalize()} CLI was not found. Install it, sign in in a terminal, then choose its executable in Setup.")
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        # npm's Windows shim requires a shell; resolve the actual Node entry point instead.
        package = "@openai/codex/bin/codex.js" if name == "codex" else "@anthropic-ai/claude-code/cli.js"
        entry = path.parent / "node_modules" / package
        node = shutil.which("node")
        if not node or not entry.is_file():
            raise IntegrationError("Use the native CLI executable, or a standard npm installation with Node on PATH. Shell launchers are not accepted.")
        return [node, str(entry)]
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise IntegrationError("Choose the CLI .exe, not a script or arbitrary command.")
    return [str(path)]


class ExportServices(Services):
    def __init__(self, config: Config):
        super().__init__(config)
        self._llama_process: subprocess.Popen | None = None
        self._llama_lock = threading.RLock()
        self._cli_slots = threading.BoundedSemaphore(4)
        self._loaded_by_us = False
        self._loaded_provider = ""

    @property
    def provider(self) -> str:
        return str(self.config.get("provider", "lmstudio"))

    def sibling_relay_active(self) -> dict[str, Any]:
        return {}

    def provider_url(self) -> str:
        return str(self.config.get({"lmstudio": "lm_url", "ollama": "ollama_url", "llamacpp": "llamacpp_url"}.get(self.provider, "lm_url")))

    def lm_url(self) -> str:
        return validate_local_url(self.config.get("lm_url"))

    def comfy_url(self) -> str:
        return validate_local_url(self.config.get("comfy_url"))

    def _request(self, path: str, payload=None, timeout=15):
        return request_local(self.provider_url(), path, payload, str(self.config.get("lm_api_token") or ""), timeout)

    def lm_models(self) -> list[dict[str, Any]]:
        if self.provider == "lmstudio":
            return super().lm_models()
        if self.provider in {"claude", "codex"}:
            cli_command(self.provider, str(self.config.get(f"{self.provider}_exe") or ""))
            model = str(self.config.get("cli_model") or "default")
            return [{"id": model, "name": f"{self.provider.capitalize()} · {model}", "vision": True, "loaded_instances": [], "cloud": True}]
        if self.provider == "ollama":
            available = self._request("/api/tags").get("models", [])
            loaded = {m.get("name") for m in self._request("/api/ps").get("models", [])}
            result = []
            for model in available:
                name = str(model.get("name") or model.get("model"))
                # Actual vision availability is verified by /api/show when a model is selected.
                result.append({"id": name, "name": name, "vision": True, "vision_unverified": True, "loaded_instances": [{"id": name}] if name in loaded else []})
            return result
        if self.config.get("llamacpp_managed") and not self._llama_process:
            model_file = str(self.config.get("llamacpp_model") or "")
            return [{"id": "local-gguf", "name": Path(model_file).stem or "Choose a GGUF in Setup", "vision": bool(self.config.get("llamacpp_mmproj")), "loaded_instances": []}] if model_file else []
        data = self._request("/v1/models").get("data", [])
        return [{"id": str(m["id"]), "name": str(m["id"]), "vision": bool(self.config.get("llamacpp_mmproj") or m.get("capabilities", {}).get("multimodal")), "loaded_instances": [{"id": str(m["id"])}]} for m in data]

    def ensure_lm(self) -> None:
        if self.provider == "lmstudio":
            return super().ensure_lm()
        if self.provider == "llamacpp" and self.config.get("llamacpp_managed"):
            self._start_llama()
        self.lm_models()

    def _start_llama(self) -> None:
        with self._llama_lock:
            if self._llama_process and self._llama_process.poll() is None:
                return
            # Never adopt or terminate an independently running server.
            try:
                self._request("/health", timeout=2)
            except IntegrationError:
                available = False
            else:
                available = True
            if available:
                raise VRAMSafetyError("The llama.cpp port is already used by an external server. Stop that server or choose another port for managed mode.")
            exe = Path(str(self.config.get("llamacpp_exe") or ""))
            model = Path(str(self.config.get("llamacpp_model") or ""))
            if not exe.is_file() or not model.is_file() or model.suffix.lower() != ".gguf":
                raise IntegrationError("Choose llama-server.exe and a local GGUF model in Setup.")
            if os.name == "nt" and exe.suffix.lower() != ".exe":
                raise IntegrationError("Choose a native llama-server executable.")
            port = urlsplit(validate_local_url(self.provider_url())).port or 8080
            args = [str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(port), "-c", str(self.config.get("lm_context_length")), "-ngl", str(self.config.get("llamacpp_gpu_layers")), "-np", "4"]
            mmproj = str(self.config.get("llamacpp_mmproj") or "")
            if mmproj:
                if not Path(mmproj).is_file():
                    raise IntegrationError("The vision projector file was not found.")
                args.extend(["--mmproj", mmproj])
            self._llama_process = subprocess.Popen(args, cwd=str(exe.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                self._wait(lambda: self._request("/health", timeout=3), 240, "llama.cpp")
            except Exception:
                self._stop_llama()
                raise

    def _stop_llama(self) -> None:
        with self._llama_lock:
            process = self._llama_process
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            self._llama_process = None

    def load_lm(self, model_id: str) -> str:
        if self.provider == "lmstudio":
            result = super().load_lm(model_id)
        else:
            self.ensure_lm()
            if not model_id:
                raise IntegrationError("Select a prompt model in Setup.")
            result = self.lm_models()[0]["id"] if self.provider == "llamacpp" and self.config.get("llamacpp_managed") else model_id
        self._loaded_by_us = True
        self._loaded_provider = self.provider
        return result

    def unload_all_lm(self) -> list[str]:
        if self.provider in {"claude", "codex"}:
            return []  # CLI completions are awaited; no local model uses VRAM.
        try:
            if self.provider == "lmstudio":
                result = super().unload_all_lm()
                if self.loaded_lm_instances():
                    raise VRAMSafetyError("LM Studio still reports loaded models. Stop them before running ComfyUI.")
            elif self.provider == "ollama":
                result = [str(m.get("name") or m.get("model")) for m in self._request("/api/ps").get("models", [])]
                for name in result:
                    self._request("/api/generate", {"model": name, "keep_alive": 0, "stream": False}, timeout=180)
                deadline = time.monotonic() + 30
                while self._request("/api/ps").get("models"):
                    if time.monotonic() >= deadline:
                        raise VRAMSafetyError("Ollama still has loaded models. Stop them before running ComfyUI.")
                    time.sleep(0.5)
            elif self.config.get("llamacpp_managed"):
                self._stop_llama()
                result = ["managed llama.cpp"]
            else:
                models = self._request("/models").get("data", [])
                result = []
                for model in models:
                    status = model.get("status", {}).get("value") if isinstance(model.get("status"), dict) else model.get("status")
                    if status not in {"unloaded", "sleeping"}:
                        response = self._request("/models/unload", {"model": model["id"]}, timeout=180)
                        if not response.get("success"):
                            raise VRAMSafetyError("llama.cpp did not confirm model unloading. Use managed mode for a standard single-model server.")
                        result.append(str(model["id"]))
                after = self._request("/models").get("data", [])
                for m in after:
                    status = m.get("status", {}).get("value") if isinstance(m.get("status"), dict) else m.get("status")
                    if status not in {"unloaded", "sleeping"}:
                        raise VRAMSafetyError("llama.cpp is still loaded. Use managed mode or stop it manually before ComfyUI.")
        except IntegrationError as error:
            if self.provider == "llamacpp" or self._loaded_by_us:
                raise VRAMSafetyError("Cannot confirm that the prompt backend released VRAM. Check it manually, then retry; ComfyUI was not started.") from error
            # Backend not used by this app and offline; ordinary pasted-prompt use remains possible.
            return []
        self._loaded_by_us = False
        self._loaded_provider = ""
        return result

    def _completion_request(self, url: str, **kwargs) -> Any:
        payload = kwargs["payload"]
        if self.provider in {"claude", "codex"}:
            output = self._cli_completion(payload, int(kwargs.get("timeout", 3600)))
            return {"choices": [{"message": {"content": output}}]}
        if self.provider == "ollama":
            messages = []
            for message in payload["messages"]:
                content = message["content"]
                text, images = [], []
                for block in content if isinstance(content, list) else [{"type": "text", "text": content}]:
                    if block["type"] == "text":
                        text.append(block["text"])
                    elif block["type"] == "image_url":
                        images.append(block["image_url"]["url"].split(",", 1)[1])
                item = {"role": message["role"], "content": "\n".join(text)}
                if images:
                    caps = self._request("/api/show", {"model": payload["model"]}).get("capabilities", [])
                    if "vision" not in caps:
                        raise IntegrationError("This Ollama model does not support images. Choose a vision model or remove the references.")
                    item["images"] = images
                messages.append(item)
            result = self._request("/api/chat", {"model": payload["model"], "messages": messages, "stream": False, "keep_alive": "10m", "options": {"temperature": payload.get("temperature", 0.7), "num_ctx": self.config.get("lm_context_length")}}, timeout=int(kwargs.get("timeout", 3600)))
            return {"choices": [{"message": {"content": result.get("message", {}).get("content", "")}}]}
        return self._request("/v1/chat/completions", payload, timeout=int(kwargs.get("timeout", 3600)))

    def _cli_completion(self, payload: dict, timeout: int) -> str:
        if not self.config.get("cli_cloud_consent"):
            raise IntegrationError("Enable cloud-prompt consent in Setup before using Claude Code or Codex. Prompts and references will be sent to that service.")
        name = self.provider
        command = cli_command(name, str(self.config.get(f"{name}_exe") or ""))
        with self._cli_slots, tempfile.TemporaryDirectory(prefix="wildcat-prompt-") as temp:
            work = Path(temp)
            help_args = ["exec", "--help"] if name == "codex" else ["--help"]
            help_result = subprocess.run(command + help_args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, cwd=temp, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            needed = ["--ignore-user-config", "--ephemeral"] if name == "codex" else ["--tools", "--setting-sources", "--strict-mcp-config", "--safe-mode"]
            if help_result.returncode or any(flag not in help_result.stdout for flag in needed):
                raise IntegrationError(f"Update {name.capitalize()} CLI: this edition requires tool/config-isolation flags supported by a recent release.")
            text_parts, images, claude_blocks = [], [], []
            for message in payload["messages"]:
                content = message["content"]
                for block in content if isinstance(content, list) else [{"type": "text", "text": content}]:
                    if block["type"] == "text":
                        text = f"{message['role'].upper()}: {block['text']}"
                        text_parts.append(text)
                        claude_blocks.append({"type": "text", "text": text})
                    elif block["type"] == "image_url":
                        header, encoded = block["image_url"]["url"].split(",", 1)
                        mime = header[5:].split(";", 1)[0]
                        if mime not in {"image/png", "image/jpeg", "image/webp"}:
                            raise IntegrationError("CLI image prompts support PNG, JPEG and WebP only.")
                        raw = base64.b64decode(encoded, validate=True)
                        if len(raw) > 15 * 1024 * 1024:
                            raise IntegrationError("A CLI reference image exceeds 15 MB.")
                        image = work / f"reference-{len(images)}.{ {'image/png':'png','image/jpeg':'jpg','image/webp':'webp'}[mime]}"
                        image.write_bytes(raw)
                        images.append(str(image))
                        claude_blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}})
            model = payload.get("model") or self.config.get("cli_model")
            if name == "claude":
                args = command + ["-p", "--safe-mode", "--no-chrome", "--tools", "", "--disallowedTools", "mcp__*", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence", "--output-format", "json", "--input-format", "stream-json"]
                stdin = json.dumps({"type": "user", "message": {"role": "user", "content": claude_blocks}, "parent_tool_use_id": None}) + "\n"
            else:
                args = command + ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "-c", "features.shell_tool=false", "-c", "features.unified_exec=false", "-c", "features.apply_patch_freeform=false", "-c", 'web_search="disabled"', "--output-last-message", str(work / "answer.txt")]
                for image in images:
                    args.extend(["--image", image])
                stdin = "Write image-generation prompts or evaluations only. Never use tools or access files.\n\n" + "\n\n".join(text_parts)
            if model and model != "default":
                args.extend(["--model", str(model)])
            if name == "codex":
                args.append("-")
            try:
                result = subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=temp, timeout=min(timeout, 7200), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired as error:
                raise IntegrationError(f"{name.capitalize()} exceeded the response timeout. Try a smaller prompt request.") from error
            if result.returncode:
                raise IntegrationError(f"{name.capitalize()} CLI failed (exit {result.returncode}). Check your CLI login, model access and usage limit in a terminal. No raw CLI output is saved.")
            if name == "claude":
                try:
                    response = json.loads(result.stdout)
                    if response.get("is_error"):
                        raise IntegrationError("Claude Code reported an error. Check account access and usage limits.")
                    answer = str(response.get("result") or "").strip()
                except json.JSONDecodeError as error:
                    raise IntegrationError("Claude Code returned invalid JSON; update the CLI and try again.") from error
            else:
                output = work / "answer.txt"
                answer = output.read_text(encoding="utf-8").strip() if output.is_file() else ""
            if not answer:
                raise IntegrationError(f"{name.capitalize()} returned no answer text.")
            return answer

    def close(self) -> None:
        self._stop_llama()

    def free_comfy(self) -> None:
        queue = self.comfy_queue()
        if queue.get("queue_running") or queue.get("queue_pending"):
            raise VRAMSafetyError("ComfyUI has unfinished work. Finish or cancel that work before loading the prompt backend.")
        super().free_comfy()
