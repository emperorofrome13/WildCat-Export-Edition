"""Verify the source archive and run its one-click launcher from a clean extraction."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get(url: str) -> bytes:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=3) as response:
        return response.read()


def main() -> None:
    archive_path = ROOT / "dist" / "WildCat-Export-Edition-v1.02-source.zip"
    evidence = ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="release-smoke-", dir=evidence))
    forbidden = {"config.json", "config.tmp", "data", "backup", ".venv", "evidence", ".git"}
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert names and all(not forbidden.intersection(Path(name).parts) for name in names)
        assert all(Path(name).parts[0] == "WildCat-Export-Edition" and ".." not in Path(name).parts for name in names)
        assert "WildCat-Export-Edition/QuickStart.bat" in names
        archive.extractall(target)
    extracted = target / "WildCat-Export-Edition"
    assert not (extracted / "config.json").exists()
    assert not (extracted / "data").exists()
    url = "http://127.0.0.1:5195"
    try:
        get(url + "/api/health")
    except (OSError, urllib.error.URLError):
        available = True
    else:
        available = False
    if not available:
        raise RuntimeError("Release verification port 5195 is already used; no process was stopped.")
    log_path = evidence / "clean-release-launch.log"
    with log_path.open("w", encoding="utf-8") as log:
        command = ["cmd.exe", "/c", "QuickStart.bat", "--port", "5195", "--no-browser"] if sys.platform == "win32" else [sys.executable, "scripts/launcher.py", "--port", "5195", "--no-browser"]
        process = subprocess.Popen(command, cwd=extracted, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Clean launcher exited early. See clean-release-launch.log.")
                try:
                    health = json.loads(get(url + "/api/health"))
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.5)
            else:
                raise RuntimeError("Clean launcher did not become ready. See clean-release-launch.log.")
            # HTTP can respond before the launcher completes its own readiness check.
            # Do not shut down that server until the launcher confirms the full start.
            ready_message = f"READY: WildCat Export Edition v{health['version']} at {url}"
            ready_deadline = time.monotonic() + 30
            while ready_message not in log_path.read_text(encoding="utf-8", errors="replace"):
                if process.poll() is not None or time.monotonic() >= ready_deadline:
                    raise RuntimeError("Server responded, but the clean launcher did not confirm READY. See clean-release-launch.log.")
                time.sleep(0.1)
            assert health["app"] == "WildCat Export Edition" and health["version"] == "1.02"
            assert not health["active"]["running"]
            html = get(url + "/").decode()
            assert "v1.02" in html and 'id="exportSetup"' in html
            bootstrap = json.loads(get(url + "/api/bootstrap"))
            cfg = bootstrap["config"]
            assert cfg["setup_complete"] is False and cfg["auto_start_apps"] is False
            assert cfg["lm_api_token"] == cfg["civitai_api_token"] == ""
            assert bootstrap["workflows"] == []
            assert len(bootstrap["guides"]) == 6
            assert json.loads(get(url + "/api/batches"))["batches"] == []
            report = {"version": "1.02", "source_files": len(names), "private_files_in_archive": False, "clean_launch": True, "empty_library": True, "starter_guides": 6, "fresh_setup": True, "command": "QuickStart.bat --port 5195 --no-browser", "extraction": str(extracted)}
            (evidence / "release-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2), flush=True)
        finally:
            try:
                token = re.search(r'name="wildcat-token" content="([^"]+)"', get(url + "/").decode())
                if token:
                    request = urllib.request.Request(url + "/api/app/stop", data=b"{}", headers={"Content-Type": "application/json", "X-Wildcat-Token": token.group(1)})
                    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5).close()
                process.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                # The launcher is our child; this never terminates an unrelated server.
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
