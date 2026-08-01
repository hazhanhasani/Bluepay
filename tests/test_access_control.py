import ast
import sys
import types
from pathlib import Path

from app.models import Merchant


def _load_access_service_without_aiogram():
    try:
        import aiogram  # noqa: F401
    except ModuleNotFoundError:
        aiogram_module = types.ModuleType("aiogram")
        aiogram_module.Bot = object
        exceptions_module = types.ModuleType("aiogram.exceptions")

        class TelegramBadRequest(Exception):
            pass

        class TelegramForbiddenError(Exception):
            pass

        exceptions_module.TelegramBadRequest = TelegramBadRequest
        exceptions_module.TelegramForbiddenError = TelegramForbiddenError
        sys.modules["aiogram"] = aiogram_module
        sys.modules["aiogram.exceptions"] = exceptions_module

    from app.services import access_service

    return access_service


def _load_phone_normalizer():
    source_path = Path("app/bot/access.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_phone_number"
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["_phone_number"]


def test_required_channels_are_normalized_and_deduplicated():
    access_service = _load_access_service_without_aiogram()
    channels = access_service._normalize_channels(
        '[{"chat_id":-1001,"title":"A","join_url":"https://t.me/a"},'
        '{"chat_id":-1001,"title":"Duplicate","join_url":"https://t.me/b"},'
        '{"chat_id":-1002,"title":"B","join_url":"https://t.me/+invite"}]'
    )
    assert [item.chat_id for item in channels] == [-1001, -1002]


def test_phone_normalization_supports_iranian_and_international_numbers():
    phone_number = _load_phone_normalizer()
    assert phone_number("0912 123 4567") == "+989121234567"
    assert phone_number("+31 6 12345678") == "+31612345678"


def test_merchant_has_encrypted_phone_verification_fields():
    assert hasattr(Merchant, "phone_number_encrypted")
    assert hasattr(Merchant, "phone_last4")
    assert hasattr(Merchant, "phone_verified_at")


def test_access_gate_checks_telegram_contact_ownership():
    source = Path("app/bot/access.py").read_text(encoding="utf-8")
    assert "contact.user_id != message.from_user.id" in source
    assert "request_contact=True" in source
    assert "AccessGateMiddleware" in source


def test_admin_can_manage_required_channels_and_access_flags():
    source = Path("app/bot/admin.py").read_text(encoding="utf-8")
    assert 'F.data == "admin:access"' in source
    assert 'F.data == "admin:access:toggle:join"' in source
    assert 'F.data == "admin:access:toggle:phone"' in source
    assert 'F.data.startswith("admin:access:remove:")' in source
