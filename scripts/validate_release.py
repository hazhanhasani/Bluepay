from __future__ import annotations

import json
import sys
from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "Dockerfile",
    "requirements.txt",
    "release.json",
    "railway.json",
    "alembic.ini",
    "app/main.py",
    "app/version.py",
    "app/models/entities.py",
    "app/services/startup_service.py",
    "app/templates/payment.html",
    "app/static/style.css",
]
BLOCKED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pyc", ".pem", ".key"}


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> int:
    missing = [item for item in REQUIRED if not (ROOT / item).exists()]
    if missing:
        fail("Missing release files: " + ", ".join(missing))

    python_roots = [ROOT / "app", ROOT / "scripts", ROOT / "alembic", ROOT / "sdks" / "python"]
    for python_root in python_roots:
        for source in sorted(python_root.rglob("*.py")):
            try:
                compile(source.read_bytes(), str(source.relative_to(ROOT)), "exec")
            except SyntaxError as exc:
                fail(f"Python compilation failed: {source.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")

    env = Environment()
    for template in sorted((ROOT / "app" / "templates").glob("*.html")):
        try:
            env.parse(template.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"Jinja template parse failed: {template.relative_to(ROOT)}: {exc}")

    release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    version = str(release.get("version") or "").strip()
    if not version:
        fail("release.json has no version")
    version_source = (ROOT / "app" / "version.py").read_text(encoding="utf-8")
    if f'"{version}"' not in version_source and f"'{version}'" not in version_source:
        fail("app/version.py and release.json versions do not match")
    if release.get("run_migrations") and not list((ROOT / "alembic" / "versions").glob("*.py")):
        fail("release requests migrations but no Alembic revision exists")

    railway = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    health_path = ((railway.get("deploy") or {}).get("healthcheckPath"))
    if health_path != "/ready":
        fail("Railway healthcheckPath must be /ready")

    blocked: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if "__pycache__" in path.parts or path.suffix.lower() in BLOCKED_SUFFIXES:
            blocked.append(rel)
        if rel.startswith(".github/workflows/"):
            blocked.append(rel + " (workflow publishing requires extra PAT permission)")
    if blocked:
        fail("Blocked release files: " + ", ".join(sorted(blocked)[:30]))

    print(f"Release {version} validated: Python, Jinja, migrations, Railway readiness and package hygiene")
    return 0


if __name__ == "__main__":
    sys.exit(main())
