"""Local-only secured entry point for the shareable edition."""
from __future__ import annotations

import argparse
import base64
import hmac
import io
import json
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path

from PIL import Image

from . import server as legacy
from .config import DEFAULTS, ROOT, VERSION
from .core import validate_workflow_json
from .providers import ExportServices, cli_command, request_local

TOKEN = secrets.token_urlsafe(32)
PUBLIC_SETTINGS = {
    "provider", "setup_complete", "lm_url", "lm_api_token", "lm_studio_exe",
    "ollama_url", "llamacpp_url", "llamacpp_managed", "llamacpp_exe", "llamacpp_model",
    "llamacpp_mmproj", "llamacpp_gpu_layers", "claude_exe", "codex_exe", "cli_model",
    "cli_cloud_consent", "comfy_url", "comfy_path", "prompt_model", "qa_model",
    "comparison_model", "lm_context_length", "comfy_job_timeout_minutes", "auto_start_apps",
}
SOURCE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".m4v": "video/mp4",
}
legacy.services = ExportServices(legacy.config)
legacy.jobs.services = legacy.services
if not legacy.config.get("setup_complete") and not legacy.guides.list(False):
    for starter in (ROOT / "example-guides").glob("*.md"):
        legacy.guides.save(starter.name, starter.read_text(encoding="utf-8"))


def redact(value):
    if isinstance(value, dict):
        return {k: "" if k in {"lm_api_token", "civitai_api_token", "api_token"} else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for key in ("lm_api_token", "civitai_api_token"):
            secret = str(legacy.config.get(key) or "")
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


def workflow_content(body: dict) -> str:
    if body.get("image_data_url"):
        url = str(body["image_data_url"])
        if not url.startswith("data:image/png;base64,"):
            raise ValueError("Workflow image import needs an original ComfyUI PNG.")
        raw = base64.b64decode(url.split(",", 1)[1], validate=True)
        if len(raw) > 30 * 1024 * 1024:
            raise ValueError("Workflow PNG exceeds 30 MB.")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG":
                raise ValueError("Choose a PNG with its original ComfyUI metadata.")
            content = image.info.get("prompt")
        if not content:
            raise ValueError("This PNG has no executable ComfyUI prompt metadata. Export Workflow (API) JSON from ComfyUI instead.")
    else:
        content = str(body.get("content") or "")
    if len(content.encode("utf-8")) > 5 * 1024 * 1024:
        raise ValueError("Workflow JSON exceeds 5 MB.")
    validate_workflow_json(content)
    return content


def valid_request(headers, port: int, mutate: bool) -> bool:
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if str(headers.get("Host") or "").lower() not in allowed:
        return False
    origin = headers.get("Origin")
    if origin and origin not in {f"http://{host}" for host in allowed}:
        return False
    if headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    if mutate and not hmac.compare_digest(str(headers.get("X-Wildcat-Token") or ""), TOKEN):
        return False
    return True


class Handler(legacy.Handler):
    server_version = "WildCatExport/1.01"

    def handle_one_request(self):
        self.connection.settimeout(30)
        return super().handle_one_request()

    def _guard(self, mutate=False):
        if not valid_request(self.headers, self.server.server_port, mutate):
            self._error(403, "This request is not from the local WildCat page. Open the app using QuickStart, or refresh its page.")
            return False
        return True

    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _json(self, status, payload):
        super()._json(status, redact(payload))

    def _static(self, relative):
        if relative in {"", "index.html"}:
            content = (legacy.STATIC_DIR / "index.html").read_text(encoding="utf-8")
            content = content.replace("<!--SESSION_TOKEN-->", f'<meta name="wildcat-token" content="{TOKEN}">')
            raw = content.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        super()._static(relative)

    def _body(self):
        if "application/json" not in str(self.headers.get("Content-Type") or "").lower():
            raise ValueError("This action requires a JSON request. Refresh the app and try again.")
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or self.headers.get("Transfer-Encoding"):
            raise ValueError("Invalid upload length.")
        body = super()._body()
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/settings":
            body = {k: v for k, v in body.items() if k in PUBLIC_SETTINGS}
            if legacy.jobs.active().get("batch_id"):
                raise ValueError("Finish or cancel the active job before changing connections.")
            if body.get("provider") and body["provider"] != legacy.config.get("provider"):
                # Do not abandon an owned model on a provider switch.
                legacy.services.unload_all_lm()
        if path in {"/api/workflow", "/api/image-tool-workflows"}:
            body["content"] = workflow_content(body)
        if path == "/api/references/processed":
            reference_id = str(body.get("reference_id") or "")
            if not legacy.references.get(reference_id):
                raise ValueError("That reference is no longer in the upload tray. Add it again.")
        return body

    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/health":
            self._json(200, {"ok": True, "app": "WildCat Export Edition", "version": VERSION, "active": legacy.jobs.active()})
            return
        if path == "/api/models":
            try:
                self._json(200, {"models": legacy.services.lm_models()})
            except Exception as error:
                self._error(503, str(error))
            return
        super().do_GET()

    def _reference_source_upload(self, parsed):
        name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0]
        mime = SOURCE_TYPES.get(Path(name).suffix.lower())
        if not mime:
            raise ValueError("Choose a raster photo (PNG/JPEG/WebP/GIF/AVIF/BMP) or video (MP4/WebM/MOV). SVG, HTML and scripts are not accepted.")
        # Never preserve an HTML/script MIME supplied by an untrusted upload client.
        if self.headers.get("Content-Type"):
            self.headers.replace_header("Content-Type", mime)
        else:
            self.headers["Content-Type"] = mime
        return super()._reference_source_upload(parsed)

    def do_POST(self):
        if not self._guard(True):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "Invalid upload size.")
            return
        if length < 0 or self.headers.get("Transfer-Encoding"):
            self._error(400, "Invalid upload framing.")
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/recycle-bin/delete-file":
            self._error(403, "For safety, this edition never deletes files outside its own image library. You can remove the external original yourself.")
            return
        if path in {"/api/actions/stop-comfy", "/api/actions/exit-comfy"}:
            # A manual GPU handoff must not immediately start another queued job.
            legacy.jobs.pause_queue()
        if path.startswith("/api/setup/") or path == "/api/app/stop":
            try:
                body = self._body()
                if path == "/api/app/stop":
                    if legacy.jobs.active().get("batch_id"):
                        raise ValueError("Cancel or finish the active job before closing WildCat.")
                    self._json(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif path == "/api/setup/test":
                    kind = body.get("service", "prompt")
                    if kind == "comfy":
                        stats = legacy.services.comfy_stats()
                        self._json(200, {"ok": True, "message": "ComfyUI is connected.", "devices": stats.get("devices", [])})
                    else:
                        # Discovery never loads a managed model or spends cloud tokens.
                        if legacy.config.get("provider") == "llamacpp" and legacy.config.get("llamacpp_managed"):
                            for key in ("llamacpp_exe", "llamacpp_model"):
                                if not Path(str(legacy.config.get(key) or "")).is_file():
                                    raise ValueError("Choose llama-server and your GGUF first.")
                            models = [{"id": "local-gguf", "name": Path(legacy.config.get("llamacpp_model")).stem, "vision": bool(legacy.config.get("llamacpp_mmproj")), "loaded_instances": []}]
                        else:
                            models = legacy.services.lm_models()
                        message = "CLI found. Login and model access are checked on the first prompt; this check does not spend usage." if legacy.config.get("provider") in {"claude", "codex"} else f"Connected. {len(models)} model(s) available."
                        self._json(200, {"ok": True, "models": models, "message": message})
                elif path == "/api/setup/browse":
                    kind = str(body.get("kind") or "")
                    if kind not in {"folder", "exe", "gguf"}:
                        raise ValueError("Choose a folder, executable or GGUF file.")
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    try:
                        selected = filedialog.askdirectory(title="Choose your ComfyUI folder") if kind == "folder" else filedialog.askopenfilename(title="Choose a local file", filetypes=[("GGUF model", "*.gguf"), ("All files", "*.*")] if kind == "gguf" else [("Application", "*.exe"), ("All files", "*.*")])
                    finally:
                        root.destroy()
                    self._json(200, {"path": selected})
                elif path == "/api/setup/workflow-check":
                    entry = legacy.jobs.workflow_entry(str(body.get("workflow_id") or ""))
                    if not entry:
                        raise ValueError("Import a workflow first.")
                    graph = json.loads(Path(entry["file"]).read_text(encoding="utf-8"))
                    nodes = request_local(legacy.config.get("comfy_url"), "/object_info")
                    missing = sorted({str(node.get("class_type")) for node in graph.values() if isinstance(node, dict) and node.get("class_type") not in nodes})
                    self._json(200, {"missing_nodes": missing, "message": "Missing custom nodes: " + ", ".join(missing) if missing else "All workflow node types are installed. Verify checkpoint/LoRA names in ComfyUI too."})
                else:
                    self._error(404, "Setup action not found.")
            except Exception as error:
                self._error(400, str(error))
            return
        super().do_POST()

    def do_DELETE(self):
        if self._guard(True):
            super().do_DELETE()


def main():
    parser = argparse.ArgumentParser(description=f"WildCat Export Edition v{VERSION}")
    parser.add_argument("--port", type=int, default=5194)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = legacy.AppServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"WildCat Export Edition v{VERSION}: {url}", flush=True)
    print("Local-only control panel. No AI services start until a job needs them.", flush=True)
    # Never auto-restart recovered jobs after a crash without an explicit Resume.
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Stopping WildCat Export Edition...", flush=True)
    finally:
        legacy.services.close()
        server.server_close()


if __name__ == "__main__":
    main()
