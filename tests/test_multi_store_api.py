from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.core.security import sha256_text
from app.db.base import Base
from app.models import Invoice, Merchant, Store, StoreApiKey
from app.services.callback_service import paid_payload


def test_multi_store_tables_and_invoice_columns_exist():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert "stores" in inspector.get_table_names()
    assert "store_api_keys" in inspector.get_table_names()
    invoice_columns = {column["name"] for column in inspector.get_columns("invoices")}
    assert {"store_id", "api_key_id", "client_order_id"}.issubset(invoice_columns)


def test_one_merchant_can_have_multiple_stores_with_one_key_each():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        merchant = Merchant(telegram_user_id=1001, name="Merchant", callback_secret="m-secret")
        session.add(merchant)
        session.flush()
        store_a = Store(
            merchant_id=merchant.id,
            code="ST-STOREA",
            name="Store A",
            callback_url="https://a.example/callback",
            callback_secret="a-secret",
        )
        store_b = Store(
            merchant_id=merchant.id,
            code="ST-STOREB",
            name="Store B",
            callback_url="https://b.example/callback",
            callback_secret="b-secret",
        )
        session.add_all([store_a, store_b])
        session.flush()
        session.add_all(
            [
                StoreApiKey(
                    merchant_id=merchant.id,
                    store_id=store_a.id,
                    label="Main API",
                    key_hash=sha256_text("gw_a"),
                    key_prefix="gw_a",
                ),
                StoreApiKey(
                    merchant_id=merchant.id,
                    store_id=store_b.id,
                    label="Main API",
                    key_hash=sha256_text("gw_b"),
                    key_prefix="gw_b",
                ),
            ]
        )
        session.commit()
        assert session.query(Store).filter_by(merchant_id=merchant.id).count() == 2
        assert session.query(StoreApiKey).filter_by(store_id=store_a.id).count() == 1
        assert session.query(StoreApiKey).filter_by(store_id=store_b.id).count() == 1
        assert store_a.callback_url != store_b.callback_url
        assert store_a.callback_secret != store_b.callback_secret


def test_callback_payload_uses_client_order_and_store_identity():
    store = Store(id=8, merchant_id=1, code="ST-A1B2C3D4", name="Main Store", callback_secret="secret")
    invoice = Invoice(
        token="payment-token",
        merchant_id=1,
        card_id=1,
        order_id="API-8-INTERNAL",
        client_order_id="ORDER-1001",
        base_amount_rial=1_000_000,
        fee_amount_rial=20_000,
        customer_fee_rial=0,
        unique_amount_rial=5_000,
        payable_amount_rial=1_005_000,
        fee_mode="merchant",
        purpose="payment",
        status="paid",
        expires_at=datetime.now(timezone.utc),
        paid_at=datetime.now(timezone.utc),
        store_id=8,
        api_key_id=21,
        store=store,
    )
    payload = paid_payload(invoice)
    assert payload["order_id"] == "ORDER-1001"
    assert payload["store_id"] == 8
    assert payload["store_code"] == "ST-A1B2C3D4"
    assert payload["api_key_id"] == 21


def test_client_order_id_is_unique_per_store_but_reusable_in_another_store():
    from sqlalchemy.exc import IntegrityError

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        merchant = Merchant(telegram_user_id=2002, name="Merchant", callback_secret="secret")
        session.add(merchant)
        session.flush()
        stores = [
            Store(merchant_id=merchant.id, code="ST-ONE", name="One", callback_secret="one"),
            Store(merchant_id=merchant.id, code="ST-TWO", name="Two", callback_secret="two"),
        ]
        session.add_all(stores)
        session.flush()

        def make_invoice(store_id: int, internal_order: str) -> Invoice:
            return Invoice(
                token=internal_order,
                merchant_id=merchant.id,
                card_id=1,
                order_id=internal_order,
                client_order_id="ORDER-1001",
                base_amount_rial=100_000,
                fee_amount_rial=0,
                customer_fee_rial=0,
                unique_amount_rial=10,
                payable_amount_rial=100_010,
                fee_mode="merchant",
                purpose="payment",
                status="pending",
                expires_at=now,
                store_id=store_id,
            )

        session.add_all([make_invoice(stores[0].id, "INTERNAL-A"), make_invoice(stores[1].id, "INTERNAL-B")])
        session.commit()
        session.add(make_invoice(stores[0].id, "INTERNAL-C"))
        try:
            session.commit()
            assert False, "same client order in one store must be rejected"
        except IntegrityError:
            session.rollback()
