from __future__ import annotations

import io
import json
import zipfile
import warnings
from pathlib import Path

import pytest

from app.services.github_service import validate_release_zip

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _minimal_zip(*, duplicate: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Dockerfile", "FROM python:3.13-slim\n")
        archive.writestr("requirements.txt", "fastapi\n")
        archive.writestr("app/main.py", "value = 1\n")
        archive.writestr("release.json", json.dumps({"version": "9.9.9", "run_migrations": False}))
        if duplicate:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("app/main.py", "value = 2\n")
    return buf.getvalue()


def test_release_validator_rejects_invalid_and_duplicate_archives():
    with pytest.raises(ValueError, match="ZIP معتبر نیست"):
        validate_release_zip(b"not-a-zip")
    with pytest.raises(ValueError, match="مسیر تکراری"):
        validate_release_zip(_minimal_zip(duplicate=True))


def test_release_validator_accepts_a_clean_root_package():
    package = validate_release_zip(_minimal_zip())
    assert package.version == "9.9.9"
    assert package.validation["python_files_compiled"] == 1


def test_sqlite_backup_is_queued_only_after_root_commit():
    source = read("app/db/session.py")
    assert "if session.in_nested_transaction():" in source
    assert "storage.mark_dirty()" in source
    assert "after_rollback" in source


def test_backup_ref_and_post_migration_snapshot_contract():
    storage = read("app/services/storage_service.py")
    main = read("app/main.py")
    assert "/git/ref/heads/" in storage
    assert "/git/refs/heads/" in storage
    assert "storage.mark_dirty()" in main


def test_expiration_cannot_overwrite_a_concurrent_payment():
    service = read("app/services/invoice_service.py")
    block = service.split("async def release_invoice_reservation", 1)[1].split("async def confirm_invoice_paid", 1)[0]
    assert "with_for_update()" in block
    assert "populate_existing=True" in block
    assert "locked_invoice.status" in block


def test_manual_invoice_confirmation_is_idempotent_and_text_safe():
    handlers = read("app/bot/handlers.py")
    assert "manual_order_id" in handlers
    assert "ManualInvoiceState.processing" in handlers
    assert "Invoice.order_id == manual_order_id" in handlers
    assert 'holder = " ".join((message.text or "").split())' in handlers
    assert 'source_text = (message.text or "").strip()' in handlers


def test_telegram_delivery_uses_resilient_polling_and_acknowledges_poison_updates():
    main = read("app/main.py")
    config = read("app/core/config.py")
    assert 'telegram_mode: str = Field(default="polling"' in config
    assert 'return asyncio.create_task(telegram_polling_worker()' in main
    assert "await bot.delete_webhook" in main
    assert 'code": "UPDATE_HANDLER_FAILED"' in main


def test_diagnostics_do_not_export_webhook_or_runtime_secrets():
    diagnostics = read("app/services/diagnostics_service.py")
    startup = read("app/services/startup_service.py")
    assert "[REDACTED]" in diagnostics
    assert "_masked_webhook_url" in diagnostics
    assert "_redact_sensitive" in diagnostics
    assert "_safe_error_text" in startup


def test_api_usage_metadata_and_idempotency_are_persisted_safely():
    deps = read("app/api/deps.py")
    idem = read("app/services/idempotency_service.py")
    routes = read("app/api/routes.py")
    assert "timedelta(minutes=5)" in deps
    assert "await session.commit()" in deps
    assert "PENDING_REQUEST_STALE_AFTER" in idem
    assert "session.begin_nested()" in idem
    assert "await session.delete(idem_row)" in routes


def test_payment_page_has_stable_expiry_and_no_reload_loop():
    routes = read("app/api/routes.py")
    template = read("app/templates/payment.html")
    assert "expires_at_iso" in routes
    assert "new Date('{{ expires_at_iso }}')" in template
    assert "window.location.replace" in template
    assert "setTimeout(()=>location.reload()" not in template
    assert "no-store, no-cache" in routes
