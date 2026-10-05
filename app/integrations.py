from __future__ import annotations

import base64
import json
import mimetypes
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .config import Config


class IntegrationError(RuntimeError):
    pass


MAX_LM_PARALLEL_REQUESTS = 4


def _is_context_error(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "context length",
        "context window",
        "context size",
        "maximum context",
        "exceeds the context",
        "exceeded the context",
        "exceeds context",
        "prompt is too long",
        "too many tokens",
        "n_ctx",
        "kv cache",
    )
    return any(marker in lowered for marker in markers)


def _json_request(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json"}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        from .providers import NoRedirect
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise IntegrationError("AI service response exceeded 32 MB.")
            if not raw:
                return {}
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type or raw[:1] in (b"{", b"["):
                return json.loads(raw.decode("utf-8"))
            return raw
    except urllib.error.HTTPError as error:
        raw = error.read(16384).decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
            message = detail.get("error") or detail.get("message") or detail
        except json.JSONDecodeError:
            message = raw or error.reason
        context = " The request exceeds the model context window; use fewer references or increase the context length." if _is_context_error(str(message)) else " Check the service logs and workflow settings."
        raise IntegrationError(f"Local AI request failed (HTTP {error.code}).{context}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise IntegrationError(f"Could not reach {url}: {error}") from error


def _download(url: str, timeout: float = 60) -> tuple[bytes, str]:
    try:
        from .providers import NoRedirect
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=timeout) as response:
            raw = response.read(100 * 1024 * 1024 + 1)
            if len(raw) > 100 * 1024 * 1024:
                raise IntegrationError("ComfyUI output exceeds 100 MB.")
            return raw, response.headers.get("Content-Type", "application/octet-stream")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise IntegrationError(f"Could not download ComfyUI output: {error}") from error


class Services:
    def __init__(self, config: Config):
        self.config = config
        self.processes: list[subprocess.Popen[Any]] = []
        self._startup_lock = threading.RLock()

    def _lm_headers(self) -> dict[str, str]:
        token = str(self.config.get("lm_api_token", "")).strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def _base(value: str) -> str:
        return str(value).rstrip("/")

    def lm_url(self) -> str:
        return self._base(self.config.get("lm_url"))

    def comfy_url(self) -> str:
        return self._base(self.config.get("comfy_url"))

    def sibling_relay_active(self) -> dict[str, Any]:
        url = self._base(str(self.config.get("sibling_relay_url", "")))
        if not url:
            return {}
        try:
            payload = _json_request(f"{url}/api/health", timeout=3)
        except IntegrationError:
            return {}
        return payload.get("active", {}) if isinstance(payload, dict) else {}

    def lm_models(self) -> list[dict[str, Any]]:
        payload = _json_request(
            f"{self.lm_url()}/api/v1/models", headers=self._lm_headers(), timeout=10
        )
        models = payload.get("models", []) if isinstance(payload, dict) else []
        result = []
        for model in models:
            if model.get("type") == "embedding":
                continue
            capabilities = model.get("capabilities") if isinstance(model.get("capabilities"), dict) else {}
            result.append(
                {
                    "id": model.get("key") or model.get("model") or model.get("id"),
                    "name": model.get("display_name") or model.get("key") or model.get("id"),
                    "type": model.get("type", "llm"),
                    "size_bytes": model.get("size_bytes"),
                    "quantization": (model.get("quantization") or {}).get("name") if isinstance(model.get("quantization"), dict) else model.get("quantization"),
                    "vision": bool(capabilities.get("vision")),
                    "loaded_instances": model.get("loaded_instances") or [],
                }
            )
        return sorted(result, key=lambda item: str(item["name"]).lower())

    def loaded_lm_instances(self) -> list[dict[str, Any]]:
        instances: list[dict[str, Any]] = []
        for model in self.lm_models():
            for instance in model.get("loaded_instances", []):
                if isinstance(instance, dict):
                    item = dict(instance)
                else:
                    item = {"instance_id": str(instance)}
                item.setdefault("model_id", model.get("id"))
                instances.append(item)
        return instances

    def load_lm(self, model_id: str) -> str:
        if not model_id:
            raise IntegrationError("Choose an LM Studio model first.")
        for instance in self.loaded_lm_instances():
            if instance.get("model_id") == model_id or instance.get("instance_id") == model_id:
                return str(instance.get("instance_id") or model_id)
        payload = {
            "model": model_id,
            "context_length": max(1024, int(self.config.get("lm_context_length", 16384))),
            "flash_attention": True,
            "echo_load_config": True,
        }
        response = _json_request(
            f"{self.lm_url()}/api/v1/models/load",
            method="POST",
            payload=payload,
            headers=self._lm_headers(),
            timeout=1800,
        )
        return str(response.get("instance_id") or model_id)

    def unload_all_lm(self) -> list[str]:
        unloaded: list[str] = []
        for instance in self.loaded_lm_instances():
            instance_id = instance.get("instance_id") or instance.get("id")
            if not instance_id:
                continue
            _json_request(
                f"{self.lm_url()}/api/v1/models/unload",
                method="POST",
                payload={"instance_id": instance_id},
                headers=self._lm_headers(),
                timeout=180,
            )
            unloaded.append(str(instance_id))
        deadline = time.time() + 30
        while time.time() < deadline:
            if not self.loaded_lm_instances():
                break
            time.sleep(1)
        return unloaded

    def chat(
        self,
        model_id: str,
        system: str,
        user_text: str,
        image_path: Path | None = None,
        temperature: float = 0.3,
    ) -> str:
        content: str | list[dict[str, Any]] = user_text
        if image_path and image_path.exists():
            mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ]
        response = self._completion_request(
            f"{self.lm_url()}/v1/chat/completions",
            method="POST",
            payload={
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                "temperature": temperature,
                "stream": False,
            },
            headers=self._lm_headers(),
            timeout=3600,
        )
        try:
            return str(response["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as error:
            raise IntegrationError("LM Studio returned no answer text.") from error

    def compare_guide_images(
        self,
        model_id: str,
        batch_goal: str,
        focus: str,
        labeled_images: list[tuple[str, int, Path]],
    ) -> str:
        if len(labeled_images) < 2:
            raise IntegrationError("At least two guide images are required for comparison.")
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Compare the attached AI-generated images as evidence of markdown prompt-guide performance. "
                    "Each image is labeled with its source guide and sample number. Judge the guides, not just a "
                    "single lucky image. Consider consistency, prompt adherence, composition, lighting, subject "
                    "clarity, visual quality, style control, and artifacts.\n\n"
                    f"Original batch goal:\n{batch_goal}\n\n"
                    f"Additional comparison focus:\n{focus or 'Choose the guide with the strongest overall outputs.'}\n\n"
                    "Return exactly these sections:\n"
                    "WINNER: [guide name]\n"
                    "RANKING: best to worst, with a concise reason for every guide\n"
                    "EVIDENCE: specific strengths or problems seen across each guide's samples\n"
                    "RECOMMENDATION: which markdown guide to keep using and what to improve"
                ),
            }
        ]
        for guide_name, sample_number, image_path in labeled_images:
            if not image_path.is_file():
                continue
            mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.extend(
                [
                    {"type": "text", "text": f"Guide: {guide_name} — sample {sample_number}"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ]
            )
        response = self._completion_request(
            f"{self.lm_url()}/v1/chat/completions",
            method="POST",
            payload={
                "model": model_id,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a strict visual evaluator comparing image-generation prompt guides.",
                    },
                    {"role": "user", "content": content},
                ],
                "temperature": 0.2,
                "stream": False,
            },
            headers=self._lm_headers(),
            timeout=7200,
        )
        try:
            return str(response["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as error:
            raise IntegrationError("LM Studio returned no comparison text.") from error

    @staticmethod
    def _guide_instruction(payload: dict[str, Any], guide: dict[str, Any]) -> str:
        count = max(1, min(20, int(payload.get("count") or 1)))
        word_limit = max(0, int(payload.get("wordLimit") or 0))
        negative_enabled = bool(payload.get("negativeEnabled"))
        has_images = bool(payload.get("images"))
        image_mode = "i2i" if payload.get("imageMode") == "i2i" else "t2i"
        parts = [
            "You are WildCat Harness's built-in image-prompt engine.",
            "Follow the supplied Markdown guide as instructions and inspiration, but output only final usable image prompts.",
            "Do not explain your process and do not use Markdown fences.",
            f'Output exactly {count} prompt{("" if count == 1 else "s")}, numbered "1." through "{count}.", each starting on its own line.',
        ]
        if word_limit:
            parts.append(f"Each positive prompt must contain at least {word_limit} words. This is a strict minimum.")
        if negative_enabled:
            parts.append("After every positive prompt, add a separate line beginning exactly with 'Negative Prompt:'.")
        else:
            parts.append("Do not include negative prompts.")
        if has_images:
            if image_mode == "i2i":
                parts.append(
                    "Use the single attached reference image as the primary visual specification for a close reconstruction."
                )
            else:
                parts.append(
                    "Use the single attached reference image as inspiration for a fresh text-to-image prompt. "
                    "Describe the clean subject, setting, composition, lighting, camera, colors, mood, and visual details."
                )
            if payload.get("removeUi", True):
                parts.append(
                    "Ignore subtitles, handles, app chrome, watermarks, logos, playback controls, borders, and other interface overlays."
                )
        parts.extend(
            [
                "",
                f"Markdown guide: {guide.get('name') or 'Untitled guide'}",
                str(guide.get("content") or ""),
                "",
                "User image goal:",
                str(payload.get("prompt") or "Create a polished image-generation prompt."),
                "",
                f"Return exactly {count} numbered prompt{'' if count == 1 else 's'} and nothing else.",
            ]
        )
        return "\n".join(parts)

    def generate_guided_prompts(self, payload: dict[str, Any]) -> dict[str, Any]:
        model_id = str(payload.get("model") or "").strip()
        if not model_id:
            raise IntegrationError("WildCat's guide engine needs the loaded LM Studio model ID.")
        guides = payload.get("guides") if isinstance(payload.get("guides"), list) else []
        images = payload.get("images") if isinstance(payload.get("images"), list) else []
        def generate_one(index: int, guide: dict[str, Any]) -> tuple[int, dict[str, str]]:
            guide_name = str(guide.get("name") or "Untitled guide")
            instruction = self._guide_instruction(payload, guide)
            content: str | list[dict[str, Any]] = instruction
            if images:
                content = [{"type": "text", "text": instruction}]
                content.extend(
                    {
                        "type": "image_url",
                        "image_url": {"url": str(image.get("dataUrl") or "")},
                    }
                    for image in images
                    if image.get("dataUrl")
                )
            try:
                response = self._completion_request(
                    f"{self.lm_url()}/v1/chat/completions",
                    method="POST",
                    payload={
                        "model": model_id,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You write precise image-generation prompts. Output only the requested numbered prompts.",
                            },
                            {"role": "user", "content": content},
                        ],
                        "temperature": max(0.0, min(2.0, float(payload.get("temperature") or 0.7))),
                        "stream": False,
                    },
                    headers=self._lm_headers(),
                    timeout=7200,
                )
                output = str(response["choices"][0]["message"]["content"]).strip()
                if not output:
                    raise IntegrationError("LM Studio returned no prompt text.")
                return index, {"guide": guide_name, "output": output}
            except Exception as error:  # One guide failure should not discard successful guide outputs.
                return index, {"guide": guide_name, "error": str(error)}

        if not guides:
            return {"model": model_id, "results": []}
        ordered_results: list[dict[str, str] | None] = [None] * len(guides)
        pending = list(enumerate(guides))
        worker_count = min(MAX_LM_PARALLEL_REQUESTS, len(pending))
        while pending:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="wildcat-lm") as executor:
                futures = [executor.submit(generate_one, index, guide) for index, guide in pending]
                for future in as_completed(futures):
                    index, result = future.result()
                    ordered_results[index] = result
            if worker_count <= 1:
                break
            context_failures = [
                (index, guide)
                for index, guide in pending
                if ordered_results[index] and _is_context_error(str(ordered_results[index].get("error") or ""))
            ]
            if not context_failures:
                break
            pending = context_failures
            worker_count = max(1, min(worker_count // 2, len(pending)))
        return {"model": model_id, "results": [result for result in ordered_results if result is not None]}

    def _completion_request(self, url: str, **kwargs: Any) -> Any:
        return _json_request(url, **kwargs)

    def comfy_stats(self) -> dict[str, Any]:
        return _json_request(f"{self.comfy_url()}/system_stats", timeout=5)

    def comfy_queue(self) -> dict[str, Any]:
        return _json_request(f"{self.comfy_url()}/queue", timeout=5)

    def free_comfy(self) -> None:
        _json_request(
            f"{self.comfy_url()}/free",
            method="POST",
            payload={"unload_models": True, "free_memory": True},
            timeout=60,
        )
        time.sleep(1)

    def submit_comfy(self, workflow: dict[str, Any], client_id: str) -> str:
        response = _json_request(
            f"{self.comfy_url()}/prompt",
            method="POST",
            payload={"prompt": workflow, "client_id": client_id},
            timeout=60,
        )
        prompt_id = response.get("prompt_id") if isinstance(response, dict) else None
        if not prompt_id:
            detail = response.get("error") if isinstance(response, dict) else response
            raise IntegrationError(f"ComfyUI rejected the workflow: {detail}")
        return str(prompt_id)

    def upload_comfy_input(self, path: Path) -> str:
        boundary = f"----WildCatHarness{uuid.uuid4().hex}"
        filename = path.name.replace('"', "_")
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("type", "input")
        field("subfolder", "wildcat-harness")
        field("overwrite", "true")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            f"{self.comfy_url()}/upload/image",
            data=b"".join(parts),
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            from .providers import NoRedirect
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise IntegrationError(f"ComfyUI image upload failed (HTTP {error.code}). Check its server logs.") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise IntegrationError(f"Could not upload the image to ComfyUI: {error}") from error
        name = str(payload.get("name") or "").strip()
        subfolder = str(payload.get("subfolder") or "").strip().strip("/\\")
        if not name:
            raise IntegrationError("ComfyUI did not return the uploaded image name.")
        return f"{subfolder}/{name}" if subfolder else name

    def comfy_history(self, prompt_id: str) -> dict[str, Any]:
        return _json_request(f"{self.comfy_url()}/history/{urllib.parse.quote(prompt_id)}", timeout=15)

    def interrupt_comfy(self) -> None:
        _json_request(f"{self.comfy_url()}/interrupt", method="POST", payload={}, timeout=15)

    @staticmethod
    def _verified_comfy_process(info: dict[str, Any], comfy_root: Path) -> bool:
        executable = str(info.get("ExecutablePath") or "").strip()
        command_line = str(info.get("CommandLine") or "").lower().replace("/", "\\")
        if not executable or "main.py" not in command_line or "comfyui" not in command_line:
            return False
        try:
            Path(executable).resolve().relative_to(comfy_root.resolve())
        except (OSError, ValueError):
            return False
        return True

    def exit_comfy(self) -> list[int]:
        parsed = urllib.parse.urlparse(self.comfy_url())
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise IntegrationError("ComfyUI process exit is allowed only for a local API address.")
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
        comfy_root = Path(str(self.config.get("comfy_path", ""))).resolve()
        if not comfy_root.is_dir():
            raise IntegrationError("The configured ComfyUI folder does not exist.")
        script = (
            f"$items = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty OwningProcess -Unique; "
            "$result = foreach ($id in $items) { "
            "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = $id\" -ErrorAction SilentlyContinue; "
            "if ($p) { [pscustomobject]@{ ProcessId=$p.ProcessId; ExecutablePath=$p.ExecutablePath; CommandLine=$p.CommandLine } } }; "
            "$result | ConvertTo-Json -Compress"
        )
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        lookup = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
            check=False,
        )
        if lookup.returncode != 0:
            raise IntegrationError(f"Could not identify the ComfyUI process: {lookup.stderr.strip()}")
        raw = lookup.stdout.strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise IntegrationError("Windows returned invalid ComfyUI process information.") from error
        candidates = payload if isinstance(payload, list) else [payload]
        verified = [
            int(item["ProcessId"])
            for item in candidates
            if isinstance(item, dict) and self._verified_comfy_process(item, comfy_root)
        ]
        if not verified:
            raise IntegrationError(
                "A process is listening on the ComfyUI port, but it does not belong to the configured ComfyUI folder."
            )
        stopped: list[int] = []
        for process_id in verified:
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=flags,
                check=False,
            )
            if result.returncode != 0:
                raise IntegrationError(f"Could not exit ComfyUI process {process_id}: {result.stderr.strip()}")
            stopped.append(process_id)
        return stopped

    def download_comfy_output(self, output: dict[str, Any]) -> tuple[bytes, str]:
        query = urllib.parse.urlencode(
            {
                "filename": output.get("filename", ""),
                "subfolder": output.get("subfolder", ""),
                "type": output.get("type", "output"),
            }
        )
        return _download(f"{self.comfy_url()}/view?{query}", timeout=300)

    def _spawn(self, command: list[str], cwd: Path | None = None) -> None:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        self.processes.append(process)

    @staticmethod
    def _wait(check, timeout: int, label: str) -> None:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                check()
                return
            except Exception as error:  # noqa: BLE001 - preserve final connection error
                last_error = error
                time.sleep(2)
        raise IntegrationError(f"{label} did not become ready: {last_error}")

    def ensure_lm(self) -> None:
        with self._startup_lock:
            try:
                self.lm_models()
                return
            except IntegrationError:
                if not self.config.get("auto_start_apps", True):
                    raise
            executable = Path(str(self.config.get("lm_studio_exe", "")))
            if not executable.is_file():
                raise IntegrationError(f"LM Studio is offline and its executable was not found: {executable}")
            subprocess.Popen([str(executable)], cwd=str(executable.parent))
            self._wait(self.lm_models, 120, "LM Studio")

    def ensure_comfy(self) -> None:
        with self._startup_lock:
            try:
                self.comfy_stats()
                return
            except IntegrationError:
                if not self.config.get("auto_start_apps", True):
                    raise
            folder = Path(str(self.config.get("comfy_path", "")))
            launcher = folder / "run_nvidia_gpu.bat"
            if not launcher.is_file():
                raise IntegrationError(f"ComfyUI is offline and its launcher was not found: {launcher}")
            self._spawn(["cmd.exe", "/c", str(launcher)], folder)
            self._wait(self.comfy_stats, 180, "ComfyUI")

    def gpu_memory(self) -> dict[str, int] | None:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total,memory.used,memory.free",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
            )
            total, used, free = [int(value.strip()) for value in result.stdout.splitlines()[0].split(",")]
            return {"total_mb": total, "used_mb": used, "free_mb": free}
        except (OSError, ValueError, subprocess.SubprocessError, IndexError):
            return None

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = {"gpu": self.gpu_memory()}
        try:
            models = self.lm_models()
            loaded = [
                {"id": model["id"], "name": model["name"], "instances": model["loaded_instances"]}
                for model in models
                if model["loaded_instances"]
            ]
            status["lm"] = {"online": True, "loaded": loaded}
        except Exception as error:  # Status polling must survive one malformed or transient service response.
            status["lm"] = {"online": False, "error": str(error)}
        try:
            stats = self.comfy_stats()
            queue = self.comfy_queue()
            devices = stats.get("devices", []) if isinstance(stats, dict) else []
            status["comfy"] = {
                "online": True,
                "queue_running": len(queue.get("queue_running", [])),
                "queue_pending": len(queue.get("queue_pending", [])),
                "device": devices[0] if devices else {},
            }
        except Exception as error:  # Keep the other service indicators and active-job state available.
            status["comfy"] = {"online": False, "error": str(error)}
        return status
