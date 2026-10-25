#!/usr/bin/env python3
"""Check runtime JSON, imports, relative Markdown links and source hygiene."""
import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_release import RUNTIME_FILES

def check():
    issues = []
    for name in RUNTIME_FILES:
        path = ROOT / name
        if not path.is_file():
            issues.append("Missing runtime file: " + name)
        elif path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for path in ROOT.rglob("*.md"):
        if any(part in ("dist", ".git") for part in path.parts):
            continue
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("http:", "https:", "mailto:", "#")):
                continue
            relative = target.split("#", 1)[0]
            if relative and not (path.parent / relative).exists():
                issues.append(str(path.relative_to(ROOT)) + ": broken link " + relative)
    for name in ("connection.json", ".env"):
        if (ROOT / name).exists():
            print("Runtime-local file present; must remain gitignored:", name)
    import allen_heath_dlive_full_mirroring as app
    assert app.VERSION == "0.6.6"
    assert app.FINAL_OPTIONS["scene_sync"] is False
    assert app.FINAL_OPTIONS["probe"] == "capture_all"
    assert not issues, "\n".join(issues)
    print("PASS:", len(RUNTIME_FILES), "runtime files; JSON, Python AST, imports and documentation links.")

if __name__ == "__main__":
    check()
