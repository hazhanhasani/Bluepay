from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = (ROOT / "app/core/config.py").read_text(encoding="utf-8")
SESSION = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
STARTUP = (ROOT / "app/services/startup_service.py").read_text(encoding="utf-8")
MAIN = (ROOT / "app/main.py").read_text(encoding="utf-8")
ENV = (ROOT / ".env.example").read_text(encoding="utf-8")


def test_strict_postgres_variable_is_supported():
    assert 'alias="DB_REQUIRE_POSTGRES"' in CONFIG
    assert "DB_REQUIRE_POSTGRES=true but DATABASE_URL is missing" in CONFIG


def test_database_retry_and_pool_variables_are_supported():
    for name in (
        "DB_CONNECT_RETRIES",
        "DB_CONNECT_RETRY_SECONDS",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT_SECONDS",
        "DB_POOL_RECYCLE_SECONDS",
    ):
        assert name in CONFIG
    assert "settings.db_pool_size" in SESSION
    assert "settings.db_max_overflow" in SESSION


def test_startup_uses_bounded_database_retry():
    assert "async def prepare_database_with_retry" in STARTUP
    assert "await prepare_database_with_retry(engine)" in MAIN


def test_railway_reference_is_exact():
    assert 'DATABASE_URL=${{Postgres.DATABASE_URL}}' in ENV
    assert 'STARTUP_FAIL_OPEN=false' in ENV
    assert 'GATEWAY_DISABLE_REMOTE_BACKUP=1' in ENV
