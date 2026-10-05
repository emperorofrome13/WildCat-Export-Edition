"""One-click Windows bootstrap. AI services remain under the user's control."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def local_get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:
        return response.read()


def main():
    parser=argparse.ArgumentParser(description="Start or stop WildCat Export Edition")
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--stop",action="store_true")
    parser.add_argument("--port",type=int,default=5194)
    args=parser.parse_args()
    if sys.version_info < (3,10):
        print("Install Python 3.10 or newer, then run QuickStart again.",flush=True)
        return 1
    url=f"http://127.0.0.1:{args.port}"
    try:
        health=json.loads(local_get(url+"/api/health"))
        if health.get("app") != "WildCat Export Edition":
            print(f"Port {args.port} is used by another app. Run QuickStart.bat --port 5195 or stop that app yourself.",flush=True)
            return 1
        online=True
    except (OSError,urllib.error.URLError,ValueError):
        online=False
    if args.stop:
        if not online:
            print("WildCat Export Edition is not running on this port.",flush=True)
            return 0
        html=local_get(url+"/").decode("utf-8")
        match=re.search(r'name="wildcat-token" content="([^"]+)"',html)
        if not match:
            print("Could not authorize a safe shutdown. Use Ctrl+C in the launcher window.")
            return 1
        req=urllib.request.Request(url+"/api/app/stop",data=b"{}",headers={"Content-Type":"application/json","X-Wildcat-Token":match.group(1)})
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5).close()
        except urllib.error.HTTPError as error:
            try:
                print(json.loads(error.read()).get("error"))
            except ValueError:
                print("Could not stop safely. Finish or cancel the active job first.")
            return 1
        print("WildCat Export Edition stopped safely. External ComfyUI/LM Studio servers were not killed.",flush=True)
        return 0
    if online:
        print(f"WildCat Export Edition v{health.get('version')} is already running: {url}",flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    folder=ROOT/".venv"
    python=folder/("Scripts/python.exe" if sys.platform=="win32" else "bin/python")
    if not python.is_file():
        print("First start: creating an isolated Python environment…",flush=True)
        venv.EnvBuilder(with_pip=True).create(folder)
    dependency=subprocess.run([str(python),"-c","from PIL import Image; assert Image.__version__ == '12.3.0'"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if dependency.returncode:
        print("Installing the pinned image library from PyPI. This is only needed on first start or an update…",flush=True)
        install=subprocess.run([str(python),"-m","pip","install","--disable-pip-version-check","-r",str(ROOT/"requirements.txt")])
        if install.returncode:
            print("Dependency installation failed. Check your internet connection and retry QuickStart.",flush=True)
            return 1
    process=subprocess.Popen([str(python),"-m","app.export_server","--no-browser","--port",str(args.port)],cwd=ROOT)
    try:
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            if process.poll() is not None:
                print("The server exited before it was ready. Check the error above.",flush=True)
                return 1
            try:
                health=json.loads(local_get(url+"/api/health"))
                if health.get("app")=="WildCat Export Edition":
                    break
            except (OSError,urllib.error.URLError,ValueError):
                time.sleep(0.25)
        else:
            print("Startup took too long. The browser was not opened.",flush=True)
            process.terminate()
            process.wait(timeout=10)
            return 1
        print(f"READY: WildCat Export Edition v{health['version']} at {url}",flush=True)
        print("Keep this window open. Use Stop.bat after jobs finish, or Ctrl+C to exit.",flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return process.wait()
    except KeyboardInterrupt:
        print("Closing WildCat. Interrupted jobs can be resumed manually on the next start.",flush=True)
        process.terminate()
        process.wait(timeout=15)
        return 0


if __name__=="__main__":
    raise SystemExit(main())
