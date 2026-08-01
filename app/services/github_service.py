from __future__ import annotations

import base64
import io
import json
import posixpath
import zipfile
from dataclasses import dataclass

import httpx

BLOCKED_NAMES = {".env", ".env.production", "secrets.json", "id_rsa", "id_ed25519"}
REQUIRED_PATHS = {"Dockerfile", "requirements.txt", "app/main.py", "release.json"}
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_BYTES = 80 * 1024 * 1024
MAX_FILES = 600


@dataclass(slots=True)
class ReleasePackage:
    version: str
    description: str
    files: dict[str, bytes]


def validate_release_zip(data: bytes) -> ReleasePackage:
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("حجم ZIP بیشتر از ۲۵ مگابایت است")

    files: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
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
            if posixpath.basename(path) in BLOCKED_NAMES:
                raise ValueError(f"فایل حساس {path} نباید داخل بسته باشد")
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("Symbolic link داخل ZIP مجاز نیست")
            content = archive.read(info)
            total += len(content)
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("حجم استخراج‌شده بسته بیش از حد مجاز است")
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
    return ReleasePackage(version=version, description=str(release.get("description", "")), files=files)


class GitHubPublisher:
    def __init__(self, token: str, repository: str, branch: str = "main"):
        if "/" not in repository:
            raise ValueError("نام مخزن باید به‌شکل owner/repository باشد")
        self.token = token
        self.repository = repository
        self.branch = branch
        self.base = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "GatewayBot-Updater",
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
                    "توکن باید به مخزن انتخاب‌شده دسترسی داشته باشد و Repository permission «Contents: Read and write» فعال باشد. "
                    "برای انتشار فایل‌های .github/workflows، مجوز «Workflows: Read and write» نیز لازم است."
                )
            suffix = ("\n" + "\n".join(extra)) if extra else ""
            raise RuntimeError(f"GitHub API {response.status_code}: {detail}{suffix}")
        return response.json() if response.content else {}

    async def publish(self, package: ReleasePackage) -> str:
        ref = await self._request("GET", f"{self.base}/git/ref/heads/{self.branch}")
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
            f"{self.base}/git/refs/heads/{self.branch}",
            json={"sha": commit["sha"], "force": False},
        )
        return commit["sha"]
