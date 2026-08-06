from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_postgres_index_migration_does_not_abort_transaction_on_duplicates():
    migration = (ROOT / "alembic/versions/20260801_1200_commerce_automation.py").read_text(encoding="utf-8")
    assert 'inspect(bind).get_indexes("invoices")' in migration
    assert 'if name not in existing_indexes' in migration
    assert 'except Exception:\n            pass' not in migration


def test_railway_readiness_is_core_health_not_external_dependency_health():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'status = 200 if ok and runtime_status.schema_ok and runtime_status.settings_ok else 503' in main
    assert 'async def telegram_delivery_supervisor' in main
    assert 'asyncio.create_task(telegram_delivery_supervisor()' in main
