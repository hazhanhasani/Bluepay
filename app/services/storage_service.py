from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

API_VERSION = "2026-03-10"
BACKUP_PATH = ".gateway/gateway.db.enc"
META_PATH = ".gateway/meta.json"


class GitHubDatabaseStorage:
    """Encrypted SQLite persistence using a dedicated GitHub branch.

    This removes the need for DATABASE_URL, PostgreSQL or a manually attached
    Railway Volume. A consistent SQLite snapshot is encrypted and force-published
    to the `gateway-data` branch after every state-changing database commit.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.disabled = os.getenv("GATEWAY_DISABLE_REMOTE_BACKUP", "0") == "1"
        self.dirty = False
        self.last_error: str | None = None
        self.last_backup_at: str | None = None
        self.last_restore_at: str | None = None

    @property
    def repository(self) -> str:
        return settings.github_repository

    @property
    def base(self) -> str:
        return f"https://api.github.com/repos/{self.repository}"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "GatewayBot-ZeroConfigStorage",
        }

    @property
    def cipher(self) -> Fernet:
        # Deterministic key: the same BOT_TOKEN and repository can restore data
        # after a fresh Railway deployment. The key itself is never committed.
        seed = f"gateway-db-v2|{settings.bot_token}|{self.repository}".encode("utf-8")
        key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
        return Fernet(key)

    async def _request(self, method: str, url: str, *, allow_404: bool = False, **kwargs):
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.request(method, url, headers=self.headers, **kwargs)
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub storage API {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else {}

    async def _read_backup_blob(self) -> bytes | None:
        result = await self._request(
            "GET",
            f"{self.base}/contents/{BACKUP_PATH}?ref={settings.data_branch}",
            allow_404=True,
        )
        if not result:
            return None
        encoded = result.get("content", "").replace("\n", "")
        if not encoded and result.get("download_url"):
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.get(result["download_url"], headers=self.headers)
            response.raise_for_status()
            return response.content
        return base64.b64decode(encoded)

    @staticmethod
    def _sqlite_is_valid(path: Path) -> bool:
        if not path.exists() or path.stat().st_size < 100:
            return False
        try:
            with sqlite3.connect(path) as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
            return bool(row and row[0] == "ok")
        except sqlite3.DatabaseError:
            return False

    async def restore_if_available(self) -> bool:
        """Restore the encrypted snapshot before SQLAlchemy opens the database."""
        if self.disabled:
            return False
        path = settings.database_path
        if self._sqlite_is_valid(path):
            return False

        async with self._lock:
            encrypted = await self._read_backup_blob()
            if encrypted is None:
                return False
            try:
                plain = self.cipher.decrypt(encrypted)
            except InvalidToken as exc:
                raise RuntimeError(
                    "The database backup exists but cannot be decrypted. "
                    "Use the same BOT_TOKEN that created the backup."
                ) from exc

            tmp = path.with_suffix(".restore.tmp")
            tmp.write_bytes(plain)
            if not self._sqlite_is_valid(tmp):
                tmp.unlink(missing_ok=True)
                raise RuntimeError("The restored GitHub database snapshot is not a valid SQLite database.")
            os.replace(tmp, path)
            self.last_restore_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
            return True

    @staticmethod
    def _consistent_snapshot(source: Path) -> bytes:
        if not source.exists():
            raise RuntimeError("Database file does not exist yet")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            with sqlite3.connect(source) as src, sqlite3.connect(temp_path) as dst:
                src.backup(dst)
                check = dst.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError("SQLite snapshot quick_check failed")
            return temp_path.read_bytes()
        finally:
            temp_path.unlink(missing_ok=True)

    async def _create_blob(self, content: bytes) -> str:
        payload = {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
        result = await self._request("POST", f"{self.base}/git/blobs", json=payload)
        return result["sha"]

    async def _publish_root_snapshot(self, encrypted: bytes, plain_sha256: str) -> str:
        backup_blob, meta_blob = await asyncio.gather(
            self._create_blob(encrypted),
            self._create_blob(
                json.dumps(
                    {
                        "format": 2,
                        "encrypted": True,
                        "database": "sqlite",
                        "sha256": plain_sha256,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        )
        tree = await self._request(
            "POST",
            f"{self.base}/git/trees",
            json={
                "tree": [
                    {"path": BACKUP_PATH, "mode": "100644", "type": "blob", "sha": backup_blob},
                    {"path": META_PATH, "mode": "100644", "type": "blob", "sha": meta_blob},
                ]
            },
        )
        commit = await self._request(
            "POST",
            f"{self.base}/git/commits",
            json={
                "message": "gateway: encrypted database snapshot",
                "tree": tree["sha"],
                "parents": [],
            },
        )
        ref_url = f"{self.base}/git/refs/heads/{settings.data_branch}"
        existing = await self._request("GET", ref_url, allow_404=True)
        if existing:
            await self._request("PATCH", ref_url, json={"sha": commit["sha"], "force": True})
        else:
            await self._request(
                "POST",
                f"{self.base}/git/refs",
                json={"ref": f"refs/heads/{settings.data_branch}", "sha": commit["sha"]},
            )
        return commit["sha"]

    async def backup_now(self) -> bool:
        """Synchronously persist a consistent, encrypted snapshot to GitHub."""
        if self.disabled:
            self.dirty = False
            return True
        async with self._lock:
            self.dirty = True
            try:
                plain = await asyncio.to_thread(self._consistent_snapshot, settings.database_path)
                if len(plain) > 50 * 1024 * 1024:
                    raise RuntimeError("Database snapshot exceeds the 50 MB zero-config storage limit")
                encrypted = self.cipher.encrypt(plain)
                await self._publish_root_snapshot(encrypted, hashlib.sha256(plain).hexdigest())
                self.dirty = False
                self.last_error = None
                self.last_backup_at = datetime.now(timezone.utc).isoformat()
                return True
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                # Do not roll back a completed local SQL transaction. The retry
                # worker will continue attempting to persist the dirty snapshot.
                print(f"database_backup_error={self.last_error}")
                return False

    async def retry_worker(self) -> None:
        while True:
            await asyncio.sleep(10)
            if self.dirty:
                await self.backup_now()

    def status(self) -> dict[str, object]:
        return {
            "dirty": self.dirty,
            "last_backup_at": self.last_backup_at,
            "last_restore_at": self.last_restore_at,
            "last_error": self.last_error,
            "branch": settings.data_branch,
            "disabled": self.disabled,
        }


storage = GitHubDatabaseStorage()
