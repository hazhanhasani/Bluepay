from __future__ import annotations

import compileall
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
required = [
    "Dockerfile", "requirements.txt", "release.json", "app/main.py",
    "app/models/entities.py", "app/templates/payment.html", "app/static/style.css",
]
missing = [item for item in required if not (ROOT / item).exists()]
if missing:
    raise SystemExit(f"Missing release files: {', '.join(missing)}")
if not compileall.compile_dir(ROOT / "app", quiet=1):
    raise SystemExit("Python compilation failed")
release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
if not release.get("version"):
    raise SystemExit("release.json has no version")
print(f"Release {release['version']} validated")
