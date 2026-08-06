from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_database_url_and_automatic_table_bootstrap_contract():
    config = read("app/core/config.py")
    main = read("app/main.py")
    service = read("app/services/postgres_bootstrap_service.py")
    requirements = read("requirements.txt")
    assert 'alias="DATABASE_URL"' in config
    assert "postgresql+asyncpg://" in config
    assert "ensure_database_tables(engine)" in main
    assert "import_legacy_sqlite_if_empty" in main
    assert "Base.metadata.create_all" in service
    assert "postgresql_already_contains_business_data" in service
    assert "asyncpg" in requirements


def test_postgres_mode_disables_sqlite_backup_worker():
    main = read("app/main.py")
    storage = read("app/services/storage_service.py")
    assert "if not settings.is_postgres" in main
    assert "settings.is_postgres" in storage
    assert "restore_legacy_snapshot_for_postgres" in storage
