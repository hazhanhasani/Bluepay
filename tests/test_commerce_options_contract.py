from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.db.base import Base
import app.models  # noqa: F401
from app.services.options_service import conditions_match

ROOT = Path(__file__).resolve().parents[1]
ENTITIES = (ROOT / "app/models/entities.py").read_text(encoding="utf-8")
ROUTES = (ROOT / "app/api/options_routes.py").read_text(encoding="utf-8")
SERVICE = (ROOT / "app/services/options_service.py").read_text(encoding="utf-8")
MAIN = (ROOT / "app/main.py").read_text(encoding="utf-8")
PAYMENT_LINK = (ROOT / "app/templates/payment_link.html").read_text(encoding="utf-8")
OPTIONS = (ROOT / "app/templates/options.html").read_text(encoding="utf-8")
MIGRATION = (ROOT / "alembic/versions/20260801_1200_commerce_automation.py").read_text(encoding="utf-8")


def test_commerce_tables_create_on_clean_database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    expected = {
        "merchant_option_profiles", "products", "customers", "payment_links",
        "partial_payments", "refund_requests", "automation_rules", "fulfillment_jobs",
        "integration_connectors", "campaigns", "branches", "customer_wallets",
        "cashier_shifts", "ab_experiments", "analytics_events", "invoice_templates",
        "subscription_plans", "subscriptions", "discount_codes", "affiliates",
        "affiliate_commissions", "support_tickets", "support_messages",
        "payment_reminders", "payment_requests", "scheduled_invoices",
    }
    assert expected <= tables


def test_options_api_and_public_experience_contract():
    required_routes = (
        '"products": Product', '"payment-links": PaymentLink',
        '"invoice-templates": InvoiceTemplate', '"subscription-plans": SubscriptionPlan',
        '"subscriptions": Subscription', '"discount-codes": DiscountCode',
        '"affiliates": Affiliate', '"support-tickets": SupportTicket',
        '"payment-requests": PaymentRequest', '"scheduled-invoices": ScheduledInvoice',
        '@router.get("/l/{slug}"', '@router.get("/customer/{customer_id}/{token}"',
        '@router.post("/api/v1/options/payment-requests/{request_id}/dispatch")',
        '@router.post("/api/v1/options/invoice-templates/{template_id}/create-invoice")',
        '@router.get("/qr/payment-link/{slug}.png"',
        '@router.post("/webhooks/sms/{merchant_id}/{token}/batch")',
    )
    for value in required_routes:
        assert value in ROUTES
    assert 'name="discount_code"' in PAYMENT_LINK
    assert 'name="affiliate_code"' in PAYMENT_LINK


def test_scheduler_discount_affiliate_and_fulfillment_contract():
    for value in (
        "process_commerce_schedules", "resolve_discount_code", "resolve_affiliate_code",
        "register_affiliate_commission", "dispatch_payment_request",
        "create_invoice_from_template", "ensure_ordering_window",
        "product.fulfillment_type", "abandoned_payment_reminder_minutes",
    ):
        assert value in SERVICE
    assert "commerce_scheduler_worker" in MAIN
    assert 'name="commerce-scheduler"' in MAIN
    assert conditions_match({"amount_rial_min": 100}, {"amount_rial": 101}) is True
    assert conditions_match({"amount_rial_max": 100}, {"amount_rial": 101}) is False


def test_portal_contains_requested_option_modules():
    for label in (
        "محصولات و تحویل خودکار", "لینک پرداخت دائمی", "دفترچه مشتریان",
        "اتوماسیون بدون کدنویسی", "اتصال‌های آماده", "کمپین و تحلیل تبلیغات",
        "شعبه و صندوق فروشگاهی", "قالب و زمان‌بندی فاکتور", "کد تخفیف",
        "همکاری در فروش", "اشتراک و تمدید", "درخواست پرداخت", "مرکز پشتیبانی",
        "قوانین ضدتقلب",
    ):
        assert label in OPTIONS


def test_migration_covers_new_invoice_links_and_is_additive():
    assert 'revision = "20260801_1200"' in MIGRATION
    for field in ("discount_id", "affiliate_id", "subscription_id"):
        assert field in MIGRATION
    assert "def downgrade" in MIGRATION
    assert "drop_table" not in MIGRATION
