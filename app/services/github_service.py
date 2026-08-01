from __future__ import annotations

import base64
import hashlib
import io
import json
import posixpath
import zipfile
from dataclasses import dataclass, field

import httpx

BLOCKED_NAMES = {".env", ".env.production", "secrets.json", "id_rsa", "id_ed25519", "gateway.db"}
BLOCKED_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".pem", ".key"}
REQUIRED_PATHS = {"Dockerfile", "requirements.txt", "app/main.py", "release.json"}
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_BYTES = 80 * 1024 * 1024
MAX_FILES = 900


@dataclass(slots=True)
class ReleasePackage:
    version: str
    description: str
    files: dict[str, bytes]
    sha256: str
    validation: dict = field(default_factory=dict)


@dataclass(slots=True)
class PublishResult:
    commit_sha: str
    previous_commit_sha: str
    branch: str
    changed_files: int


def validate_release_zip(data: bytes) -> ReleasePackage:
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("حجم ZIP بیشتر از ۲۵ مگابایت است")

    package_hash = hashlib.sha256(data).hexdigest()
    files: dict[str, bytes] = {}
    total = 0
    python_files = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"فایل خراب داخل ZIP: {bad}")
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise ValueError("تعداد فایل‌های بسته بیش از حد مجاز است")
        for info in infos:
            path = info.filename.replace("\\", "/").lstrip("/")
            path = posixpath.normpath(path)
            if path.startswith("../") or path == ".." or "/../" in f"/{path}":
                raise ValueError("مسیر ناامن داخل ZIP شناسایی شد")
            if path.startswith("__MACOSX/") or path.endswith("/.DS_Store"):
                continue
            basename = posixpath.basename(path)
            if basename in BLOCKED_NAMES or any(basename.lower().endswith(suffix) for suffix in BLOCKED_SUFFIXES):
                raise ValueError(f"فایل حساس یا دیتابیس {path} نباید داخل بسته باشد")
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("Symbolic link داخل ZIP مجاز نیست")
            content = archive.read(info)
            total += len(content)
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("حجم استخراج‌شده بسته بیش از حد مجاز است")
            if path.endswith(".py"):
                try:
                    compile(content, path, "exec")
                except SyntaxError as exc:
                    raise ValueError(f"خطای Syntax در {path}:{exc.lineno}: {exc.msg}") from exc
                python_files += 1
            files[path] = content

    missing = REQUIRED_PATHS - set(files)
    if missing:
        raise ValueError("فایل‌های ضروری موجود نیستند: " + ", ".join(sorted(missing)))

    try:
        release = json.loads(files["release.json"].decode("utf-8"))
    except Exception as exc:
        raise ValueError("release.json معتبر نیست") from exc
    version = str(release.get("version", "")).strip()
    if not version:
        raise ValueError("نسخه در release.json مشخص نشده است")
    if not version[0].isdigit():
        raise ValueError("شماره نسخه باید با عدد شروع شود")
    if release.get("run_migrations") and not any(path.startswith("alembic/versions/") for path in files):
        raise ValueError("نسخه نیازمند Migration است اما Alembic revision داخل بسته نیست")

    validation = {
        "zip_bytes": len(data),
        "extracted_bytes": total,
        "file_count": len(files),
        "python_files_compiled": python_files,
        "has_migrations": any(path.startswith("alembic/versions/") for path in files),
        "workflow_files": sum(1 for path in files if path.startswith(".github/workflows/")),
    }
    return ReleasePackage(
        version=version,
        description=str(release.get("description", "")),
        files=files,
        sha256=package_hash,
        validation=validation,
    )


class GitHubPublisher:
    def __init__(self, token: str, repository: str, branch: str = "main"):
        if not token:
            raise ValueError("GITHUB_TOKEN تنظیم نشده است")
        if "/" not in repository:
            raise ValueError("نام مخزن باید به‌شکل owner/repository باشد")
        self.token = token
        self.repository = repository
        self.branch = branch
        self.base = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "BluePay-Updater",
        }

    async def _request(self, method: str, url: str, **kwargs):
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(method, url, headers=self.headers, **kwargs)
        if response.status_code >= 400:
            detail = response.text[:600]
            accepted = response.headers.get("X-Accepted-GitHub-Permissions", "").strip()
            granted = response.headers.get("X-OAuth-Scopes", "").strip()
            extra = []
            if accepted:
                extra.append(f"مجوز لازم: {accepted}")
            if granted:
                extra.append(f"مجوز فعلی: {granted}")
            if response.status_code == 403 and "Resource not accessible by personal access token" in detail:
                extra.append(
                    "توکن باید Repository permission «Contents: Read and write» داشته باشد. "
                    "برای فایل‌های .github/workflows مجوز «Workflows: Read and write» هم لازم است."
                )
            suffix = ("\n" + "\n".join(extra)) if extra else ""
            raise RuntimeError(f"GitHub API {response.status_code}: {detail}{suffix}")
        return response.json() if response.content else {}

    async def preflight(self, *, needs_workflows: bool = False) -> dict:
        repo = await self._request("GET", self.base)
        ref = await self._request("GET", f"{self.base}/git/ref/heads/{self.branch}")
        permissions = repo.get("permissions") or {}
        if permissions and not (permissions.get("push") or permissions.get("admin")):
            raise RuntimeError("توکن روی مخزن دسترسی Push ندارد")
        return {
            "repository": self.repository,
            "branch": self.branch,
            "head": ref["object"]["sha"],
            "private": bool(repo.get("private")),
            "needs_workflows": needs_workflows,
            "permissions": permissions,
        }

    async def ensure_branch(self, branch: str, *, from_sha: str | None = None) -> str:
        try:
            ref = await self._request("GET", f"{self.base}/git/ref/heads/{branch}")
            return ref["object"]["sha"]
        except RuntimeError as exc:
            if "GitHub API 404" not in str(exc):
                raise
        if not from_sha:
            main_ref = await self._request("GET", f"{self.base}/git/ref/heads/{self.branch}")
            from_sha = main_ref["object"]["sha"]
        await self._request("POST", f"{self.base}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": from_sha})
        return from_sha

    async def publish(self, package: ReleasePackage, *, branch: str | None = None) -> PublishResult:
        target_branch = branch or self.branch
        await self.ensure_branch(target_branch)
        ref = await self._request("GET", f"{self.base}/git/ref/heads/{target_branch}")
        parent_sha = ref["object"]["sha"]

        tree_items = []
        for path, content in package.files.items():
            blob = await self._request(
                "POST",
                f"{self.base}/git/blobs",
                json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

        tree = await self._request("POST", f"{self.base}/git/trees", json={"tree": tree_items})
        commit = await self._request(
            "POST",
            f"{self.base}/git/commits",
            json={
                "message": f"Release {package.version}: {package.description}".strip(),
                "tree": tree["sha"],
                "parents": [parent_sha],
            },
        )
        await self._request(
            "PATCH",
            f"{self.base}/git/refs/heads/{target_branch}",
            json={"sha": commit["sha"], "force": False},
        )
        return PublishResult(
            commit_sha=commit["sha"],
            previous_commit_sha=parent_sha,
            branch=target_branch,
            changed_files=len(tree_items),
        )

    async def rollback(self, target_sha: str, *, branch: str | None = None) -> str:
        target_branch = branch or self.branch
        # Validate that the target commit exists before moving the ref.
        commit = await self._request("GET", f"{self.base}/git/commits/{target_sha}")
        await self._request(
            "PATCH",
            f"{self.base}/git/refs/heads/{target_branch}",
            json={"sha": commit["sha"], "force": True},
        )
        return commit["sha"]
