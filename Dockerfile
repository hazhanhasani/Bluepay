# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir aiogram==3.21.0 httpx==0.28.1

RUN cat <<'PY' > /installer.py
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import posixpath
import zipfile
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

BOT_TOKEN = os.environ["BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
OWNER = os.getenv("RAILWAY_GIT_REPO_OWNER", "").strip()
REPO_NAME = os.getenv("RAILWAY_GIT_REPO_NAME", "").strip()
REPOSITORY = f"{OWNER}/{REPO_NAME}" if OWNER and REPO_NAME else ""
BRANCH = os.getenv("RAILWAY_GIT_BRANCH", "").strip() or "main"
STATE_FILE = Path("/tmp/gateway-installer.json")
MAX_ZIP = 25 * 1024 * 1024
MAX_EXTRACTED = 80 * 1024 * 1024
MAX_FILES = 700
REQUIRED = {"Dockerfile", "requirements.txt", "release.json", "app/main.py"}
BLOCKED = {".env", ".env.production", "secrets.json", "id_rsa", "id_ed25519"}
API_VERSION = "2026-03-10"


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"admin_id": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def _safe_zip_entries(data: bytes):
    if len(data) > MAX_ZIP:
        raise ValueError("حجم ZIP بیشتر از ۲۵ مگابایت است")

    files = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [item for item in zf.infolist() if not item.is_dir()]
        if len(infos) > MAX_FILES:
            raise ValueError("تعداد فایل‌های بسته بیش از حد مجاز است")

        for info in infos:
            path = posixpath.normpath(info.filename.replace("\\", "/").lstrip("/"))
            if path in {"", "."}:
                continue
            if path == ".." or path.startswith("../"):
                raise ValueError("مسیر ناامن داخل ZIP")
            if path.startswith("__MACOSX/") or path.endswith(".DS_Store"):
                continue
            if posixpath.basename(path) in BLOCKED:
                raise ValueError(f"فایل حساس {path} داخل ZIP مجاز نیست")
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("Symbolic link مجاز نیست")

            content = zf.read(info)
            total += len(content)
            if total > MAX_EXTRACTED:
                raise ValueError("حجم استخراج‌شده بیش از حد مجاز است")
            files[path] = content
    return files


def _project_from_entries(entries: dict[str, bytes]):
    # حالت عادی: فایل‌های پروژه مستقیماً در ریشه ZIP هستند.
    if REQUIRED.issubset(entries):
        return entries

    # پشتیبانی از ZIPهایی که کل پروژه داخل یک پوشه مانند gateway-bot/ قرار دارد.
    roots = set()
    for path in entries:
        parts = path.split("/")
        for index in range(1, len(parts)):
            roots.add("/".join(parts[:index]) + "/")

    for root in sorted(roots, key=lambda item: (item.count("/"), len(item))):
        stripped = {
            path[len(root):]: content
            for path, content in entries.items()
            if path.startswith(root) and path[len(root):]
        }
        if REQUIRED.issubset(stripped):
            return stripped

    return None


def validate_zip(data: bytes, depth: int = 0):
    entries = _safe_zip_entries(data)
    project = _project_from_entries(entries)

    # پشتیبانی از بسته تحویل: اگر ZIP اصلی داخل ZIP دیگری باشد، خودکار بازش کن.
    if project is None and depth < 2:
        nested = [
            (path, content)
            for path, content in entries.items()
            if path.lower().endswith(".zip")
        ]
        nested.sort(key=lambda item: ("gateway-bot" not in item[0].lower(), item[0]))
        errors = []
        for path, content in nested:
            try:
                return validate_zip(content, depth + 1)
            except (ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
                errors.append(f"{path}: {exc}")

    if project is None:
        found = ", ".join(sorted(entries)[:12]) or "هیچ فایل قابل استفاده‌ای"
        raise ValueError(
            "فایل‌های ضروری موجود نیستند: "
            + ", ".join(sorted(REQUIRED))
            + "\nفایل‌های ابتدای بسته: "
            + found
            + "\nفایل gateway-bot-v0.2.1.zip یا بسته تحویل جدید را ارسال کن."
        )

    missing = REQUIRED - set(project)
    if missing:
        raise ValueError("فایل‌های ضروری موجود نیستند: " + ", ".join(sorted(missing)))

    release = json.loads(project["release.json"].decode("utf-8"))
    version = str(release.get("version", "")).strip()
    if not version:
        raise ValueError("version در release.json وجود ندارد")
    return version, str(release.get("description", "")), project


class GitHubPublisher:
    def __init__(self):
        if not REPOSITORY:
            raise RuntimeError("Railway repository metadata was not detected")
        self.base = f"https://api.github.com/repos/{REPOSITORY}"
        self.headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "Gateway-Initial-Installer/0.2.1",
        }

    async def request(self, method, url, **kwargs):
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.request(method, url, headers=self.headers, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else {}

    async def publish(self, version, description, files):
        ref = await self.request("GET", f"{self.base}/git/ref/heads/{BRANCH}")
        parent = ref["object"]["sha"]
        tree_items = []
        for path, content in files.items():
            blob = await self.request(
                "POST",
                f"{self.base}/git/blobs",
                json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = await self.request("POST", f"{self.base}/git/trees", json={"tree": tree_items})
        commit = await self.request(
            "POST",
            f"{self.base}/git/commits",
            json={
                "message": f"Install gateway {version}: {description}".strip(),
                "tree": tree["sha"],
                "parents": [parent],
            },
        )
        await self.request(
            "PATCH",
            f"{self.base}/git/refs/heads/{BRANCH}",
            json={"sha": commit["sha"], "force": False},
        )
        return commit["sha"]


router = Router()
state = load_state()


def is_admin(message: Message):
    if not state.get("admin_id"):
        state["admin_id"] = message.from_user.id
        save_state(state)
    return message.from_user.id == state.get("admin_id")


@router.message(CommandStart())
async def start(message: Message):
    claimed = not state.get("admin_id")
    if not is_admin(message):
        return await message.answer("این ربات نصب‌کننده فقط در اختیار مدیر اولیه است.")
    if not REPOSITORY:
        return await message.answer(
            "❌ اطلاعات Repository از Railway دریافت نشد. سرویس را حتماً با Deploy from GitHub بساز."
        )
    text = (
        "🚀 <b>نصب‌کننده خودکار درگاه</b>\n\n"
        f"مخزن: <code>{REPOSITORY}</code>\n"
        f"شاخه: <code>{BRANCH}</code>\n\n"
        "فقط فایل ZIP کامل سیستم را ارسال کن. نام مخزن، شاخه، دیتابیس و کلیدهای داخلی خودکار هستند."
    )
    if claimed:
        text += "\n\n👑 شما به‌عنوان مدیر نصب ثبت شدید."
    await message.answer(text)


@router.message(Command("status"))
async def status(message: Message):
    if not is_admin(message):
        return
    await message.answer(
        f"مخزن: <code>{REPOSITORY or 'تشخیص داده نشد'}</code>\n"
        f"شاخه: <code>{BRANCH}</code>\n"
        "BOT_TOKEN: ✅\nGITHUB_TOKEN: ✅"
    )


@router.message(F.document)
async def upload(message: Message):
    if not is_admin(message):
        return
    if not message.document.file_name.lower().endswith(".zip"):
        return await message.answer("فقط فایل ZIP پروژه کامل را ارسال کن.")
    progress = await message.answer("📦 در حال دریافت و بررسی فایل...")
    buffer = io.BytesIO()
    await message.bot.download(message.document, destination=buffer)
    try:
        version, description, files = validate_zip(buffer.getvalue())
        await progress.edit_text(f"✅ نسخه <code>{version}</code> معتبر است. در حال انتشار خودکار...")
        sha = await GitHubPublisher().publish(version, description, files)
        await progress.edit_text(
            "🚀 فایل‌ها روی GitHub ثبت شدند و Railway نسخه کامل را خودکار Deploy می‌کند.\n\n"
            f"نسخه: <code>{version}</code>\nCommit: <code>{sha[:12]}</code>"
        )
    except Exception as exc:
        await progress.edit_text(f"❌ نصب ناموفق بود:\n<code>{str(exc)}</code>")


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
PY

CMD ["python", "/installer.py"]
