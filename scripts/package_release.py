"""Build a source-only ZIP, never a copy of your working folder."""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
FILES={"QuickStart.bat","Stop.bat","Run Tests.bat","Package Release.bat","requirements.txt","README.md","SECURITY.md","PLAN.md","AUDIT_REPORT.md",".gitignore"}
FOLDERS={"app","scripts","tests","example-guides",".github"}
EXCLUDED={"scripts/rebrand.py","scripts/verification_services.py"}
EXTENSIONS={".py",".js",".html",".css",".md",".yml",".yaml"}
SENSITIVE=[re.compile(rb"sk-[A-Za-z0-9_-]{24,}"),re.compile(rb"(?i)Bearer\s+[A-Za-z0-9_-]{24,}"),re.compile(rb'(?i)api[_-]?token["\x27]?\s*[:=]\s*["\x27][A-Za-z0-9_-]{24,}')]


def release_files():
    result=[]
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel=path.relative_to(ROOT)
        if rel.as_posix() in EXCLUDED:
            continue
        if any(part in {"__pycache__",".venv","backup","data","dist","evidence",".git"} for part in rel.parts):
            continue
        if (len(rel.parts)==1 and rel.name in FILES) or (rel.parts[0] in FOLDERS and path.suffix in EXTENSIONS):
            content=path.read_bytes()
            if any(pattern.search(content) for pattern in SENSITIVE):
                raise ValueError(f"Potential secret in {rel}. Packaging refused; remove the secret before publishing.")
            result.append((path,rel.as_posix()))
    return sorted(result,key=lambda item:item[1])


def main():
    files=release_files()
    if not files:
        raise ValueError("No release source files found.")
    target=ROOT/"dist"/"WildCat-Export-Edition-v1.02-source.zip"
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as archive:
        for path,rel in files:
            archive.write(path,"WildCat-Export-Edition/"+rel)
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"Created {target}\n{len(files)} source files; no config, credentials, images, database, backups, venv, or evidence.\nSHA256: {digest}")


if __name__=="__main__":
    main()
